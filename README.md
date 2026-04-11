<div align="center">

# Cortex

### Vector search is passive memory. Cortex is active instinct.

**The Claude Code hook that puts the right lesson on your agent's desk  
_before_ it reasons about the task.**

[![pypi](https://img.shields.io/pypi/v/llmcortex-agent)](https://pypi.org/project/llmcortex-agent/)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-219%20passing-brightgreen)](tests/)
[![code style](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![fail-open](https://img.shields.io/badge/contract-fail--open-blue)](docs/architecture.md)

```bash
pip install llmcortex-agent
```

**[PyPI](https://pypi.org/project/llmcortex-agent/)** · **[GitHub](https://github.com/jimbokl/LLMCORTEX)** · **[Architecture](docs/architecture.md)** · **[Benchmarks](BENCHMARKS.md)**

</div>

---

## Why this exists

Vector stores, RAG, Palace memory — they all answer **"what is similar to this query?"**. Useful when the agent _already knows to ask_. But agents rarely know to ask. They dive into the task, recreate a bug they fixed last week, misread a fee formula they already benchmarked, run a backtest on features that leak the future.

By the time the agent realizes it should have checked, the mistake is in the commit. Or worse — in the live trade.

**Cortex is the read-before-you-reason layer.** A single Claude Code hook intercepts every user prompt, classifies it against a curated store of _tripwires_ (structured lessons from your own past failures), and injects the matched lessons into the agent's working context **before the agent spends a token reasoning**. No RAG query to write. No "remember to check X" ritual. The agent walks into the task pre-briefed.

## What an injection looks like

Not a mock. Literal stdout of `cortex-hook` fed a real research prompt, pulled from a live session:

```
<cortex_brief n="5" critical="4">
Cortex matched rule(s): poly_backtest_task, poly_directional_5m

SYNTHESIS (cumulative cost from matched tripwires):
  pm_5m_directional_block: Sum = 19.65pp (threshold 5.0pp, op gte)
    +2.4pp    spread_slip              [directional_5m_dead]
    +7.25pp   info_decay_5min          [information_decay_5m]
    +10.0pp   adverse_selection        [adverse_selection_maker]
    >> Sum structural drag = 19.65pp (3 components) >= 5.0pp floor.
       Any directional 5m strategy needs pre-fee edge > 19.65pp to
       even be testable.

The following lessons apply to this task. Each cost real money or
research time in the past. Read them before committing to an approach:

[1] poly_fee_empirical  --  CRITICAL
    Polymarket net fee = 0.072 x min(p, 1-p) x size  --  NOT 10% flat
    ...

[2] real_entry_price  --  CRITICAL
    Use real up_ask/dn_ask entry -- never $0.50 midpoint (33x inflation)
    ...
</cortex_brief>
```

The `19.65pp` number is **computed at runtime** by summing three separate cost components tied to three separate tripwires. Most memory systems list matched lessons. Cortex _sums_ them. That composition — turning three individual warnings into one blocking number — is what the agent would not have done on its own, and is the reason Cortex exists.

## Install

```bash
pip install llmcortex-agent
```

**One runtime dependency** (`pyyaml`). Everything else is Python stdlib. 71 KB wheel. Zero network calls outside your machine. Python 3.10+.

Optional extras:

```bash
pip install "llmcortex-agent[dev]"   # pytest + ruff (for contributing)
pip install "llmcortex-agent[dmn]"   # anthropic SDK (for `cortex reflect`)
```

> **Naming note**: the PyPI distribution is `llmcortex-agent` because `cortex-agent` was already taken by an unrelated project. The Python import name stays `cortex` — so you install `llmcortex-agent` but write `from cortex.store import CortexStore`.

## Wire it into Claude Code (30 seconds)

```bash
cortex init              # create .cortex/store.db
cortex migrate           # seed 11 example tripwires from MEMORY.md
cortex stats             # sanity check

mkdir -p .claude
cat > .claude/settings.json <<'EOF'
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type":"command","command":"cortex-hook"}]}],
    "PostToolUse":      [{"hooks": [{"type":"command","command":"cortex-watch"}]}]
  }
}
EOF
```

Done. Your next prompt containing keywords like `backtest`, `poly`, `5m`, `directional`, `live deploy` automatically fires the hook.

Test without launching Claude Code:

```bash
echo '{"prompt":"run a 5m poly directional backtest"}' | cortex-hook | python -m json.tool
```

## Active capabilities (Claude Code skills)

Cortex ships five Claude Code skills that turn Cortex from a passive
hook into an active toolbox the agent reaches for on its own:

```bash
cortex install-skills            # copy into ~/.claude/skills/
cortex install-skills --list     # show what's bundled
cortex install-skills --project  # install into ./.claude/skills/ instead
```

| skill | what it does | activates on |
|---|---|---|
| `cortex-bootstrap` | seeds Cortex from local docs on a fresh project | "set up cortex", "обучи cortex" |
| `cortex-capture-lesson` | distills an incident the user describes into an inbox draft | "we just lost X", "post-mortem", "запомни этот баг" |
| `cortex-search` | actively queries the store before committing to an approach | "what does cortex know about X" |
| `cortex-tune` | reads Phase-0 fitness, proposes rule rewording / depreciation | "cortex tuning", "which lessons aren't working" |
| `cortex-status` | six-check health audit at the start of any session | "is cortex working here", "cortex status" |

The skills never write to the store directly — they always go through
the inbox workflow so the human stays in the approval loop. Skills
auto-activate from their description frontmatter; restart your
Claude Code session after the first install to pick them up.

## Use cases

Cortex was born in a Polymarket research project, but the pattern is domain-agnostic. You want Cortex if one or more of these sounds familiar:

### Your agent makes the same expensive mistake twice

Every engineering team has a graveyard of `feedback_*.md` and `post-mortem.md` files describing past failures. None of them get read at the right moment. Cortex turns each file into a tripwire that _injects itself automatically_ when the triggering task appears. The agent doesn't need to know the file exists.

### Your research pipeline compounds small biases into dead hypotheses

Spread (+2.4pp) + info decay (+7.25pp) + adverse selection (+10pp) = **19.65pp of drag that makes any 5-min directional signal structurally impossible**. None of those three facts individually says "stop." The Cortex **synthesizer** sums them across all matched tripwires and surfaces the cumulative cost as a single number. That's the novel contribution.

### You run backtests where "it worked in sim but bled live"

Cortex ships a lookahead-bug verifier (`cortex-check-lookahead`) that greps any feature pipeline for the canonical floor-of-open-time pattern `slot_ts = (ts // N) * N`. Wire it into the hook with `CORTEX_VERIFY_ENABLE=1`. Set `CORTEX_VERIFY_BLOCK=1` and critical failures exit code 2 — Claude Code rejects the prompt. Your "it worked in sim" commit gets stopped before it's a commit.

### Your team has institutional memory that dies with whoever wrote it

`cortex import-palace "tail risk strategy" --to-inbox` stages Palace drawers as editable JSON drafts. A human fills in TODO fields and runs `cortex inbox approve`. Day-9 `cortex suggest-patterns` reads real session logs and auto-generates regex candidates so nobody has to write `violation_patterns` by hand.

### You want to _measure_ whether a warning actually gets applied

Day-6 silent-violation detection watches every tool call after each injection and regex-matches against the tripwire's `violation_patterns`. `cortex stats --sessions` prints a per-tripwire `[OK]` / `[WARN]` / `[FAIL]` status with a violation rate. Rate near 0 = lesson applied. Rate > 0.5 = rewrite the brief, or flip on blocking mode.

### You run a subagent orchestrator and want shared institutional memory

Cortex hooks into the top-level `UserPromptSubmit`. Every subagent spawned by the orchestrator inherits the same tripwire store, synthesis rules, and audit log. Zero per-subagent configuration.

## Before and after

**Without Cortex:**

```
You:     "Let's try a directional signal on 5m poly slots"
Agent:   "Sure, I'll set up a replay..."
Agent:   [8 minutes of reasoning, writes replay script]
Agent:   "Done. 1344 trades, +$284, z=+3.90. Looks promising!"
You:     "Wait, did you check for lookahead?"
Agent:   "Let me check... [3 more minutes] ... yes, the feature
         pipeline uses slot_ts = (ts // 300) * 300, which is a
         lookahead. The real edge is -0.31σ."
You:     "We learned this 3 weeks ago."
```

**With Cortex:**

```
You:     "Let's try a directional signal on 5m poly slots"

                    [ cortex-hook fires, <60ms ]
                    
         SYNTHESIS: Sum drag = 19.65pp >= 5.0pp floor
           +2.4pp  spread_slip       [directional_5m_dead]
           +7.25pp info_decay_5min   [information_decay_5m]
           +10.0pp adverse_selection [adverse_selection_maker]
           >> Any directional 5m strategy needs edge > 19.65pp
              to even be testable. Known structural dead zone.
              9 prior replay tests all failed Bonferroni.

Agent:   "Before building this, the synthesizer flags 19.65pp of
         structural drag on 5m directional. Your last 9 replay
         tests on similar hypotheses all failed Bonferroni. Path
         forward is structural arbitrage (late-lock, settlement,
         whale copy). Want me to draft one of those instead?"
```

The lessons were already in your memory both times. The difference is whether the agent **read them before starting**.

## Numbers that matter

All measured on the live development machine with `cortex bench`. Reproduce with `pip install llmcortex-agent && cortex init && cortex migrate && cortex bench`.

### Speed (measured, not claimed)

| Subsystem | p50 | p99 |
|---|---:|---:|
| Tokenize | **0.001 ms** | 0.003 ms |
| Full `classify_prompt` | **6.3 ms** | 8.8 ms |
| TF-IDF fallback | **0.5 ms** | 1.5 ms |
| Synthesize | **0.017 ms** | 0.027 ms |
| Render brief | **0.012 ms** | 0.014 ms |
| **End-to-end `cortex-hook` subprocess** | **~59 ms** | — |

The 59 ms is a **fresh Python process on every prompt** — the real cost model Claude Code uses. ~50 ms of that is Python startup; Cortex itself runs in 6-9 ms. Below human perception (200 ms reaction time), well below any network hop your agent makes anyway. **Invisible.**

### Token economics

| Metric | Value |
|---|---:|
| Avg brief per matched prompt | **~1,250 tokens** |
| Max brief observed | ~1,544 tokens |
| Non-matched prompts (silent) | **0 tokens** |
| Assumed cost of one prevented mistake | ~3,000 tokens (one wasted tool-call cycle) |
| **Break-even rate** | **1 prevented mistake per 2 injections** |

Cortex is net-positive on context tokens if **at least 50% of injections prevent a mistake**. In practice, the tripwires that fire are the ones your agent would have violated — the break-even is almost always beaten.

### DMN reflection cost (Day-11 Haiku loop)

| Metric | Value |
|---|---:|
| Prompt size on a real 17-session sample | ~1,069 tokens |
| Haiku 4.5 input + output per call | **~$0.011** |
| One reflection per week × 52 | **~$0.57 / year** |

Fifty-seven cents a year to keep your tripwire set growing from real session data.

### Engineering footprint

| Metric | Value |
|---|---:|
| Python source | ~3,100 LOC across 14 modules |
| Tests | **219 passing, 4.2 s** |
| Runtime dependencies | **`pyyaml` only** |
| Wheel size | **71 KB** |
| Ruff clean | every file |
| Fail-open paths | covered by `test_hook.py` + `test_watch.py` |

## In production (13 days, audit log receipts)

Cortex has been wired into a live Claude Code project via `UserPromptSubmit` + `PostToolUse` hooks since Day 7. Everything below is real audit-log data, pulled from `cortex stats --sessions --anonymize` — safe to share publicly (session ids hashed, tool_input snippets redacted).

### Headline numbers

| Metric | Value |
|---|---:|
| `cortex_brief` injected into the agent's context | **20 primary + 21 fallback = 41 times** |
| Synthesizer fires on real prompts | **16 times** |
| Silent violations detected by `cortex-watch` | 1 (one deliberate Day-6 test case) |
| Top-fired tripwire | `poly_fee_empirical` — **33 hits** ($500 past cost) |
| Runner-up | `real_entry_price` — **29 hits** (critical) |
| Third | `backtest_must_match_prod` — **24 hits** (critical) |
| Cold tripwires (never matched) | 2 — candidates for retirement |

### Primary vs fallback — the empirical architecture decision

On Day 4 I almost built a Palace semantic-search daemon (350 LOC + ONNX + HTTP daemon). Killed it the same day and replaced it with a 130-line TF-IDF scorer over tripwire bodies. **5 days of production audit data tell me that was the right call**: the fallback picks up briefs the rule engine missed in **10 of 17 active sessions**. In raw event counts the fallback fires as often as the primary (ratio ~1.05×). Without Day 4, half of all injections wouldn't exist.

This is the meta-case Cortex catches on itself: **the tool detected its own rule-engine blind spot via the same audit log it uses to catch agent failures**. See [the 3.6× ratio blog post](docs/blog/2026-04-11-the-36x-ratio.md) for the full story.

### Real session timeline excerpt (anonymized)

One live session, pulled verbatim from `cortex timeline <sid> --anonymize`:

```
Session timeline: anon_c729080f
  (showing first 12 of 176 events)
==================================================================
  +00:00:00  INJECT      rules=poly_live_deploy
             5 tripwires: poly_fee_empirical, never_single_strategy,
                          lookahead_parquet, backtest_must_match_prod,
                          no_budget_paper
  +00:26:37  INJECT      rules=backtest_vs_prod_match,poly_late_lock,poly_fee_pnl
             5 tripwires: poly_fee_empirical, lookahead_parquet,
                          backtest_must_match_prod, real_entry_price,
                          adverse_selection_maker  [SYNTH]
  +00:31:03  FALLBACK    2 tripwires: backtest_must_match_prod, never_single_strategy
  +00:59:21  INJECT      rules=poly_fee_pnl
             2 tripwires: poly_fee_empirical, real_entry_price
```

One anonymized real session, 60 minutes of work, **three distinct inject events** plus one TF-IDF fallback. Second inject fired the **synthesizer** (`[SYNTH]` marker) — compound rule match across `backtest_vs_prod_match + poly_late_lock + poly_fee_pnl` triggered the cost-component composition for the first time on a real prompt, not a test.

**That's what "active instinct" looks like when it works.** Not "the tool saved $N" (counterfactual — impossible to prove). Just: the agent walked into three separate task shapes pre-briefed with the right lessons, automatically, in 60 minutes of real work.

### What the data doesn't yet show

Per-tripwire violation rate stays at 0.0 for most lessons because **only 2 of 13 seeded tripwires have `violation_patterns`**. Silent-violation detection needs regex to match against tool_input, and regex authoring is still partially manual (Day 9 `cortex suggest-patterns` auto-generates candidates from session data; it needs a few more weeks of real usage to produce high-confidence regexes for the remaining 11 tripwires).

The honest headline: **we can measure what gets injected, we can't yet measure what gets prevented.** That's a known limitation, documented in [BENCHMARKS.md](BENCHMARKS.md) and on the Day-13+ roadmap.

## The six layers of defense

Cortex is not one more rule engine. It's six independent subsystems over a single 80 KB SQLite store, each targeting a different failure mode:

| # | Layer | Question it answers |
|---|---|---|
| 1 | **Classify** (YAML rules) | _What is this task about, and which lessons apply?_ |
| 2 | **Synthesize** (cost-component sums) | _Do the drags of the matched lessons exceed any plausible edge?_ |
| 3 | **Fallback** (TF-IDF over tripwire bodies) | _Did the rule engine miss due to a vocabulary gap?_ |
| 4 | **Verify** (pre-flight code grep, auto-run from hook) | _Is the bug from the past incident still present in the current codebase right now?_ |
| 5 | **Audit + Detect** (jsonl session log, silent violations) | _Which lessons got shown, and did the agent apply them?_ |
| 6 | **Reflect** (Haiku DMN loop) | _What new tripwires should we add based on last week's sessions?_ |

Most tools solve one of these. Cortex solves three and **measures effectiveness on a fourth**.

Full design rationale: [docs/architecture.md](docs/architecture.md).

## Why not just...

### ...use a vector store?
Vector stores answer **"what's similar to this query?"** — useful when you already know to ask. Cortex answers **"what should I be warned about right now?"** — the question your agent faces at task start, automatically, with zero queries. They're different questions. Cortex sits **alongside** your vector store, not instead of it.

### ...prompt-engineer Claude to check things?
Tried that. Works for one specific warning. Doesn't scale to 15 domain-specific lessons across 6 task shapes. YAML rules are diffable, inspectable, testable. Prompt strings are a mess of "please also consider".

### ...have the agent read the docs?
Docs rot. Docs are too long. Docs don't get loaded into working context because the agent doesn't think to load them. Cortex puts the precise ~1,250 tokens you need — and nothing else — into the context before reasoning starts.

### ...use an LLM classifier?
Predictability. Rules are diffable in git. When a false positive shows up in the logs, you tighten the rule and re-test. An LLM classifier is a black box that hallucinates on some non-zero fraction of prompts — and you silently ship bad injections on exactly the cases you're least equipped to notice.

The TF-IDF fallback exists for when the rule engine misses due to vocabulary gaps. Still deterministic. Still grep-able. Still fixable in a PR.

### ...use `memory/` in Claude Code natively?
That's text in the system prompt — always loaded, for every prompt, regardless of relevance. Cortex is targeted: injected only when the specific task matches a specific tripwire, with cumulative cost computation that pure text can't do.

## What Cortex is NOT

- **Not a replacement for vector memory.** Palace / ChromaDB still answer "what's in my knowledge base"; Cortex answers "what should I be warned about right now".
- **Not a linter.** It operates on task intent, not code correctness.
- **Not an RL feedback loop.** Violation tracking is passive audit; rule tuning is human-driven.
- **Not a general agent framework.** It's a Claude Code integration point.
- **Not magic.** It can't help with lessons you haven't yet encoded as tripwires. The first week is you writing tripwires — or running `cortex reflect` to have Haiku propose them.

## Honest about rejected paths

Good tools document what they chose _not_ to build and why. Cortex has three such post-mortems in [CHANGELOG.md](CHANGELOG.md):

- **Day 4 — Palace semantic daemon**: built and killed in one day. 350 LOC of HTTP daemon + ONNX model + warmup logic, replaced by a 130-line TF-IDF function that gave strictly better coverage. **Lesson: measure before you build.**
- **Day 10 — `cortex serve` daemon**: subprocess cost measured at 59 ms is below human perception. A warm daemon would save ~10 seconds per day of real work, not worth the lifecycle complexity.
- **Day 9 — auto-regex without safety gate**: the regex generator overfits to the bug pattern and starts firing on the common fix. We added `--fix-example` as a non-optional quality gate.

If you're evaluating Cortex, read those. You'll learn more from what we didn't build than from what we did.

## Quickstart (end to end, ~5 minutes)

```bash
# 1. Install
pip install llmcortex-agent

# 2. Initialize store + seed example tripwires
cortex init
cortex migrate
cortex list

# 3. Simulate the hook on a realistic prompt
echo '{"session_id":"test","prompt":"run a 5m poly directional backtest"}' \
  | cortex-hook \
  | python -m json.tool

# 4. Wire into Claude Code
mkdir -p .claude
cat > .claude/settings.json <<'EOF'
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type":"command","command":"cortex-hook"}]}],
    "PostToolUse":      [{"hooks": [{"type":"command","command":"cortex-watch"}]}]
  }
}
EOF

# 5. Go write code. Your agent is now pre-briefed on every matching prompt.

# 6. After a week of real usage, measure effectiveness
cortex stats --sessions

# 7. Let Haiku propose new tripwires from session data
export ANTHROPIC_API_KEY=sk-ant-...
cortex reflect --dry-run         # preview the prompt first
cortex reflect                    # ~$0.01, writes drafts to .cortex/inbox/
cortex inbox list                 # review proposals
cortex inbox approve dmn_haiku_...  # promote to store
```

## CLI reference

| Command | Purpose |
|---|---|
| `cortex init` | Create an empty `.cortex/store.db` |
| `cortex migrate` | Seed tripwires from the importers (idempotent) |
| `cortex list [--domain X] [--severity Y]` | Browse tripwires |
| `cortex show <id>` | Full detail of one tripwire |
| `cortex find w1,w2,w3` | Simulate a trigger match |
| `cortex stats` | Store summary by severity and domain |
| `cortex stats --sessions [--days N]` | Session audit: injection rate, cold tripwires, effectiveness |
| `cortex add ...` | Manually add a tripwire |
| `cortex import-palace "query" [--to-inbox]` | Smart search over Palace, print templates or stage drafts |
| `cortex inbox list / show / approve / reject` | Manage draft tripwires awaiting human approval |
| `cortex bench [--iterations N] [--json]` | Benchmark subsystem latency + storage + brief sizes |
| `cortex suggest-patterns <id> [--fix-example "..."]` | Auto-generate regex candidates for `violation_patterns` |
| `cortex reflect [--days N] [--dry-run]` | Haiku DMN: propose new tripwires into the inbox |
| `cortex-hook` | `UserPromptSubmit` hook entry point |
| `cortex-watch` | `PostToolUse` audit hook entry point |
| `cortex-check-lookahead --features-dir DIR` | Standalone lookahead-bug verifier |

## Docs

| Document | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Why Cortex exists. Three failure modes. Design decisions. Cortex vs Palace. Rejected paths. |
| [docs/authoring.md](docs/authoring.md) | How to write tripwires, rules, cost components, synthesis rules, verifiers, violation patterns. Includes a decision tree. |
| [docs/hooks.md](docs/hooks.md) | Claude Code hook contract. Environment variables. Manual testing. Troubleshooting. |
| [BENCHMARKS.md](BENCHMARKS.md) | Real latency / storage / brief-size / token-impact numbers. Reproducible on your machine. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Five ground rules for PRs. Dev setup. PR checklist. Release process. |
| [CHANGELOG.md](CHANGELOG.md) | Full Day-1-through-Day-12 feature history with rejected-path post-mortems. |

## Roadmap

**Days 1-12 — shipped:**

Day 1: store + CLI + 11 tripwires · Day 2: YAML rule engine + `UserPromptSubmit` hook · Day 3: synthesizer + code verifier + session audit + `PostToolUse` hook · Day 4: TF-IDF fallback (rejected Palace daemon) · Day 5: session stats + Palace import helper · Day 6: silent violation detection + effectiveness rates · Day 7: verifier auto-run from hook · Day 8: inbox workflow · Day 8.5: benchmark suite · Day 9: auto-regex pattern-suggest helper · Day 10: verifier blocking mode · Day 11: Haiku DMN reflection loop · Day 12: PyPI release.

**Next (no firm timeline):**

- **Trusted Publishing** for PyPI releases via GitHub Actions OIDC (no tokens)
- **`cortex bench --compare <baseline>`** for CI regression tracking
- **Multi-project store federation** (one `.cortex/` per repo, optional cross-project queries)
- **Web dashboard** for `cortex stats --sessions` visualization

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The five ground rules:

1. **Tripwires must be earned.** Specific past failure, quantifiable cost, actionable how-to-apply.
2. **Rules must be narrow.** Test with `cortex-hook` before shipping.
3. **Fail-open is non-negotiable.** A broken Cortex never blocks the user.
4. **Zero runtime deps beyond stdlib + pyyaml.** Every import slows every prompt.
5. **Violation patterns: prefer false negatives to false positives.** A missed violation is acceptable; a false positive erodes trust in the entire injection path.

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

*Built because the agents we trust to reason about our code  
have no institutional memory of what blew up last week.  
Cortex gives them one.*

**⭐ [github.com/jimbokl/LLMCORTEX](https://github.com/jimbokl/LLMCORTEX)**  
**📦 [pypi.org/project/llmcortex-agent](https://pypi.org/project/llmcortex-agent/)**

</div>
