<div align="center">

# Cortex

### Active memory and executive control for AI coding agents.

*Stop your agent from repeating the mistakes you already paid for.*

[![tests](https://img.shields.io/badge/tests-107%20passing-brightgreen)](tests/)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)
[![fail-open](https://img.shields.io/badge/contract-fail--open-blue)](docs/architecture.md#fail-open-contract)
[![runtime deps](https://img.shields.io/badge/runtime%20deps-pyyaml-lightgrey)](pyproject.toml)

</div>

---

## The $7.78 incident

On **2026-04-10**, a live trading bot was deployed after a backtest that showed
**152 out of 152 winning trades**. It ran for 82 minutes, made 4 trades, lost
$7.78, and auto-killed itself on the third consecutive loss.

The bug was a one-line lookahead error in the feature pipeline. The backtest
lied because it computed features using the same bugged code as the live
system. 100% WR in simulation became 69.6% in reality. $174 paper profit
became a $1.30 loss.

**The forensic report documenting the bug was written 2.5 hours after the kill.**

Memory didn't fail. There was *nothing in memory to fail with*. The agent
was blind to a lesson that didn't yet exist.

**Cortex is the system we built so that never happens again.**

---

## What Cortex actually does

Cortex hooks into Claude Code's `UserPromptSubmit` event. When you submit a
prompt, it runs through a four-stage pipeline in under 20ms and injects a
structured brief of relevant past lessons into the agent's working context
— *before* the agent reasons about your task.

```
User prompt ─────────────────────────────────────────┐
  "replay basis-arb on 5m poly slots"                │
                                                     ▼
                                          ┌────────────────────┐
                                          │    cortex-hook     │
                                          │                    │
                              ┌───────────┤  1. Classify       │
                              │           │     YAML rules     │
                              │  <1ms     │                    │
                              │           │  2. Synthesize     │
                              │           │     sum cost       │
                              │           │     components     │
                              │           │                    │
                              │           │  3. Fallback       │
                              │           │     TF-IDF scoring │
                              │           │                    │
                              │           │  4. Audit          │
                              │           │     jsonl log      │
                              │           └────────────────────┘
                              ▼
                 <cortex_brief> injected into agent context
                 (as hookSpecificOutput.additionalContext)
```

No vector search to trigger. No RAG query to write. No "remember to check X"
ritual. The agent gets the warning **automatically at the one moment it
matters** — before it starts.

Here's what the brief actually looks like for a real prompt:

```
<cortex_brief n="5" critical="4">
Cortex matched rule(s): poly_backtest_task, poly_lag_arb

SYNTHESIS (cumulative cost from matched tripwires):
  pm_5m_directional_block: Sum = 19.65pp (threshold 5.0pp, op gte)
    +2.4pp    spread_slip              [directional_5m_dead]
    +7.25pp   info_decay_5min          [information_decay_5m]
    +10.0pp   adverse_selection        [adverse_selection_maker]
    >> Sum structural drag = 19.65pp (3 components) >= 5.0pp floor.
       Any directional 5m strategy needs pre-fee edge > 19.65pp to
       even be testable. See directional_5m_dead for the full autopsy.

The following lessons apply to this task. Each cost real money or
research time in the past. Read them before committing to an approach:

[1] poly_fee_empirical  --  CRITICAL (past cost $500.00)
    Polymarket net fee = 0.072 x min(p, 1-p) x size  -  NOT 10% flat
    ...

[2] lookahead_parquet  --  CRITICAL (past cost $7.78)
    Feature parquets must be computable STRICTLY before decision time
    ...
</cortex_brief>
```

This is not a mock. It's the literal stdout of `cortex-hook` fed a real
research prompt. The `19.65pp` number is computed at runtime by summing
three cost components tied to three separate tripwires. That computation
is the bit that would have saved $7.78 on April 10.

---

## Install

```bash
git clone https://github.com/jimbokl/LLMCORTEX.git
cd LLMCORTEX
pip install -e ".[dev]"
```

**One runtime dependency** (`pyyaml`). Everything else is stdlib. 107 tests.
~1800 lines of Python. Zero telemetry, zero network calls outside your
machine.

---

## Wire it into Claude Code

```bash
# Inside your project directory
cortex init             # creates .cortex/store.db
cortex migrate          # seeds 13 example tripwires
cortex stats            # sanity check

# Hook it up
mkdir -p .claude
cat > .claude/settings.json <<'EOF'
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "cortex-hook"}]}
    ],
    "PostToolUse": [
      {"hooks": [{"type": "command", "command": "cortex-watch"}]}
    ]
  }
}
EOF
```

Your next prompt with any relevant keyword (`backtest`, `live`, `deploy`,
`5m`, `directional`, `fee`, `poly`, ...) fires the hook and injects the
brief. If nothing matches, the hook is silent and Claude Code proceeds
normally.

**Verify without launching Claude Code:**

```bash
echo '{"session_id":"test","prompt":"run a 5m poly directional backtest"}' \
  | cortex-hook \
  | python -m json.tool
```

---

## The six layers of defense

Cortex is not one more rule engine. It's six independent subsystems over a
single SQLite store, each targeting a different failure mode:

| # | Layer | Answers the question |
|---|---|---|
| 1 | **Classify** (YAML rules) | *What is this task about, and which lessons apply?* |
| 2 | **Synthesize** (cost sums) | *Do the drags of the matched lessons exceed any plausible edge?* |
| 3 | **Fallback** (TF-IDF over bodies) | *Did the rule engine miss due to a vocabulary gap?* |
| 4 | **Verify** (code grep) | *Is the bug from incident X still present in the current codebase?* |
| 5 | **Audit** (jsonl session log) | *What lessons got injected in this session? What tool calls happened?* |
| 6 | **Detect** (runtime regex) | *Did the agent act on the warning, or silently ignore it?* |

One rule engine fixes one failure mode. Cortex fixes **three** (blindness,
laziness, compositional ignore) and **measures effectiveness on a fourth**
(silent violation).

See [docs/architecture.md](docs/architecture.md) for the three failure
modes that drove the design and which layer targets each.

---

## Why the synthesizer matters

Most memory systems *list* matched lessons. Cortex **composes** them.

Real example from the session logs:

> **Task:** test a directional signal on 5m Polymarket slots
>
> **Matched tripwires (individual):**
> - Spread + slippage: 2.4pp per trade
> - Information decay: 1.45pp per minute × 5 min = 7.25pp
> - Adverse selection on maker fills: 10pp WR drop
>
> **Synthesizer:** `2.4 + 7.25 + 10.0 = 19.65pp ≥ 5.0pp threshold — FIRE`
>
> **Output:** *"Any directional 5m strategy needs pre-fee edge > 19.65pp
> to even be testable."*

The three individual lessons were already in the agent's memory. **None of
them individually said "stop".** The synthesizer is the only thing that
sums them and produces an actionable blocking signal. It's the bit that
closes the gap between "I knew each of those things" and "I didn't put
them together in time."

This is the novel contribution. The rest of Cortex is boring plumbing that
exists to make the synthesizer useful.

---

## Measured effectiveness (Day 6)

Cortex doesn't just inject. It **measures whether the injections are
applied**. Here's real output from a development session:

```
$ cortex stats --sessions

Cortex session audit (all-time)
============================================================
Sessions:                  14
Total events:              344
Sessions with inject:      5   (36%)
Sessions with fallback:    9   (64%)
Avg tool_calls / session:  23.4

Top matched tripwires:
    14 x  poly_fee_empirical
    10 x  real_entry_price
     7 x  backtest_must_match_prod
     4 x  never_single_strategy
     4 x  lookahead_parquet
     3 x  directional_5m_dead

Synthesis rules fired:
     3 x  pm_5m_directional_block

Silent violations detected: 1 across 1 session(s)
     1 x  lookahead_parquet

Tripwire effectiveness (violation rate = viol / hits):
  [WARN] lookahead_parquet       hits=6   viol=1   rate=0.17
  [OK  ] poly_fee_empirical      hits=16  viol=0   rate=0.00
  [OK  ] real_entry_price        hits=12  viol=0   rate=0.00
  [OK  ] backtest_must_match_prod hits=9  viol=0   rate=0.00

Cold tripwires (2 never matched in window):
  - book_holography_failed
  - late_lock_replay_traps
```

Tripwires with `violation_patterns` get a runtime effectiveness rate via
`cortex-watch`. Every tool call *after* an injection is regex-matched
against the tripwire's patterns. If the agent runs
`df['slot_ts'] = (df['ts'] // 300) * 300` after being warned about
lookahead, that's a `potential_violation` event. The regex is smart
enough to *not* flag `(df['ts'] // 300) * 300 + 300` — the honest
forward-shift fix.

**Rate = 0.00** means the lesson was applied every time it was shown.
**Rate > 0.5** means it was mostly ignored — rephrase the brief, or add
blocking enforcement, or kill the tripwire if it's crying wolf. Either
way, you have a number instead of a guess.

This closes the feedback loop. You're not arguing about whether Cortex is
worth running. You measure it.

---

## Fail-open is a promise

Every code path reachable from `cortex-hook` or `cortex-watch` **exits 0
on any error**. The hierarchy:

1. Empty stdin → exit 0, no output
2. JSON parse fails → exit 0, no output
3. Store missing or locked → exit 0, no output
4. Classifier crash → exit 0, no output
5. Verifier crash → exit 0, no output
6. Any other exception anywhere → exit 0, no output

**A broken Cortex must never block the user's interaction.** If the hook
throws, Claude Code sees empty `additionalContext` and proceeds normally.
You don't notice. There are tests for every failure path — see
[test_hook.py](tests/test_hook.py) and [test_watch.py](tests/test_watch.py).

---

## Rejected paths (honest CHANGELOG)

Cortex wasn't designed in one shot. Some things we built and deleted:

### Palace semantic daemon (Day 4 morning → Day 4 afternoon)

Built a `mempalace.searcher` HTTP daemon for semantic fallback. Killed
it the same day. Three reasons:

1. **Narrow coverage.** The ONNX embedding model is English-only. Short
   queries (`show top features`) scored zero hits. Russian queries scored
   zero hits. Only long, domain-specific English queries worked.
2. **Infrastructure cost.** Required a ~200MB warm daemon holding ONNX
   + chromadb, a ~30s warmup phase, and an HTTP layer between hook and
   daemon.
3. **Replacement was trivial.** Weighted token overlap over the 13
   tripwire bodies gave strictly better coverage at 1% of the complexity.
   **130 lines of pure Python vs 350 lines + a daemon + an ONNX model.**

**Lesson:** measure before you build. A 20-minute TF-IDF prototype would
have answered the question "is rule-engine + keyword scoring enough?"
before half a day went into daemon plumbing. The delete commit is in
`CHANGELOG.md`.

This kind of thing stays documented on purpose. If you're evaluating
Cortex for your own project, you should see what we tried that didn't
work, not just the highlight reel.

---

## FAQ

### Why not a vector store?

Vector stores answer **"what's semantically similar to this query?"** — useful when you *already know to ask*. Cortex answers **"what should I be warned about right now?"** — the question the agent faces at task start, automatically, with no query.

They're different questions. Cortex sits **alongside** your vector store, not instead of it. See [docs/architecture.md](docs/architecture.md#cortex-vs-palace-separate-stores-by-design).

### Why not an LLM for classification?

Predictability. Rules are diffable in git. When a false positive shows up in the logs, you tighten the rule and re-test. An LLM classifier is a black box that hallucinates on some non-zero fraction of prompts — and you silently ship bad injections on exactly the cases you're least equipped to notice.

The TF-IDF fallback exists for when the rule engine misses. It's still deterministic. Still `grep`-able. Still fixable in a PR.

### What's the cost at hook time?

- Rule match: <1ms
- TF-IDF fallback: <1ms
- Synthesizer: <5ms
- Audit log append: <1ms
- Total `cortex-hook` round-trip: **<20ms** on a warm Python interpreter

The longest thing in the hook path is a single SQLite query. No network, no subprocess, no embeddings, no model load.

### Does it work on Linux / Mac / Windows?

Built and primarily tested on Windows. Cross-platform paths via `pathlib`. UTF-8 everywhere. Tests use `tmp_path` fixtures. CI matrix covers Ubuntu + Python 3.10 / 3.11 / 3.12. No Windows-only syscalls.

### Is it on PyPI?

Not yet. Install from this repo with `pip install -e .`. PyPI release when Day 7-8 features ship.

### Is this production-ready?

It's running live in one production project (the Polymarket research repo where it was born) on a shared Claude Code session with PostToolUse + UserPromptSubmit hooks active. 107 tests, ruff clean, fail-open on every error path. Use it with that level of caveat: it's alpha, it's maintained, and the author eats the dog food daily.

---

## Docs

| Document | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Why Cortex exists. Three failure modes. Design decisions. Rejected paths. Cortex vs Palace. |
| [docs/authoring.md](docs/authoring.md) | How to write tripwires, rules, cost components, synthesis rules, verifiers, violation patterns. Includes a "should this be a tripwire?" decision tree. |
| [docs/hooks.md](docs/hooks.md) | Claude Code hook contract. Environment variables. Manual testing. Five troubleshooting recipes. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Five ground rules. Dev setup. PR checklist. |
| [CHANGELOG.md](CHANGELOG.md) | Day 1-6 feature history with rejected-path post-mortems. |

---

## CLI at a glance

| Command | Purpose |
|---|---|
| `cortex init` | Create an empty store |
| `cortex migrate` | Seed tripwires from importers (idempotent) |
| `cortex list [--domain X] [--severity Y]` | Browse tripwires |
| `cortex show <id>` | Full detail of one tripwire |
| `cortex find w1,w2,w3` | Simulate a trigger match for given words |
| `cortex stats` | Store summary by severity and domain |
| `cortex stats --sessions [--days N]` | Session audit: rule hits, cold tripwires, silent violations, effectiveness |
| `cortex add ...` | Add a tripwire manually |
| `cortex import-palace "query"` | Smart search over Palace and emit tripwire draft templates |
| `cortex-hook` | `UserPromptSubmit` hook entry point |
| `cortex-watch` | `PostToolUse` audit hook entry point |
| `cortex-check-lookahead --features-dir DIR` | Standalone lookahead-bug verifier |

---

## About the seed tripwires

The 13 tripwires shipped in [`cortex/importers/memory_md.py`](cortex/importers/memory_md.py)
are **real lessons** from a Polymarket trading research project where
Cortex was born. They reference specific past failures — a $7.78 live bot
loss, a misread fee formula, a dead-zone trading hypothesis, a $57
single-strategy bleed, a $201 survivorship-biased paper test.

They're included as **concrete working examples of the tripwire format**,
not as rules for your project. Write your own. The examples are there so
you can see what "good" looks like. See [docs/authoring.md](docs/authoring.md)
for the full guide and a decision tree.

The rules under `cortex/rules/polymarket.yml` are likewise domain-specific.
`cortex/rules/generic.yml` has a few reusable ones (paper trading config,
backtest-vs-prod comparison, feature pipelines).

---

## Roadmap

**Day 7+** (not yet shipped):

- **Weekly DMN reflection loop** — cheap LLM (Haiku) processes session
  logs and proposes new tripwires to an inbox for human approval
- **Verifier auto-run from hook** — critical tripwires with `verify_cmd`
  run the command live during hook invocation; block on failure
- **Pattern authoring helper** — `cortex suggest-patterns <tripwire_id>`
  reads session logs and proposes regex candidates from observed
  violations the user retroactively marks
- **Inbox workflow** — `cortex inbox list / approve / reject` for
  DMN-proposed drafts
- **PyPI release**

---

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

*Built after a bot lost $7.78 because the agent didn't know what it
didn't know. If that's ever happened to you, Cortex is for you.*

</div>
