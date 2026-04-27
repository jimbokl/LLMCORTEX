# Cortex benchmarks

Numbers below were measured against the bundled example seed (9
tripwires, 3 cost components, 1 synthesis rule). Reproduce on your own
machine:

```bash
cortex init                     # create .cortex/store.db
cortex migrate                  # seed the example tripwires
cortex bench                    # human-readable
cortex bench --json             # machine-readable for regression tracking
cortex bench --no-subprocess    # skip the slow subprocess measurement
cortex bench --iterations 10000 # higher sample count for stable p99
```

Measurement methodology: `time.perf_counter()` with 20-iteration warmup
followed by N timed samples. Percentiles computed from the sorted
sample array — no kernel density estimation, no outlier trimming.

## Environment (reference run)

| | |
|---|---|
| Python | 3.10.11 |
| Platform | `win32` (Windows 10) |
| Cortex | v0.2.0 |
| Hardware | Windows desktop, Python launched from Git Bash |

## Storage footprint

| | |
|---|---|
| SQLite store | **~110 KB** with the example seed |
| Tripwires | 9 (7 critical, 2 high) |
| Cost components | 3 (deploy_no_ff_risk, deploy_staging_drift_risk, deploy_spof_risk) |
| Synthesis rules | 1 (`prod_deploy_unsafe`) |

A store with 100 tripwires lands around 500 KB. This is not going to be
your memory bottleneck.

## In-process subsystem latency

**Sample: 1000 iterations per subsystem, post-warmup, on the seeded
store.** Times in milliseconds.

| Component | p50 | p95 | p99 | max | mean |
|---|---:|---:|---:|---:|---:|
| `tokenize` | **0.006** | 0.008 | 0.010 | 0.011 | 0.006 |
| `classify_prompt` | **11.1** | 16.6 | 17.5 | 18.7 | 11.9 |
| `fallback_search` | **0.66** | 1.16 | 1.25 | 1.38 | 0.78 |
| `synthesize` | **0.026** | 0.030 | 0.046 | 0.256 | 0.027 |
| `render_brief` | **0.011** | 0.022 | 0.023 | 0.052 | 0.012 |

### Interpretation

- **`tokenize`** (~6 µs median) — regex + set construction, basically
  free.
- **`classify_prompt`** (~11 ms median) — full hook-time cost path:
  YAML rules parsed from disk, prompt tokenized, rules matched, SQLite
  store opened, matching tripwire rows fetched, synthesizer called,
  store closed. The YAML parse and SQLite open dominate; the matching
  logic itself is well under 1 ms. An in-process cache for parsed
  rules + prepared statements would drop this to <1 ms.
- **`fallback_search`** (~0.7 ms median) — TF-IDF weighted token
  overlap over all seeded tripwires. Scales linearly with tripwire
  count.
- **`synthesize`** (~30 µs median) — summing cost components across
  matched tripwires. Essentially free until you have hundreds of
  synthesis rules.
- **`render_brief`** (~12 µs median) — string building. Free.

**Hot-path budget** (everything except `classify_prompt`): ~0.7 ms.
The remainder of `classify_prompt` is YAML and SQLite overhead. A
warm long-running mode would bring the hot path well under 1 ms.

## End-to-end `cortex-hook` subprocess latency

Real-world cost model: Claude Code spawns a fresh `cortex-hook` process
for each user prompt. Python startup, import of `cortex` modules,
full classify pipeline, stdout JSON emission.

Typical numbers from `cortex bench` on the reference machine:

| | ms |
|---|---:|
| min | **~56** |
| p50 | **~60** |
| mean | ~58 |
| max | ~65 |

Python startup and module imports dominate — most of the measured
time is "Python turning on". The actual Cortex work inside the
process is the millisecond-scale measurements above.

### What this means for your prompt latency

**Every user prompt adds ~60 ms of invisible latency** between
pressing Enter and the agent starting to think. That is well below
human perception (≈200 ms reaction time) and well below any network
round-trip the agent will do anyway. Cortex is effectively free at
the wall-clock level.

If you care about cutting this further, a long-running daemon mode
that keeps the Python interpreter and cortex modules warm could drop
subprocess latency from ~60 ms to <5 ms. The trade-off is operational
complexity (daemon lifecycle, health checks). Not worth it until the
~60 ms actually hurts someone.

## Brief size distribution

Measured against the canned test prompts in `cortex.bench.TEST_PROMPTS`
(matching, non-matching, English, and Russian cases). Token counts are
rough (`chars / 4`, no tiktoken dependency).

| Label | chars | tokens\* | tripwires | synthesis | prompt |
|---|---:|---:|---:|---:|---|
| `trivial_irrelevant` | 0 | 0 | 0 | 0 | `hi` |
| `short_irrelevant` | 0 | 0 | 0 | 0 | `what time is it` |
| `short_matching` | ~5,960 | ~1,490 | 4 | 1 | `production deploy` |
| `medium_matching` | ~3,280 | ~820 | 2 | 0 | `run a backtest against the prod config caps` |
| `long_matching` | ~5,960 | ~1,490 | 4 | 1 | `…ship the new pricing feature to production…` |
| `long_irrelevant` | 0 | 0 | 0 | 0 | `write me a python function that sorts a list…` |
| `russian_with_kw` | ~7,200 | ~1,800 | 5 | 1 | `запусти backtest на prod config перед deploy` |
| `russian_no_kw` | 0 | 0 | 0 | 0 | `какая сегодня погода и что нового` |
| `fallback_only_secrets` | 0 | 0 | 0 | 0 | `remember to redact tokens before logging` |
| `live_deploy` | ~5,960 | ~1,490 | 4 | 1 | `should I deploy my new release to production today` |

\*`tokens_est = chars // 4` — a rough, conservative upper bound. Actual
token counts from GPT-family tokenizers typically land at `chars / 4.2`
for English and lower for domain-specific text with repeating
substrings. The real brief is probably 10-20% smaller in tokens than
the estimate.

### Key observations

**Silent on irrelevant prompts.** Half of the canned test prompts
produced 0 chars. Cortex adds zero tokens to context when the task
isn't matched. This is the fail-open principle applied to the token
budget: no match → no cost.

**Matched prompts average ~1300-1500 tokens.** This is the honest
per-injection cost. Largest observed in this canned set was the
Russian-context deploy prompt at ~1800 tokens — the synthesis section
plus four tripwire bodies adds up.

**Russian prompts work if they contain at least one English keyword.**
`backtest`, `prod`, `config`, `deploy` in `russian_with_kw` were enough
to fire the rule engine and inject a multi-tripwire brief. Russian
prompts with no English keywords stay silent (as they should, with the
default ASCII-only tokenizer; opt in to `CORTEX_UNICODE_TOKENS=1` for
Cyrillic-aware tokenization with light RU stemming).

## Token impact analysis

Given:

- **Average brief on matched prompt**: ~1300 tokens
- **Assumed cost of one prevented mistake**: ~3000 tokens (rough
  estimate for one wasted tool-call round: agent makes a wrong choice,
  runs a Bash/Edit, reads the output, realizes the mistake, corrects)

**Break-even rate**: 1 prevented mistake per **~2 injections**.

### What this means

Cortex is net-positive on context tokens if **≥50% of injections
prevent a mistake**. In practice:

- For the canned test set, 5/10 prompts matched. If Cortex helped on
  even 1 of those 5, it saved ~3000 tokens against the 5 × 1300 = 6500
  tokens cost. Margin: ~−3500 tokens in the worst case, ~+8500 tokens
  in the best case (5/5 helped).
- In a normal coding session with 50 prompts and a ~30-40% match rate,
  you typically see 15-20 injections. Net positive if at least half
  prevent a real mistake.

**The honest answer: it depends on whether the specific tripwires
ACTUALLY fire on failures your agent would otherwise make.** Without
real silent-violation data we cannot measure this empirically. After a
week of real usage, `cortex stats --sessions` shows tripwire
effectiveness rates — which is how you tune the tripwire set to
maximize token efficiency.

## Token economics: the other direction

Cortex also **saves tokens you don't pay for via agent reasoning**.
Without Cortex, a capable agent might:

1. Receive a vague task
2. Spend tokens "thinking about what to check"
3. Decide to look up memory / docs
4. Spend tokens reading / querying / synthesizing
5. Finally act

With Cortex, steps 2-4 are partially front-loaded: the relevant lessons
are already in the agent's context when reasoning starts. The agent
**doesn't need to remember to check** because the checks are already
on the table.

This is harder to measure than the prevention savings, but it's
probably the bigger effect in practice. Even a 20% reduction in "what
should I be careful about here" reasoning time is a significant token
win across a 50-prompt session.

## Regression tracking

For CI-friendly regression tracking:

```bash
cortex bench --json --no-subprocess > bench.json
# diff against previous bench.json in git
```

The schema is versioned implicitly by the keys in `latency_ms` and
`brief_sizes`.

## What this report doesn't measure

- **Memory / RSS** — skipped to avoid a `psutil` dependency.
  Empirically a warm Python 3.10 interpreter with `cortex` imported
  holds ~45 MB resident. The SQLite store is memory-mapped by default.
- **Token cost of the brief in the agent's OUTPUT** — not measured.
  Claude rarely echoes the brief back verbatim, so this is small.
- **Prevented mistake rate** — not empirically measurable without
  silent-violation data over a multi-week period. The break-even
  analysis is a lower bound, not a claim.
- **Disk I/O** — session log append is <10 µs (measured separately);
  the `classify_prompt` measurement includes SQLite reads but not the
  session log write (which happens after the measured operation).

---

*Generated by `cortex bench`. Reproduce against your own seeded store
by cloning [jimbokl/LLMCORTEX](https://github.com/jimbokl/LLMCORTEX),
installing, and running `cortex init && cortex migrate && cortex
bench`. Numbers will vary with hardware; the relative ordering across
subsystems is what matters.*
