# Cortex changelog

## 0.1.0 — unreleased

Initial working version shipped over 4 iteration days.

### Day 1 — store + CLI

- SQLite schema with `tripwires`, `cost_components`, `synthesis_rules`,
  `violations`, `schema_version` tables
- Foreign keys on + WAL journal mode + idempotent UPSERT preserving
  violation stats across re-migrations
- CLI: `init`, `migrate`, `list`, `show`, `find`, `stats`, `add`
- Importer with 11 seed tripwires distilled from BOTWA `MEMORY.md`
- 19 tests

### Day 2 — classifier + hook

- YAML rule engine (`cortex/rules/*.yml`) with `match_any` / `and_any`
  semantics
- `classify.py` with `<20ms` tokenize-and-set-intersect matching
- `cortex-hook` entry point for Claude Code `UserPromptSubmit`
- `find_db()` walks up from CWD so the hook works from any subdirectory
  of a project that has `.cortex/` at its root
- Fail-open contract on every error path
- +18 tests

### Day 3 — synthesizer + verifier + audit

- `synthesize.py` with cost-component summation and threshold firing
- Seeded 3 cost components (`spread_slip 2.4pp`, `info_decay_5min 7.25pp`,
  `adverse_selection 10.0pp`) + 1 synthesis rule (`pm_5m_directional_block`)
- `verifiers/check_feature_lookahead.py` with honest-shift detection:
  ignores `(ts // N) * N + N` as the fix pattern, flags bare
  `(ts // N) * N` as the bug
- `session.py` + `cortex-watch` entry for `PostToolUse` audit logging
- Hook extended to log `inject` events with matched rules, tripwire ids,
  and synthesis ids
- +28 tests (store delta: 65 total)

### Day 4 — TF-IDF fallback

**Rejected path**: Palace semantic daemon via `mempalace.searcher` over
HTTP. Built Day 4 morning, killed Day 4 afternoon. English-only embedding
model scored 0 hits on short or Russian queries; daemon infrastructure
cost (~200MB warm ONNX + chromadb + HTTP layer) was not justified.

**Shipped**: `tfidf_fallback.py` with weighted token overlap over tripwire
`triggers` / `title` / `body`. Works on mixed Russian+English prompts
because the Latin-alpha regex silently drops Cyrillic while still matching
English domain terms.

- Hook falls through to TF-IDF when the rule engine returns 0 matches
- New `keyword_fallback` audit event type for DMN accounting
- Fallback brief marked `source="keyword_fallback"` and more compact than
  the primary brief, so the agent can distinguish advisory from authoritative
- +14 tests (78 total)

### Day 5 — session audit analyzer + Palace import helper

**`cortex stats --sessions [--days N]`** — passive DMN accounting over the
`.cortex/sessions/*.jsonl` substrate built in Day 3. Reports:
- Sessions total, with inject, with fallback (injection rate)
- Top matched rules / tripwires / synthesis rules
- Tool-call distribution per session
- Cold tripwires (never matched in window — candidates for tuning/removal)

First real-data run on this conversation surfaced that the rule engine
fires on only 36% of sessions while keyword fallback fires on 64% — an
actionable signal that rules are too narrow and need widening. Also
identified 2 cold tripwires in the current store.

**`cortex import-palace "query" [--n N] [--min-sim F]`** — smart-search
helper that queries Palace (`mempalace.searcher`) and emits tripwire draft
templates the user can review and paste into `cortex/importers/memory_md.py`.
Palace stays authoritative for broad semantic recall; Cortex stays
authoritative for active injection; the human-in-the-loop approval step
is intentional (automatic drawer → tripwire promotion would dilute signal).

**Chore**: `main()` now force-reconfigures stdout to UTF-8 on Windows
consoles so non-ASCII content (Cyrillic, Unicode symbols in tripwire
bodies) doesn't crash the CLI with cp1251 encoding errors.

- `cortex/stats.py` — session aggregation module
- Extended `cortex/cli.py` with `--sessions` flag and `import-palace` subcommand
- +14 tests (92 total)

### Day 6 — silent violation detection

**The "is anyone applying these lessons?" question finally gets a data answer.**

- **Schema delta**: idempotent `ALTER TABLE tripwires ADD COLUMN
  violation_patterns TEXT` via `_migrate_schema()`. Old stores get the column
  on next open; new stores get it in `CREATE TABLE`. Upsert preserves it.
- **`cortex/violation_detect.py`**: reads session audit log for active
  tripwires (injected or fallback-matched earlier in the same session),
  compiles their `violation_patterns` regexes, matches against tool_input.
  One violation per tripwire per tool call. Fail-safe on regex errors.
- **Enriched `cortex-watch`**: now captures `tool_input` via
  `summarize_tool_input()` (500-char max), emits tool-specific snippets
  (Bash=command, Edit=file+old+new diff, Read=path only). Runs detection
  after logging, emits `potential_violation` events when patterns match.
- **Seed 2 violation patterns**:
  - `lookahead_parquet`: detects `slot_ts = ... // N * N` (no forward shift)
    -- same pattern as the static `check_feature_lookahead` verifier, but
    runtime. The `\b` anchor after `\d+` prevents backtracking from hiding
    the subsequent `+ N` from the negative lookahead.
  - `real_entry_price`: detects `entry = 0.5` or `up_ask = 0.5` hardcodes.
- **`cortex stats --sessions` effectiveness report**: new section shows
  per-tripwire `hits / violations / rate` with OK/WARN/FAIL status flags.
  Rate near 0 = lesson applied, rate > 0.5 = lesson ignored (signal to
  improve brief formatting or add blocking).
- **`cortex show` displays patterns**: violation regexes rendered in tripwire
  detail view for transparency.
- **First live violation caught**: smoke test injected `lookahead_parquet`,
  then submitted an Edit with `df['slot_ts'] = (df['ts'] // 300) * 300`,
  then submitted a fix with `+ 300`. Bug pattern fired `potential_violation`,
  fix pattern stayed silent. **Effectiveness rate 0.17 for lookahead_parquet.**
- +15 tests (107 total)

### Day 7 — pre-flight verifier auto-run from hook

**Static warnings become "your current code has this bug right now".**

- **`cortex/verify_runner.py`**: runs `verify_cmd` for matched critical
  tripwires during `cortex-hook` invocation and appends the results to the
  injected brief. Safe-by-default:
  - **Opt-in**: nothing runs unless `CORTEX_VERIFY_ENABLE=1` is set
  - **Critical-only**: tripwires with severity `high` / `medium` / `low`
    are skipped even when enabled (noise control)
  - **Allow-list prefix**: commands must start with `cortex-` or
    `python -m cortex` by default, overridable via
    `CORTEX_VERIFY_PREFIXES="prefix1,prefix2,..."`
  - **DANGER override**: `CORTEX_VERIFY_ALLOW_ANY=1` disables the allow-list
    entirely — documented but not recommended
  - **Hard timeout**: `CORTEX_VERIFY_TIMEOUT` (default 3s)
  - **No shell**: commands parsed with `shlex.split`, run with `shell=False`
  - **Captured output truncated**: stdout 500 chars, stderr 200 chars
  - **Fail-safe**: timeout, OSError, parse error → `skipped` marker, no crash
- **Hook integration**: `cortex-hook` calls `run_verifiers_for()` after
  classification; results appear at the top of the brief right after the
  synthesizer block. `inject` events log `verifier_ids` for audit.
- **Brief rendering**: `[OK]` / `[FAIL]` / `[SKIP]` per tripwire with a
  "VERIFIER FAILED: the bug is PRESENT in your current code. Fix before
  proceeding" footer when any check fails.
- **Seed update**: `lookahead_parquet` gets
  `verify_cmd: "cortex-check-lookahead --features-dir DETECTOR"` as a
  working Day 7 example. Gracefully no-ops when `DETECTOR/` doesn't exist
  in the current working directory.
- **Live smoke test**: with `CORTEX_VERIFY_ENABLE=1`, matching a Polymarket
  backtest prompt against the BOTWA POLY project correctly SKIPs
  `poly_fee_empirical` (its `verify_cmd` starts with `BOT/` — not
  allow-listed, because it would execute a real trade) and runs
  `cortex-check-lookahead` against the live `POLY/DETECTOR/` folder,
  producing `[OK] lookahead_parquet — OK: scanned DETECTOR, 0 lookahead
  patterns found`. Allow-list saved a destructive command from auto-execution.
- +20 tests (127 total)

### Test coverage

```
127 passed in ~1.9s
```

Modules covered: `store`, `importers/memory_md`, `classify`, `hook`,
`synthesize`, `verifiers/check_feature_lookahead`, `session`, `watch`,
`tfidf_fallback`, `stats`, `violation_detect`, `verify_runner`.

### Day 9 — auto-regex pattern-suggest helper

**`cortex suggest-patterns <tripwire_id> [--fix-example "..."]`** reads
session logs for past injections of a tripwire, extracts the `tool_call`
events that followed, and **auto-generates regex candidates** for
`violation_patterns`. No more hand-writing regexes while staring at
snippets.

- **`cortex/suggest_patterns.py`**: 380-line module. Core primitives:
  - `collect_post_injection_snippets(tripwire_id, window)` — reads all
    session logs, finds `inject` / `keyword_fallback` events for the
    given tripwire, collects the next `window` `tool_call` events per
    injection
  - `analyze_snippets(findings)` — aggregates by tool_name, extracts
    common identifiers, builds `snippets_by_tool` dict
  - `_longest_common_substring(a, b)` — classic O(m*n) DP
  - `_lcs_across(snippets)` — iterative pair-wise LCS over N snippets
  - `_generalize_to_regex(text)` — escape metacharacters, then replace
    runs of spaces with `\\s*` and runs of digits with `\\d+`
  - `generate_regex_candidate(snippets, fix_example)` — LCS + generalize,
    verify the regex actually matches all input snippets (if the
    generalization broke matching, fall back to plain escaped anchor),
    score confidence (HIGH/MEDIUM/LOW based on anchor length + match
    count), optionally verify the regex does NOT match a known fix
    example and downgrade confidence to LOW if it does
  - `generate_regex_candidates(analysis, fix_example)` — produce one
    global candidate and one per-tool candidate (for tools with >=2
    snippets)
- **`--fix-example "..."` flag**: lets the user supply a known-fix
  string. The tool verifies each candidate does NOT match the fix; any
  candidate that does match gets marked `[LOW CONFIDENCE]` with a
  `fix: MATCHES the given fix example — too broad, narrow manually`
  footnote.
- **`render_suggestions()` output** leads with `Auto-generated regex
  candidates` section (confidence tag, anchor, regex, match count, fix
  verification), followed by the raw tool call distribution + snippet
  dump + common identifiers for human inspection.
- **Live smoke on real session logs**: found 16 past injections of
  `lookahead_parquet`, generated a `[HIGH]` Edit candidate containing
  the exact `(df['ts'] // 300) * 300` pattern from Day 6 smoke tests.
  When re-run with `--fix-example "...// 300) * 300 + 300"`, the
  candidate was correctly downgraded to `[LOW]` with the "too broad"
  warning. End-to-end workflow verified.
- +31 tests (225 total). `test_suggest_patterns.py` covers the
  session-scan + analyze path; `test_suggest_patterns_regex.py`
  covers LCS + generalize + candidate generation + fix-example logic.

**Why this matters**: after a week of real Cortex usage, the user's
session logs contain hundreds of real `tool_input` snippets tied to
tripwire injections. Instead of writing regexes by hand, the user now
runs `cortex suggest-patterns <id>`, gets a concrete candidate ready to
paste into `cortex/importers/memory_md.py`, verifies it against a known
fix with one flag, and ships. The Day 6 silent violation detection
machinery now has a data-driven authoring path.

### Day 8.5 — benchmark suite

**Measured answers instead of hand-waved claims.**

- **`cortex/bench.py`**: `run_benchmarks()` runs per-subsystem latency
  benchmarks (tokenize / classify_prompt / fallback_search / synthesize
  / render_brief) using `time.perf_counter` with warmup + N iterations,
  measures storage footprint (SQLite size + row counts by section),
  session log footprint (file count + total bytes), brief size
  distribution across 10 canned test prompts, and end-to-end
  `cortex-hook` subprocess latency via `shutil.which` + `subprocess.run`.
  Plus a break-even token impact analysis.
- **`cortex bench`** CLI: `--iterations N`, `--no-subprocess`, `--json`.
- **[BENCHMARKS.md](BENCHMARKS.md)**: full report with real numbers from
  the live BOTWA store. Headline: `classify_prompt` p50 = 6.3 ms,
  end-to-end hook subprocess p50 = 59.3 ms, avg brief ≈ 1250 tokens,
  break-even at 1 prevented mistake per 2 injections.
- **README FAQ updated** to cite measured numbers (was: hand-waved
  `<20 ms` claim; now: specific percentiles from the bench output).
- +11 tests for `bench.py` structure (not regression-sensitive on
  actual numbers — tests verify shape, not performance).

### Day 8 — inbox workflow

**The human-approval step between "I found something" and "it's in the store".**

- **`cortex/inbox.py`**: draft tripwire store as JSON files under
  `.cortex/inbox/*.json`. Walk-up resolution like session logs; honors
  `CORTEX_INBOX_DIR` env var. All I/O is fail-safe (exceptions swallowed).
- **CLI**: four new subcommands under `cortex inbox`:
  - `list` — show all pending drafts with validation status
    (READY / TODO: fields / MISSING: fields)
  - `show <draft_id>` — display one draft with full JSON contents
  - `approve <draft_id> [--force]` — validate required fields, check for
    TODO placeholders, promote to the tripwire store via
    `store.add_tripwire()`, delete the draft on success. `--force`
    bypasses the TODO check for advanced users.
  - `reject <draft_id>` — delete the draft without promoting
- **`cortex import-palace --to-inbox`**: the existing Palace smart-search
  now has an opt-in flag to stage hits as draft JSON files in the inbox
  instead of printing copy-paste templates to stdout. Drafts have uuid-6
  suffixes on auto-generated ids to avoid collisions when multiple hits
  land in the same second.
- **`validate_draft()`**: reusable helper that returns
  `(missing_fields, todo_fields)` tuples. Ships as a library primitive
  for Day-9 DMN reflection loops and for external callers.
- **`draft_to_tripwire_kwargs()`**: filters a draft dict down to the
  fields accepted by `store.add_tripwire()`, silently dropping unknown
  keys so drafts can carry extra metadata (Palace similarity scores,
  provenance markers) without breaking promotion.
- +19 tests (165 total)

**Why this matters:** Cortex can now close the Palace-to-Cortex knowledge
transfer loop with a human in the loop. Without inbox, surfacing a Palace
drawer meant opening `cortex/importers/memory_md.py` in an editor,
pasting a Python dict, running `cortex migrate`. With inbox, it's a
three-step CLI flow that validates schema, flags unedited TODO
placeholders, and preserves provenance. The same workflow will absorb
Day-9 DMN proposals from the Haiku reflection loop.

### Day 7 post-prep — public release housekeeping

- Removed hardcoded BOTWA Palace path from `cli.py`; `cortex import-palace`
  now reads `$CORTEX_PALACE_PATH` env var, exits with a clear message if
  missing. Wing also reads `$CORTEX_PALACE_WING` env var.
- Added `LICENSE` (MIT)
- Added `CONTRIBUTING.md` with tripwire/rule/pattern authoring rules,
  fail-open contract, PR checklist
- Added `.github/workflows/ci.yml`: pytest + ruff on Python 3.10 / 3.11 / 3.12
- Added README badges and a disclaimer framing the 13 seed tripwires as
  concrete working examples from the Polymarket project rather than
  universal defaults
- Changed `pyproject.toml` `authors` from `BOTWA` to `Cortex contributors`

This is the first commit that can be published to a public GitHub repo
without leaking private paths or domain-specific defaults.
