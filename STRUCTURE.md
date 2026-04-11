# STRUCTURE.md — handoff for the next agent

This file is the 15-minute onboarding for anyone (human or LLM) who
inherits the Cortex repo cold. It covers what the project is, how the
code is laid out, what the invariants are, what NOT to touch, and
where the bodies are buried. Read this before editing anything.

If you only have 90 seconds, read these four points:

1. **Cortex is a Claude Code `UserPromptSubmit` hook.** It classifies
   the prompt, synthesizes matched lessons, and injects a
   `<cortex_brief>` block into the agent's additional context. That's
   the entire product in one sentence.
2. **Fail-open is non-negotiable.** Any error on the hook path must
   return exit code 0 and empty stdout. A broken Cortex never blocks
   the user's interaction. There are tests for every failure path;
   do not weaken them.
3. **Zero runtime dependencies beyond `pyyaml`.** Every additional
   import slows every user prompt via the 60 ms subprocess cost.
   Optional features go under `[project.optional-dependencies]` (see
   `[dmn]` for the Anthropic SDK pattern).
4. **The audit log is load-bearing.** `.cortex/sessions/*.jsonl` is
   where Cortex monitors itself and where `cortex reflect` learns
   what to propose next. Never reformat it without a migration plan.

---

## Table of contents

1. [What Cortex is](#what-cortex-is)
2. [Repo layout](#repo-layout)
3. [The 14 Python modules](#the-14-python-modules)
4. [Subsystem interaction (data flow)](#subsystem-interaction)
5. [Storage: SQLite schema + session log format](#storage)
6. [Environment variables](#environment-variables)
7. [CLI reference (fast lookup)](#cli-reference)
8. [Fail-open contract](#fail-open-contract)
9. [Testing](#testing)
10. [Rejected paths (do not re-attempt)](#rejected-paths)
11. [Day-by-day feature history](#day-by-day)
12. [Things that break hard if you change them](#things-that-break-hard)
13. [Where to find things](#where-to-find-things)
14. [Known limitations and current work](#known-limitations)

---

## What Cortex is

Cortex is a Python package (`cortex` on import, `llmcortex-agent` on
PyPI) that gives Claude Code agents active memory. It hooks the
`UserPromptSubmit` event, takes the user's prompt, and before the
agent reasons it:

1. **Classifies** the prompt against a curated store of *tripwires*
   (structured lessons distilled from past failures) using a
   rule-based YAML engine with `match_any` + `and_any` keyword sets.
2. **Synthesizes** matched tripwires via cost-component composition
   — the novel bit. Three separate warnings can compose into one
   blocking number (e.g. 2.4pp + 7.25pp + 10pp = 19.65pp of
   cumulative structural drag).
3. **Falls back** to TF-IDF keyword scoring over tripwire bodies when
   the rule engine returns zero matches. Carries roughly half the
   injection load in real usage (see Day 4 post-mortem).
4. **Verifies** by optionally running code-level `verify_cmd` checks
   (with a strict allow-list guard) via `CORTEX_VERIFY_ENABLE=1`, and
   can block a prompt with exit code 2 via `CORTEX_VERIFY_BLOCK=1`.
5. **Audits** every inject / fallback / tool_call / violation into
   `.cortex/sessions/<sid>.jsonl` for Day-6 silent-violation detection
   and Day-11 DMN reflection.
6. **Reflects** via Haiku 4.5 (`cortex reflect`) to propose new
   tripwires from observed session data into `.cortex/inbox/` drafts
   for human approval.

Cortex sits **alongside** a vector store (Palace, ChromaDB, RAG), not
instead of one. Vector stores answer "what is similar to this query?"
— useful when the agent knows to ask. Cortex answers "what should I
be warned about right now?" — the question the agent faces at task
start, automatically, with zero queries.

## Repo layout

```
CORTEX/
├── README.md                    ← marketing entry point, real audit numbers
├── STRUCTURE.md                 ← this file
├── CHANGELOG.md                 ← Day 1-13 feature history
├── CONTRIBUTING.md              ← ground rules, dev setup, PyPI release
├── BENCHMARKS.md                ← real latency / storage / brief-size data
├── LICENSE                      ← MIT
├── pyproject.toml               ← name=llmcortex-agent (NOT cortex-agent, taken)
├── .gitignore                   ← .cortex/, *.db, dist/, build/, *.egg-info/
│
├── .github/
│   └── workflows/
│       └── ci.yml               ← pytest + ruff on 3.10/3.11/3.12
│
├── cortex/                      ← Python package (import name stays "cortex")
│   ├── __init__.py              ← __version__ = "0.1.0"
│   ├── cli.py                   ← argparse CLI, all subcommands wired here
│   ├── store.py                 ← SQLite CRUD for tripwires, cost_components,
│   │                              synthesis_rules, violations
│   ├── classify.py              ← rule engine + render_brief + find_db walk-up
│   ├── synthesize.py            ← cost-component summation (novel contribution)
│   ├── tfidf_fallback.py        ← weighted token overlap when rules miss (Day 4)
│   ├── hook.py                  ← UserPromptSubmit entry (cortex-hook script)
│   ├── watch.py                 ← PostToolUse audit entry (cortex-watch)
│   ├── verify_runner.py         ← pre-flight verifier auto-run (Day 7/10)
│   ├── violation_detect.py      ← silent violation detection (Day 6)
│   ├── session.py               ← jsonl audit log helpers
│   ├── stats.py                 ← session analyzer (Day 5), anonymize (Day 13)
│   ├── bench.py                 ← cortex bench subsystem benchmarks
│   ├── inbox.py                 ← draft-tripwire JSON inbox (Day 8)
│   ├── suggest_patterns.py      ← auto-regex generator (Day 9)
│   ├── dmn.py                   ← Haiku reflection loop (Day 11)
│   │
│   ├── importers/
│   │   ├── __init__.py
│   │   └── memory_md.py         ← 13 seed tripwires + cost_components + 1 synthesis rule
│   │
│   ├── rules/
│   │   ├── __init__.py
│   │   ├── polymarket.yml       ← 7 domain rules (backtest, directional, deploy, ...)
│   │   └── generic.yml          ← 3 domain-agnostic rules (paper, prod-match, features)
│   │
│   └── verifiers/
│       ├── __init__.py
│       └── check_feature_lookahead.py  ← standalone grep for slot_ts=(ts//N)*N bug
│
├── tests/                       ← 232 pytest tests, ~4 seconds
│   └── test_*.py                ← one file per cortex/ module
│
└── docs/
    ├── architecture.md          ← why Cortex, three failure modes, design decisions
    ├── authoring.md             ← how to write tripwires / rules / patterns / verifiers
    ├── hooks.md                 ← Claude Code hook contract, env vars, troubleshooting
    └── blog/
        └── 2026-04-11-the-36x-ratio.md  ← long-form essay on Day 4 meta-case
```

## The 14 Python modules

| Module | Day | Purpose | Imports |
|---|---|---|---|
| `store.py` | 1 | SQLite schema + upsert CRUD | stdlib only |
| `importers/memory_md.py` | 1 | Seed tripwire definitions (13 tripwires, 3 cost components, 1 synthesis rule) | `store` |
| `cli.py` | 1-13 | argparse CLI, all subcommands | most other modules (lazy imports to keep startup fast) |
| `classify.py` | 2 | Rule engine, `find_db()` walk-up, `render_brief()` | `store`, `synthesize`, `tfidf_fallback`, `yaml` |
| `hook.py` | 2 | `cortex-hook` entry point, stdin JSON → stdout JSON | `classify`, `verify_runner`, `session` |
| `session.py` | 3 | `log_event()`, `read_session()`, `sessions_dir()` walk-up | stdlib only |
| `watch.py` | 3 | `cortex-watch` entry point, PostToolUse audit | `session`, `violation_detect` |
| `verifiers/check_feature_lookahead.py` | 3 | Standalone `slot_ts = (ts//N)*N` detector | stdlib only |
| `synthesize.py` | 3 | Cost-component summation, the novel bit | `store` |
| `tfidf_fallback.py` | 4 | Weighted token overlap over tripwire bodies | `store` |
| `stats.py` | 5+13 | Session analyzer, anonymize, timeline, primary/fallback ratio | `session`, `store` |
| `violation_detect.py` | 6 | Regex matching over tool_input after injections | `session`, `store`, `classify` |
| `verify_runner.py` | 7+10 | Pre-flight verifier execution with allow-list + blocking | stdlib (`subprocess`, `shlex`) |
| `inbox.py` | 8 | Draft-tripwire JSON inbox under `.cortex/inbox/` | stdlib only |
| `bench.py` | 8.5 | Latency + storage + brief-size benchmarks | `classify`, `synthesize`, `tfidf_fallback`, `store`, `stats`, `session` |
| `suggest_patterns.py` | 9 | Auto-regex from LCS of post-injection tool_inputs | `session`, `stats` |
| `dmn.py` | 11 | Haiku reflection loop, prompt builder, proposal parser | `stats`, `store`, `classify`, `inbox`; lazy `anthropic` |

**Rule of thumb**: the hook path (`cortex-hook`) only reaches
`classify.py` → `store.py` → `synthesize.py` → `tfidf_fallback.py` →
`verify_runner.py` → `session.py`. Everything else (`bench`,
`suggest_patterns`, `dmn`, `inbox`) is CLI-only and never runs at hook
time. This separation matters for latency.

## Subsystem interaction

```
                      User prompt (Claude Code)
                                 │
                                 ▼
                         cortex-hook (hook.py)
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │ classify_prompt()        │
                    │   tokenize               │
                    │   load rules from YAML   │
                    │   open SQLite store      │
                    │   match rules            │
                    │   fetch tripwires        │
                    │   call synthesize()      │──► synthesize.py
                    └──────────────────────────┘         │
                                 │                        └─► cost_components
                                 ▼                             sum over matched
                    (any tripwires matched?)                   fire rule if drag
                         │            │                        crosses threshold
                        YES           NO                        │
                         │            ▼                         ▼
                         │   ┌──────────────────┐     SYNTHESIS section
                         │   │ fallback_search  │     in <cortex_brief>
                         │   │ (tfidf_fallback) │
                         │   └──────────────────┘
                         │            │
                         └────────────┤
                                      ▼
                      ┌──────────────────────────┐
                      │ render_brief()           │
                      │ build <cortex_brief>     │
                      └──────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────┐
              │ run_verifiers_for() (Day 7/10)       │
              │   if CORTEX_VERIFY_ENABLE=1          │
              │   shlex.split + subprocess.run       │
              │   allow-list prefix guard            │
              │   3s hard timeout                    │
              └──────────────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────┐
              │ log_event(session_id, "inject", {    │
              │   matched_rules, tripwire_ids,       │
              │   synthesis_ids, verifier_ids,       │
              │   blocked: bool                      │
              │ })                                   │
              └──────────────────────────────────────┘
                                 │
                                 ▼
            JSON output to Claude Code via hookSpecificOutput.additionalContext
              + optional exit 2 if CORTEX_VERIFY_BLOCK=1 and verifier failed


              Separately on every tool call:

                       agent tool call
                              │
                              ▼
                       cortex-watch (watch.py)
                              │
                              ▼
              ┌──────────────────────────────┐
              │ summarize_tool_input()       │
              │ log_event "tool_call"        │
              │ detect_violations() (Day 6)  │
              │   if active tripwire         │
              │   regex matches tool_input   │
              │   log "potential_violation"  │
              └──────────────────────────────┘
```

## Storage

### SQLite store: `.cortex/store.db`

Schema versioning via the `schema_version` table. Migrations are
idempotent DDL in `store.py::_SCHEMA` plus `_migrate_schema()` for
`ALTER TABLE ADD COLUMN` deltas.

```sql
CREATE TABLE tripwires (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK(severity IN ('critical','high','medium','low')),
    domain              TEXT NOT NULL,
    triggers            TEXT NOT NULL,        -- JSON array
    body                TEXT NOT NULL,
    verify_cmd          TEXT,
    cost_usd            REAL NOT NULL DEFAULT 0,
    born_at             TEXT NOT NULL,
    last_violated_at    TEXT,
    violation_count     INTEGER NOT NULL DEFAULT 0,
    source_file         TEXT,
    violation_patterns  TEXT                  -- JSON array of regex strings (Day 6)
);

CREATE TABLE cost_components (
    id           TEXT PRIMARY KEY,
    tripwire_id  TEXT NOT NULL REFERENCES tripwires(id) ON DELETE CASCADE,
    metric       TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT NOT NULL,               -- e.g. "pp", "pct", "bp"
    sign         TEXT NOT NULL CHECK(sign IN ('drag','boost'))
);

CREATE TABLE synthesis_rules (
    id           TEXT PRIMARY KEY,
    triggers     TEXT NOT NULL,               -- JSON array
    sum_over     TEXT NOT NULL,               -- JSON array of cost_component ids
    threshold    REAL NOT NULL,
    op           TEXT NOT NULL CHECK(op IN ('gte','lte','gt','lt')),
    message      TEXT NOT NULL
);

CREATE TABLE violations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tripwire_id  TEXT NOT NULL REFERENCES tripwires(id) ON DELETE CASCADE,
    session_id   TEXT,
    at           TEXT NOT NULL,
    evidence     TEXT
);
```

**Upsert contract**: `add_tripwire()` uses `INSERT ... ON CONFLICT(id)
DO UPDATE SET` which preserves `born_at`, `last_violated_at`, and
`violation_count` across re-migrations. Tests in `test_store.py`
guard this behaviour. Do not drop the preserved columns from the
`DO UPDATE SET` clause.

### Session audit log: `.cortex/sessions/<session_id>.jsonl`

One jsonl file per Claude Code session, append-only. Event types:

- `inject` — primary rule-engine match, includes `matched_rules`,
  `tripwire_ids`, `synthesis_ids`, `verifier_ids`, `blocked` fields
- `keyword_fallback` — TF-IDF fallback fired, includes `n_hits`,
  `tripwire_ids`, `scores`
- `tool_call` — any PostToolUse event, includes `tool_name` and
  `input_snippet` (≤500 chars, see `summarize_tool_input()`)
- `potential_violation` — regex match on tool_input after injection,
  includes `tripwire_id`, `tool_name`, `pattern`, `snippet`
- `verifier_blocked` — `CORTEX_VERIFY_BLOCK=1` caused exit code 2,
  includes `failed_tripwires`

Every event has `at` (ISO timestamp) and `event` (type) fields at
minimum. The log is the ground truth for Day 5 (`cortex stats
--sessions`), Day 6 (violation detection), Day 9 (pattern suggest),
Day 11 (DMN reflection), and Day 13 (timeline rendering).

**Log rotation is NOT implemented.** Sessions stay forever until the
user deletes the directory. Plan for a `cortex sessions prune --days
N` command if this becomes a problem.

### Draft inbox: `.cortex/inbox/<draft_id>.json`

One JSON file per pending tripwire draft. Schema:

```json
{
  "draft_id": "palace_polymarket_20260411T171820_a7f2c1",
  "created_at": "2026-04-11T17:18:20+00:00",
  "source": "palace_polymarket" | "dmn_haiku" | "manual",
  "draft": {
    "id": "TODO_or_real_id",
    "title": "...",
    "severity": "critical|high|medium|low",
    "domain": "...",
    "triggers": [...],
    "body": "...",
    "verify_cmd": null,
    "cost_usd": 0.0,
    "source_file": "...",
    "violation_patterns": []
  }
}
```

Validation happens at approval time (`cortex inbox approve`), not at
write time — drafts can carry TODO placeholders that a human will
fill in. The `--force` flag bypasses TODO checks; prefer editing the
draft JSON directly.

## Environment variables

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `CORTEX_DB` | walk-up from CWD | `classify.py::find_db` | Absolute path to SQLite store |
| `CORTEX_SESSIONS_DIR` | walk-up from CWD + `.cortex/sessions` | `session.py::sessions_dir` | Override session log directory |
| `CORTEX_INBOX_DIR` | walk-up + `.cortex/inbox` | `inbox.py::inbox_dir` | Override inbox directory |
| `CORTEX_PALACE_PATH` | (unset, required for `cortex import-palace`) | `cli.py::cmd_import_palace` | Path to Palace chromadb dir |
| `CORTEX_PALACE_WING` | `polymarket` | `cli.py::cmd_import_palace` | Palace wing name |
| `CORTEX_VERIFY_ENABLE` | (unset) | `verify_runner.py::_enabled` | Set to `1` to run verifiers at hook time |
| `CORTEX_VERIFY_BLOCK` | (unset) | `hook.py::_verify_block_enabled` | Set to `1` to exit code 2 on critical verifier fail |
| `CORTEX_VERIFY_TIMEOUT` | `3` (seconds) | `verify_runner.py::_timeout` | Hard timeout per verifier command |
| `CORTEX_VERIFY_PREFIXES` | `cortex-,python -m cortex` | `verify_runner.py::_prefixes` | Comma-separated allow-list |
| `CORTEX_VERIFY_ALLOW_ANY` | (unset) | `verify_runner.py::_allow_any` | DANGER — disable allow-list entirely |
| `ANTHROPIC_API_KEY` | (unset) | `dmn.py::call_haiku` | Required for `cortex reflect` |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD` | (unset) | CI + local test runs | Workaround for a global `anchorpy` pytest plugin that fails import |

**All walk-up helpers start from `Path.cwd()` and climb parents looking
for a `.cortex/` directory.** This means `cortex-hook` works from any
subdirectory of a project as long as `.cortex/` exists at the root.

## CLI reference

Detailed in `README.md` and `docs/hooks.md`. Quick lookup:

| Command | Reads | Writes | Notes |
|---|---|---|---|
| `cortex init` | — | store.db schema | Idempotent |
| `cortex migrate` | importers | store.db | UPSERT, preserves violation stats |
| `cortex list` | store | — | Supports `--domain`, `--severity` |
| `cortex show <id>` | store | — | Shows violation_patterns if present |
| `cortex find w1,w2,w3` | store | — | Simulates trigger match |
| `cortex stats` | store | — | Store summary |
| `cortex stats --sessions [--days N] [--anonymize]` | store + session logs | — | Primary-vs-fallback ratio, effectiveness rates, cold tripwires |
| `cortex timeline <sid> [--anonymize]` | session log | — | ASCII event timeline |
| `cortex add` | store | store | Manual tripwire add |
| `cortex import-palace "query" [--to-inbox]` | Palace mempalace + (optional) inbox | inbox if `--to-inbox` | Requires `CORTEX_PALACE_PATH` |
| `cortex inbox list/show/approve/reject` | inbox | inbox + store on approve | Validates before promotion |
| `cortex bench [--json]` | store + session dir | — | Latency + storage + brief size |
| `cortex suggest-patterns <id> [--fix-example "..."]` | session logs | — | Auto-regex via LCS |
| `cortex reflect [--days N] [--dry-run]` | session logs + store | inbox | Requires `ANTHROPIC_API_KEY` and `[dmn]` extra |
| `cortex-hook` | classifier + store + session log | session log, stdout | Entry point for UserPromptSubmit |
| `cortex-watch` | session log | session log | Entry point for PostToolUse |
| `cortex-check-lookahead --features-dir DIR` | directory tree | — | Standalone grep; exit 0/1 |

## Fail-open contract

This is the most important invariant in the codebase. It is tested in
`test_hook.py` and `test_watch.py` via `_run_hook()` / `_run_watch()`
helpers that mock stdin. Any change that violates this contract must
be rejected at PR review.

**The rule**: any error reachable from `cortex-hook` or `cortex-watch`
must exit code 0 with empty stdout. The hook never blocks the user's
interaction.

**Specific failure paths tested**:

- Empty stdin → exit 0, no output
- JSON parse fails → exit 0, no output
- Missing `prompt` field → exit 0, no output
- `classify_prompt()` raises → caught, exit 0
- `run_verifiers_for()` raises → `verifier_results = []`, continues normally
- `log_event()` raises → caught in the inner try/except, hook path continues
- `render_brief()` raises → outer exception handler, exit 0
- Store missing / locked / corrupt → `classify_prompt()` returns empty, exit 0
- `tfidf_fallback.fallback_search()` raises → `brief = ""`, exit 0

**The one documented exception** is Day 10's `CORTEX_VERIFY_BLOCK=1`:
when a critical verifier reports `passed=False` AND the env var is set,
the hook exits code 2. This is the ONLY case where `cortex-hook` can
exit non-zero. The brief is still emitted before the exit so the agent
sees the FAIL marker. See `hook.py::main` and `test_hook.py` for the
controlled test cases.

## Testing

- **Total tests**: 232 passing in ~4 seconds.
- **Runner**: pytest. All tests use `tmp_path` and `monkeypatch` so they
  are hermetic (no dependency on the live `.cortex/` directory).
- **Plugin-autoload disabled**: always run with
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` locally and in CI because there
  is a global `anchorpy` plugin on some machines that fails import.
- **One test file per cortex/ module**: `test_store.py`,
  `test_classify.py`, `test_synthesize.py`, etc. Plus
  `test_suggest_patterns.py` + `test_suggest_patterns_regex.py` for
  the two halves of that module.
- **Dependency injection for external services**: `call_haiku()` takes
  a `client=None` parameter, `run_verifier()` can be monkey-patched,
  `classify_prompt()` takes `db_path` and `rules_dir`. Use these
  hooks for mocks; do not rely on fake filesystem mounts.
- **Real-data smoke tests are NOT in the suite**. They live in the
  README and in individual Day-X commit messages. If you change core
  behaviour, rerun the real-data smoke tests from the README
  ("Wire it into Claude Code" section) before shipping.

## Rejected paths

Three documented rejections in `CHANGELOG.md`. Do not re-attempt these
without reading the post-mortems first:

1. **Day 4 — Palace semantic daemon.** Built and killed same day.
   350 LOC of HTTP daemon + ONNX embedding model + warmup phase,
   replaced by a 130-line TF-IDF function (`tfidf_fallback.py`) that
   gave strictly better coverage. **Lesson**: semantic search over
   frozen curated bodies loses to weighted token overlap when the
   bodies are small and domain-specific.
2. **Day 10 — `cortex serve` warm daemon.** Would drop subprocess
   cost from 59 ms to <5 ms, saving ~10 seconds per day of real work.
   Rejected because 59 ms is below human perception and daemon
   lifecycle complexity (crash recovery, health checks, port
   conflicts, upgrades) exceeds the savings. **Lesson**: measure
   before you build.
3. **Day 9 — auto-regex without `--fix-example`.** The first version
   of `cortex suggest-patterns` emitted regex candidates without a
   way to check if they also matched the fix pattern. Overfits
   catastrophically. Added `--fix-example` as a mandatory quality
   gate. **Lesson**: any auto-generator for regex / rules / prompts
   needs a negative-example sanity check.

If you find yourself about to re-build one of these, read the
post-mortem first. If you still want to build it, write a new
rejected-path post-mortem explaining why the conditions changed.

## Day-by-day

Full history lives in `CHANGELOG.md`. The one-liner version:

| Day | Shipped |
|---|---|
| 1 | SQLite store + CLI + 11 seed tripwires |
| 2 | YAML rule engine + `UserPromptSubmit` hook + `find_db()` walk-up |
| 3 | Synthesizer + code verifier + session audit + `PostToolUse` hook |
| 4 | TF-IDF fallback (rejected Palace daemon) |
| 5 | Session audit analyzer + `cortex import-palace` helper |
| 6 | Silent violation detection + per-tripwire effectiveness rate |
| 7 | Pre-flight verifier auto-run from hook |
| 8 | Inbox workflow (`cortex inbox list/show/approve/reject`) |
| 8.5 | `cortex bench` + BENCHMARKS.md |
| 9 | Auto-regex pattern-suggest helper with `--fix-example` safety gate |
| 10 | Verifier blocking mode (`CORTEX_VERIFY_BLOCK=1` → exit 2) |
| 11 | Haiku DMN reflection loop (`cortex reflect`) |
| 12 | PyPI release prep + `llmcortex-agent` rename + `twine upload` |
| 13 | `cortex stats --anonymize` + `cortex timeline` + README production section + blog post + DMN prompt enrichment |

## Things that break hard

These are load-bearing choices. Touch them and tests will fail fast,
OR (worse) production will silently misbehave. Grep the repo for the
keyword before you change behaviour.

1. **`find_db()` / `sessions_dir()` walk-up semantics.** Every hook
   invocation resolves `.cortex/store.db` by walking up parents from
   CWD. If you change the walk-up (e.g. to prefer a config file), you
   break users running Cortex from subdirectories.
2. **UPSERT `ON CONFLICT DO UPDATE SET` clause.** The SET clause
   intentionally omits `born_at`, `violation_count`, and
   `last_violated_at` so re-migrations don't clobber accumulated
   stats. `test_store.py::test_upsert_preserves_violation_stats`
   guards this.
3. **`_tokenize()` regex `[a-z0-9_\-]+`.** This is the prompt
   tokenizer for the rule engine. Cyrillic is intentionally dropped
   so mixed Russian+English prompts still match on English domain
   keywords. Changing it to `\w+` (Unicode) silently breaks Russian
   users who don't realize their prompts were falling through to
   fallback anyway.
4. **Lookahead verifier's `+ <digit/word>` detection**. The honest
   forward-shift fix pattern is `(ts // N) * N + N`, distinguished
   from the bug `(ts // N) * N` by the trailing `+`. The verifier
   uses `[^\+]*?$` logic, not a simple regex. See
   `verifiers/check_feature_lookahead.py::_detect_lookahead`. Changing
   this to a pure regex reintroduces the false-positive cascade
   from Day 3.
5. **Violation pattern for `lookahead_parquet`**. The regex
   `slot_ts[^\n]*?=[^\n]*?//\s*\d+[^\n]*?\*\s*\d+\b(?!\s*\+)` needs
   the trailing `\b` before `(?!\s*\+)` to prevent backtracking into
   a single-digit match that hides the `+ 300`. Dropping `\b` silently
   breaks the fix-pattern exclusion.
6. **`auto-generated` draft_ids use uuid6 suffix**. Multiple drafts
   staged in the same second would otherwise collide and overwrite.
   `inbox.py::write_draft` generates
   `{source}_{timestamp}_{uuid.uuid4().hex[:6]}` for auto-ids. Do not
   simplify back to timestamp-only.
7. **The import-name vs distribution-name split**. PyPI name is
   `llmcortex-agent`; Python import name is `cortex`. This is called
   out in `pyproject.toml`, `README.md`, and `CONTRIBUTING.md`.
   Renaming either one breaks existing users. If you must rename the
   distribution (e.g., for trademark reasons), keep `cortex` as the
   import name and add a PyPI metadata redirect.
8. **Fail-open contract, covered above.** Non-negotiable.

## Where to find things

- **The novel bit (synthesizer)**: `cortex/synthesize.py` and
  `docs/architecture.md#synthesize`. The 19.65pp example is the
  canonical demo.
- **The production audit data**: run `cortex stats --sessions
  --anonymize` on the user's machine. Real numbers in `README.md`'s
  "In production" section.
- **The rejected Palace daemon story**: `CHANGELOG.md` under Day 4,
  plus `docs/architecture.md`'s "Rejected path" section.
- **The blog post narrative**: `docs/blog/2026-04-11-the-36x-ratio.md`.
- **Why tripwires must be earned**: `CONTRIBUTING.md` ground rule #1
  and `docs/authoring.md`'s decision tree.
- **How to add a new subsystem**: read `bench.py` + `dmn.py` as two
  examples of "module + CLI subcommand + tests + docs" in the same
  commit.
- **How to test a hook change locally**: `README.md`'s "Wire it into
  Claude Code" section plus the `echo '{"prompt":"..."}' | cortex-hook
  | python -m json.tool` one-liner.
- **How to publish a release**: `CONTRIBUTING.md` "Publishing to PyPI"
  section.

## Known limitations

Documented honestly in `CHANGELOG.md`, `BENCHMARKS.md`, and
`README.md`'s "Honest about rejected paths" and "What Cortex is NOT"
sections.

- **Silent violation detection covers only 2 of 13 seed tripwires.**
  The rest need `violation_patterns` authored. `cortex suggest-patterns`
  helps but needs ≥2 weeks of real session data to produce
  high-confidence candidates for most tripwires.
- **Counterfactual prevention is unprovable.** We can measure what
  gets injected. We cannot measure what would have been a mistake
  without the injection.
- **The rule engine is English-biased.** Cyrillic tokens are dropped
  by `[a-z0-9_\-]+`. Mixed prompts work if any English keyword
  survives; pure Russian prompts stay silent.
- **Session logs grow forever.** No rotation yet.
- **`cortex reflect` is untested against the live API in CI.**
  Requires `ANTHROPIC_API_KEY`, mock-tested end-to-end, live
  dry-runs verified. Real submissions are one env var away.
- **`cortex stats --sessions` doesn't filter test sessions by
  default.** Bench + smoke + demo session IDs show up in the totals.
  A `--exclude-test-sessions` flag is on the roadmap.
- **No `cortex serve` warm daemon.** Documented rejection — the 59 ms
  subprocess cost is below human perception. If a future high-throughput
  use case emerges, this may be revisited.
- **No multi-project store federation.** Each `.cortex/store.db` is
  per-project. Cross-project queries are roadmap-only.

---

## If you change Cortex, test these three things before shipping

1. **Fail-open**: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_hook.py tests/test_watch.py -v`
2. **Real hook smoke**: `echo '{"prompt":"run a 5m poly directional backtest"}' | cortex-hook | python -m json.tool` — should print a brief with synthesis
3. **Audit-log shape**: `echo '{"session_id":"smoke","tool_name":"Bash","tool_input":{"command":"echo test"}}' | cortex-watch && cat .cortex/sessions/smoke.jsonl`

If all three pass, your change probably hasn't broken the core
product. Run `pytest -q` to confirm.

---

*Last updated: 2026-04-11. Keep this file in sync with the
CHANGELOG.md and the module inventory in `cortex/`. If you add a new
subsystem, add it to the table in [The 14 Python modules](#the-14-python-modules)
and to [Day-by-day](#day-by-day).*
