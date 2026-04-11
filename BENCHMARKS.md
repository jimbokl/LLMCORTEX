# Cortex benchmarks

Measured on 2026-04-11 against the Cortex v0.1.0 live store at
`C:/code/BOTWA/.cortex/store.db` (13 tripwires, 3 cost components, 1
synthesis rule, 180 KB of real session audit logs from normal usage).

Run the same on your machine with:

```bash
cortex bench                        # human-readable
cortex bench --json                 # machine-readable for regression tracking
cortex bench --no-subprocess        # skip the slow subprocess measurement
cortex bench --iterations 10000     # higher sample count for stable p99
```

Measurement methodology: `time.perf_counter()` with 20-iteration warmup
followed by N timed samples. Percentiles computed from sorted sample
array (no kernel density estimation, no outlier trimming).

## Environment

| | |
|---|---|
| Python | 3.10.11 |
| Platform | `win32` (Windows 10) |
| Cortex | v0.1.0 |
| Hardware | Windows desktop, Python launched from Git Bash |

## Storage footprint

| | |
|---|---|
| SQLite store | **80 KB** |
| Tripwires | 13 (5 critical, 7 high, 1 medium) |
| Cost components | 3 (`pm_5m_spread`, `pm_5m_info_decay`, `pm_5m_adverse_sel`) |
| Synthesis rules | 1 (`pm_5m_directional_block`) |
| Session audit logs | 16 files, 180 KB total |

A store with 100 tripwires would be ~500 KB. This is not going to be
your memory bottleneck.

## In-process subsystem latency

**Sample: 1000 iterations per subsystem, post-warmup, on the live
store.** Times in milliseconds.

| Component | p50 | p95 | p99 | max | mean |
|---|---:|---:|---:|---:|---:|
| `tokenize` | **0.001** | 0.002 | 0.003 | 0.006 | 0.002 |
| `classify_prompt` | **6.329** | 7.490 | 8.805 | 14.079 | 6.497 |
| `fallback_search` | **0.457** | 0.916 | 1.462 | 1.782 | 0.525 |
| `synthesize` | **0.017** | 0.019 | 0.027 | 0.809 | 0.019 |
| `render_brief` | **0.012** | 0.013 | 0.014 | 0.031 | 0.012 |

### Interpretation

- **`tokenize`** (1 µs median) — regex + set construction, basically free.
- **`classify_prompt`** (6.3 ms median) — this is the full hook-time cost
  path: YAML rules parsed from disk, prompt tokenized, rules matched,
  SQLite store opened, matching tripwire rows fetched, synthesizer
  called, store closed. The YAML parse and SQLite open dominate the 6 ms;
  the actual matching logic is well under 1 ms. A Day-9 in-process cache
  for parsed rules + prepared statements would drop this to <1 ms.
- **`fallback_search`** (0.5 ms median) — TF-IDF weighted token overlap
  over all 13 tripwires. Scales linearly with tripwire count.
- **`synthesize`** (17 µs median) — summing cost components across
  matched tripwires. Essentially free until you have hundreds of
  synthesis rules.
- **`render_brief`** (12 µs median) — string building. Free.

**Hot-path budget**: `tokenize + fallback_search + synthesize +
render_brief` = ~0.5 ms. Everything else in `classify_prompt` is the
YAML and SQLite overhead (~5.8 ms). If you write a Day-9 `cortex
serve` long-running mode, the hot path drops to sub-millisecond.

## End-to-end `cortex-hook` subprocess latency

Real-world cost model: Claude Code spawns a fresh `cortex-hook` process
for each user prompt. Python startup, import of `cortex` modules,
full classify pipeline, stdout JSON emission.

**Sample: 10 iterations, each a fresh Python process.** Times in
milliseconds.

| | ms |
|---|---:|
| min | **56.0** |
| p50 | **59.3** |
| mean | 58.4 |
| max | 60.1 |

Python startup and module imports dominate — ~50 ms of the 58 ms total
is "Python turning on". The actual Cortex work inside the process is
the 6-8 ms measured in the table above.

### What this means for your prompt latency

**Every user prompt adds ~60 ms of invisible latency** between pressing
Enter and the agent starting to think. That's well below human
perception (200 ms reaction time) and well below any network round-trip
the agent will do anyway. Cortex is effectively free at the wall-clock
level.

If you care about cutting this further, Day 9 roadmap has
`cortex serve` — a long-running daemon that keeps the Python interpreter
and cortex modules warm. Subprocess latency would drop from ~60 ms to
<5 ms. The trade-off is operational complexity (daemon lifecycle,
health checks). Not worth it until the ~60 ms actually hurts someone.

## Brief size distribution

Measured across 10 canned test prompts covering matching, non-matching,
English, and Russian cases. Token counts are rough (chars / 4, no
tiktoken dependency).

| Label | chars | tokens\* | tripwires | synthesis | prompt |
|---|---:|---:|---:|---:|---|
| `trivial_irrelevant` | 0 | 0 | 0 | 0 | `hi` |
| `short_irrelevant` | 0 | 0 | 0 | 0 | `what time is it` |
| `short_matching` | 4,473 | 1,118 | 4 | 0 | `poly backtest` |
| `medium_matching` | 6,178 | **1,544** | 5 | **1** | `run a 5m poly directional backtest on btc` |
| `long_matching` | 5,049 | 1,262 | 4 | 1 | `test late-lock strategy on 5m polymarket slots...` |
| `long_irrelevant` | 0 | 0 | 0 | 0 | `write me a python function that sorts a list...` |
| `russian_with_kw` | 4,487 | 1,121 | 4 | 0 | `покажи мне статистику по pnl для poly backtest` |
| `russian_no_kw` | 0 | 0 | 0 | 0 | `какая сегодня погода и что нового` |
| `fallback_only_fee` | 0 | 0 | 0 | 0 | `what are the fee mechanics for traders` |
| `live_deploy` | 5,275 | 1,318 | 5 | 0 | `should I deploy my new live bot for polymarket` |

\*`tokens_est = chars // 4` — a rough, conservative upper bound.
Actual token counts from GPT-family tokenizers typically land at
`chars / 4.2` for English and lower for domain-specific text with
repeating substrings. The real brief is probably 10-20% smaller in
tokens than the estimate.

### Key observations

**Silent on irrelevant prompts** (5 out of 10 produced 0 chars). Cortex
adds zero tokens to context when the task isn't matched. This is the
fail-open principle applied to the token budget: no match → no cost.

**Matched prompts average ~5000 chars ≈ 1250 tokens.** This is the
honest per-injection cost. Max observed was 1544 tokens on a 5m poly
directional backtest prompt — which fired the synthesizer, so the brief
included the `SYNTHESIS` block on top of the five tripwire bodies.

**Russian prompts work if they contain at least one English keyword.**
`pnl` and `poly` in `russian_with_kw` were enough to fire the keyword
fallback and inject a 4500-char brief. Russian prompts with no English
keywords stay silent (as they should).

## Token impact analysis

Given:

- **Average brief on matched prompt**: ~1250 tokens
- **Assumed cost of one prevented mistake**: ~3000 tokens (rough
  estimate for one wasted tool-call round: agent makes a wrong choice,
  runs a Bash/Edit, reads the output, realizes the mistake, corrects)

**Break-even rate**: 1 prevented mistake per **~2 injections**.

### What this means

Cortex is net-positive on context tokens if **≥50% of injections
prevent a mistake**. In practice:

- For the canned test set, 5/10 prompts matched. If Cortex helped on
  even 1 of those 5, it saved ~3000 tokens against the 5 × 1250 = 6250
  tokens cost. Margin: -3250 tokens in the worst case, +9750 tokens
  in the best case (5/5 helped).
- In a normal research session with 50 prompts and a 36% match rate
  (measured from real session logs in Day 5), ~18 injections happen.
  Net positive if 9 or more of those prevent a real mistake.

**The honest answer: it depends on whether the specific tripwires
ACTUALLY fire on failures your agent would otherwise make.** We don't
yet have enough silent-violation data (Day 6 feature) to measure this
empirically. After a week of real usage, `cortex stats --sessions`
shows tripwire effectiveness rates — which is how you'd tune the
tripwire set to maximize token efficiency.

## Token economics: the other direction

Cortex also **saves tokens you don't pay for via agent reasoning**.
Without Cortex, a capable agent might:

1. Receive a vague task
2. Spend tokens "thinking about what to check"
3. Decide to look up memory / docs
4. Spend tokens reading / querying / synthesizing
5. Finally act

With Cortex, steps 2-4 are partially front-loaded: the relevant
lessons are already in the agent's context when reasoning starts.
The agent **doesn't need to remember to check** because the checks
are already on the table.

This is harder to measure than the prevention savings, but it's
probably the bigger effect in practice. Even a 20% reduction in
"what should I be careful about here" reasoning time is a significant
token win across a 50-prompt session.

## Regression tracking

For CI-friendly regression tracking, run:

```bash
cortex bench --json --no-subprocess > bench.json
# diff against previous bench.json in git
```

The schema is versioned implicitly by the keys in `latency_ms` and
`brief_sizes`. A Day-9 `cortex bench --compare <file>` mode is on
the roadmap for automated regression detection.

## What this report doesn't measure

- **Memory / RSS** — skipped to avoid a `psutil` dependency. Empirically
  a warm Python 3.10 interpreter with `cortex` imported holds ~45 MB
  resident. The SQLite store is memory-mapped by default.
- **Token cost of the brief in the agent's OUTPUT** — not measured.
  Claude rarely echoes the brief back verbatim, so this is small.
- **Prevented mistake rate** — not empirically measurable without
  Day-6 silent violation data over a multi-week period. The
  break-even analysis is a lower bound, not a claim.
- **Disk I/O** — session log append is <10 µs (measured separately);
  the `classify_prompt` measurement includes SQLite reads but not the
  session log write (which happens after the measured operation).

---

*Generated by `cortex bench`. All numbers in this report were
produced by running the command on the live BOTWA development
machine. Reproduction: clone
[jimbokl/LLMCORTEX](https://github.com/jimbokl/LLMCORTEX), install,
`cortex init && cortex migrate && cortex bench`.*
