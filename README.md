# Cortex

[![tests](https://img.shields.io/badge/tests-107%20passing-brightgreen)](tests/)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)

**Active memory and executive control for AI coding agents.**

Vector memory stores (Palace, ChromaDB, RAG) are passive — the agent must
know to query them, and usually doesn't. Critical lessons decay or get
ignored even when they're already in the store. Cortex is a thin active
layer that intercepts the user prompt, classifies it against a curated
tripwire store, and injects the relevant lessons into the agent's working
context **before** the agent reasons about the task.

Built after a live trading bot lost $7.78 in 82 minutes on a lookahead
lesson that was documented minutes later but never loaded at decision time.

## 30-second overview

```
User prompt ──► Claude Code UserPromptSubmit hook ──► cortex-hook
                                                          │
                                                          ▼
                  ┌─────────────────────────────────────┐
                  │ 1. Classify   (YAML rule engine)    │
                  │ 2. Synthesize (sum cost components) │
                  │ 3. Fallback   (TF-IDF over bodies)  │
                  └─────────────────────────────────────┘
                                                          │
                                                          ▼
                  <cortex_brief> injected into agent context
```

Everything runs in under 20ms at hook time. Fail-open on every error path:
if anything breaks the hook exits silently and Claude Code proceeds as if
nothing happened.

## Install

```bash
git clone https://github.com/jimbokl/LLMCORTEX.git
cd cortex-agent
pip install -e ".[dev]"
```

Runtime dependency: `pyyaml` only. Everything else is stdlib.

## About the seed tripwires

The 13 tripwires shipped in [cortex/importers/memory_md.py](cortex/importers/memory_md.py)
are real lessons distilled from a Polymarket trading research project
where Cortex was born. They reference specific past failures (a $7.78 live
bot loss, a misread fee formula, a dead-zone trading hypothesis) and are
included as **concrete working examples of the tripwire format**, not as
rules you need to follow.

For your own project, either edit `memory_md.py` to replace the seeds with
your own lessons, or write a new importer under `cortex/importers/`. See
[docs/authoring.md](docs/authoring.md) for the full guide including a
"should this be a tripwire" decision tree.

The rules under `cortex/rules/polymarket.yml` are likewise domain-specific;
`cortex/rules/generic.yml` has a few reusable ones (paper-trading
configuration, backtest-vs-prod comparison, feature pipelines).

## Quickstart

```bash
# one-time setup inside your project
cortex init              # creates .cortex/store.db
cortex migrate           # seeds 11 tripwires distilled from BOTWA MEMORY.md
cortex stats             # sanity check

# wire into Claude Code (project root .claude/settings.json)
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

Done. Your next prompt containing keywords like `backtest`, `poly`, `5m`,
`directional`, `fee`, or `live deploy` will fire the hook and get a
structured brief injected into Claude Code's context automatically.

Test it without launching Claude Code:

```bash
echo '{"session_id":"test","prompt":"run a 5m poly directional backtest"}' \
  | cortex-hook \
  | python -m json.tool
```

## Architecture in one paragraph

Cortex has four subsystems over a single SQLite store:

1. **Classify** — YAML rule engine with tokenize-and-set-intersect matching
2. **Synthesize** — sums `cost_components` across matched tripwires and
   fires when cumulative drag crosses a threshold (the novel contribution)
3. **Fallback** — TF-IDF keyword scoring over tripwire bodies when the rule
   engine misses
4. **Verify** — standalone CLI verifiers (`cortex-check-lookahead`) that
   scan code for specific failure patterns

All four layers write to `.cortex/sessions/{session_id}.jsonl` as an audit
substrate for Day-5 DMN accounting.

See **[docs/architecture.md](docs/architecture.md)** for the three failure
modes that drove the design, the rejected Palace-daemon path, and the
complete data flow.

## CLI reference

| Command | Purpose |
|---|---|
| `cortex init` | Create an empty store |
| `cortex migrate` | Seed tripwires from importers (idempotent) |
| `cortex list [--domain X] [--severity Y]` | Browse tripwires |
| `cortex show <id>` | Full detail of one tripwire |
| `cortex find w1,w2,w3` | Simulate a trigger match for given words |
| `cortex stats` | Store summary by severity and domain |
| `cortex stats --sessions [--days N]` | Session audit analyzer: rule hits, cold tripwires, tool-call density |
| `cortex import-palace "query" [--n N] [--min-sim F]` | Search Palace, emit tripwire draft templates |
| `cortex add ...` | Manually add a tripwire (`cortex add --help`) |
| `cortex-hook` | `UserPromptSubmit` hook entry (stdin JSON → stdout JSON) |
| `cortex-watch` | `PostToolUse` audit hook |
| `cortex-check-lookahead --features-dir DIR` | Lookahead-bug verifier |

## Docs

- **[docs/architecture.md](docs/architecture.md)** — why Cortex, the 3
  failure modes, design decisions, rejected paths, Cortex vs Palace
- **[docs/authoring.md](docs/authoring.md)** — how to add tripwires, rules,
  cost components, synthesis rules, and verifiers
- **[docs/hooks.md](docs/hooks.md)** — Claude Code integration details,
  environment variables, troubleshooting
- **[CHANGELOG.md](CHANGELOG.md)** — version history

## Status (v0.1.0)

| Day | What shipped |
|---|---|
| 1 | SQLite store + CLI + 11 seed tripwires (19 tests) |
| 2 | YAML rule engine + `UserPromptSubmit` hook (+18 tests) |
| 3 | Synthesizer + code verifier + session audit + `PostToolUse` hook (+28 tests) |
| 4 | TF-IDF fallback over tripwire bodies (+14 tests, rejected Palace daemon) |
| 5 | Session audit analyzer (`cortex stats --sessions`) + `cortex import-palace` helper (+14 tests) |
| 6 | Silent violation detection via `violation_patterns` + enriched `cortex-watch` logging (+15 tests) |

**107 tests passing. ~1800 lines of Python. Zero runtime deps beyond stdlib + pyyaml.**

Day-7 roadmap: weekly Haiku DMN reflection loop proposing new tripwires to
an inbox, verifier auto-run from hook for critical tripwires, pattern
authoring helper from observed violations.

## License

MIT.
