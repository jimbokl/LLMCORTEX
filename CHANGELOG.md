# Cortex changelog

## 0.1.0 — unreleased

Initial working version shipped over 4 iteration days.

### Day 16 — DMN Promoter (autonomous lifecycle)

**The promoter closes the loop between the Day-14 Surprise Engine, the
Phase-0 composite fitness score, and the Day-15 shadow lifecycle.**
Surprise pairs are classified by Haiku into `match / mismatch / partial`,
labels replace the Day-14 token-overlap heuristic per pair, and a pure
decider proposes `shadow -> active` / `active -> shadow` / `shadow ->
archived` transitions that the applier writes atomically to a new audit
table. Dry-run by default; mutation only with `--apply`.

Ships as a 6-stage rollout. Every stage was committed separately with
a green test suite so the git history walks the whole design.

- **Stage 1 (store.py)**. Two new tables with idempotent DDL and
  schema version bump 1 → 2:
  - `pair_classifications` — one row per classified surprise pair,
    primary key `(session_id, at)` so reruns of `cortex promote
    classify` never re-bill the API. Label enum `match / mismatch /
    partial / error`, confidence clamped to `[0,1]`, reasoning
    truncated to 300 chars.
  - `status_changes` — full audit trail for every tripwire status
    transition, populated by every promoter action and by the new
    `apply_status_transition` helper that wraps `set_status` + the
    audit write in a single transaction.

- **Stage 2 (`cortex/promoter_prompt.py` + `cortex/promoter.py`)**.
  Haiku classification layer with a defensive parser (code-fence
  strip, first `{...}` extract, enum check, confidence clamp,
  reasoning truncate) and a client-injectable `classify_pair` so
  tests never import `anthropic`. Any parse failure becomes a
  persisted `label='error'` row so the pair is not retried.

- **Stage 3 (`cortex/promoter.py` decider + applier)**. Threshold
  constants grounded in the live empirical distribution (median hit
  count 8, max cost_weight 2.67):
  - Primary shadow → active (ALL required):
    `MIN_HITS=5 MIN_DISTINCT_SESSIONS=3 MIN_TENURE_HOURS=168 (7d)
    MIN_FITNESS=5.0 MIN_MISMATCHES=2 MIN_CAUGHT_RATE=0.8`.
  - Classification-free fallback gate (OR): `FALLBACK_FITNESS=10.0
    FALLBACK_DISTINCT_SESSIONS=5 FALLBACK_CAUGHT_RATE=0.9`.
  - Demotion: `MAX_IGNORED_RATE_ACTIVE=0.5` (floor
    `MIN_HITS_FOR_IGNORED_DEMOTE=3`), `DORMANT_HOURS=720` (30d),
    `NEGATIVE_FITNESS_TENURE_HOURS=168`,
    `SHADOW_TENURE_HOURS_FOR_ARCHIVE=336` (14d).
  - Anti-flap cooldown: ≥2 changes in 168h freezes for another 168h.
  - Per-calendar-day caps (NOT per-invocation):
    `MAX_PROMOTIONS_PER_DAY=1 MAX_DEMOTIONS_PER_DAY=3`.
  `decide()` is pure — no store, no LLM, no wall clock. Clock is
  injected via a module-level `_now()` that tests monkeypatch.
  `apply_decisions()` also writes a `status_change` session event
  so `cortex timeline` surfaces each transition.

- **Stage 4 (`cortex/fitness.py` + `cortex/cli.py`)**. `compute_fitness`
  gains an optional `classification_index` kwarg. When a pair has a
  classification, the label REPLACES the Day-14 heuristic per-pair
  with `match→0.0 partial→0.5 mismatch→1.0 error→0.0` — replacement
  (not sum) because both signals measure the same evidence.
  Unclassified pairs still fall back to the heuristic, preserving
  Day 14 behavior exactly. The row now carries `distinct_sessions`
  and `mismatches` counters, both feeding the decider's gates.

- **Stage 5 (`cortex/cli.py`)**. User-facing CLI:
  - `cortex promote classify [--days] [--batch-size] [--dry-run]
    [--yes]` — scans surprise pairs, filters already-classified via
    PK, hard-caps at 200/call, prints cost estimate, requires `--yes`
    when >10 pairs queued.
  - `cortex promote run [--days] [--apply] [--session-id]` — runs
    the decider against current store state; dry-run by default.
  - `cortex promote log [--days]` — renders recent status changes
    with fitness snapshots.

- **Stage 6**. This CHANGELOG entry, live dry-run smoke test against
  the repo's `.cortex/store.db`, and suite verification.

**Test coverage**: +64 new tests over 6 commits. Stage 1 adds 13 store
tests. Stage 2 adds 19 unit tests (parser + prompt + classify_pair).
Stage 3 adds 18 more (decider + applier + clock injection). Stage 4
adds 6 regression tests (classification override vs heuristic
fallback + distinct_sessions tracking). Stage 5 adds 8 end-to-end
CLI tests. Full suite: **383 passing** (was 319 after Day 14 fix +
skills installer).

**Known limitations (deferred to Day 17+)**:
- Fitness history is not persisted — the "fitness < 0 for 7+ days"
  demotion rule is approximated as `current_fitness < 0 AND
  tenure_hours >= 168`.
- Classifications are not invalidated when a tripwire body is edited.
- The first shadow → active promotion is delayed by
  `MIN_TENURE_HOURS=168` (7 days) by design.

### Day 15 — Shadow Mode (status lifecycle)

**Tripwires now have a lifecycle: `active` -> `shadow` -> `archived`.
Shadow rules match the classifier and get logged to the audit log but
are never rendered into `<cortex_brief>`. This is the safe probation
path for DMN-proposed rules — the Day-9 lesson about auto-generated
regex overfitting told us we cannot auto-promote to active without
ground-truth evidence.**

The infrastructure ships here; the Day-16+ promoter loop that graduates
shadow -> active based on Day-14 Surprise Engine pairs is deferred until
real prediction data accumulates.

- **Schema delta**: `ALTER TABLE tripwires ADD COLUMN status TEXT
  NOT NULL DEFAULT 'active'`. Idempotent in `_migrate_schema()`. Fresh
  stores get a `CHECK (status IN ('active','shadow','archived'))`
  constraint via `_SCHEMA`; migrated stores enforce the same values via
  application-level validation in `add_tripwire()` and `set_status()`
  (SQLite cannot add a CHECK constraint after the fact).

- **Upsert intentionally omits `status` from the `ON CONFLICT DO UPDATE
  SET` clause**, alongside `born_at` / `violation_count` /
  `last_violated_at`. Re-running `cortex migrate` therefore never
  demotes a shadow-tested tripwire back to active, and never promotes
  a newly-seeded one if the user manually set it shadow. Tested by
  `test_upsert_preserves_status`.

- **New `CortexStore.set_status(tripwire_id, new_status)`** is the only
  code path that mutates the status field after creation. Validates
  against `_VALID_STATUSES`, returns False when the id is unknown,
  raises `ValueError` on bogus status strings.

- **`list_tripwires(status='active')` by default**. Day-15 signature
  gains `status: str | None = 'active'`. The default keeps every
  pre-Day-15 caller (including the hook path) from suddenly seeing
  shadow rows. Pass `status=None` for "all statuses" (used by
  `cortex list --all` and tests).

- **`classify.py::classify_prompt` splits the result**:
  - `tripwires`        -> active rows only, rendered into the brief
  - `shadow_tripwires` -> shadow rows only, audit log only
  - `archived` rows are hidden from both lists entirely
  - Synthesis runs over the ACTIVE set only. Shadow synthesis is a
    Day-16+ concern once we have evidence to score with.

- **`hook.py` logs `shadow_hit` events**. When the classifier returns
  a non-empty `shadow_tripwires`, the hook writes one event to the
  session audit log BEFORE proceeding with the normal flow (so even a
  later crash on the fail-open path preserves the signal needed by
  the Day-16+ promoter). Shadow matches do NOT suppress the fallback:
  if only shadow rows matched, `result["tripwires"]` is empty and
  TF-IDF fallback still fires.

- **CLI surface**:
  - `cortex list --status {active|shadow|archived}` filter
    (default remains `active`).
  - `cortex list --all` shows every status.
  - `cortex list` now prints the `STATUS` column per row.
  - `cortex show <id>` displays the `Status:` line.
  - `cortex add ... --status {active|shadow|archived}` initial lifecycle
    state (defaults to `active`).
  - `cortex status <id> <new_status>` explicit transition command.
  - `cortex inbox approve --shadow <draft_id>` promotes a draft as
    shadow. This is the intended safe path for DMN-proposed rules: an
    operator reviews the draft in the inbox, hits `approve --shadow`,
    and the rule starts accumulating `shadow_hit` audit entries
    without touching the agent's context window.

- **`inbox.draft_to_tripwire_kwargs`** now recognizes `status` as a
  passthrough field, so drafts authored with an explicit
  `"status": "shadow"` key in the JSON file are honored at approve
  time (no flag needed).

- **Tests (+24, total 276)**:
  - `test_store.py` — 10 new tests covering the default 'active'
    behaviour, explicit status on add, validation of invalid statuses,
    `list_tripwires` default + filter + all, `set_status` transition
    and error paths, upsert preserving status across re-migration,
    and a full legacy-store migration simulation (drop the column,
    reopen, verify rows come back with `status='active'`).
  - `test_classify.py` — 2 new tests: classifier splits matched rows
    into active / shadow / (hidden archived) buckets, and
    `render_brief` never includes shadow tripwires even when they
    match the rule engine.
  - `test_hook.py` — 3 new tests: shadow_hit event is logged alongside
    the normal inject for a mixed result, all-shadow matching still
    logs shadow_hit even when the hook falls through to TF-IDF,
    default state (no shadow rows) emits no shadow_hit events.
  - `test_inbox.py` — 1 new test: `draft_to_tripwire_kwargs` passes
    through a `status` field.
  - `test_cli_status.py` — 8 new end-to-end argparse smoke tests
    covering the four new CLI affordances.

**Fail-open preserved**: every new path is wrapped in `try/except
Exception: pass`. The `shadow_hit` write failing on IO error is
swallowed, the hook still emits the active brief, exit code stays 0.
Covered by the existing `test_hook_*_fails_open` family plus the
Day-15 additions above.

**Deliberately NOT shipped in Day 15** (see the Day-15 design note in
the head of `classify.py` and `hook.py`):

- **Day 16 — `cortex promote`** offline promoter that reads Day-14
  surprise pairs plus `shadow_hit` events and auto-graduates rules
  when `confidence >= 3` AND `cost_usd >= threshold`. Needs >=14 days
  of real Surprise Engine data to calibrate the promotion threshold
  (picking it blind is guessing). Blocker: empty surprise substrate.
- **Day 17 — Cost-weighted LTD pruning.** Auto-archival of cold
  tripwires is safe but not urgent. Auto-demoting `severity` on
  low-violation rules is unsafe as originally proposed: a $500
  one-shot tripwire can have `violation_count=1` forever and still
  be worth keeping critical. The weighting formula must be cost-aware
  and gated on `cost_usd < threshold` before any demotion.
  Deferred until we have data showing which tripwires genuinely drift
  cold vs stay valuable but rarely triggered.
- **Day 18+ — Auto-mutation of YAML triggers by DMN.** This is
  directly the Day-9 failure mode: auto-generated rules overfit to
  one incident and break legitimate code. If/when it ships, mutations
  go to the `inbox/` as diff-proposals, never direct YAML writes.

### Day 14 — Surprise Engine (predictive coding)

**Agent emits a falsifiable prediction before acting; Cortex captures
it and pairs it with the real outcome, producing DMN's missing label
column.**

Borrowed straight from Karl Friston's predictive-coding framework: a
brain only *learns* when reality diverges from its forward model. Our
agent has no such signal today — DMN (`cortex reflect`) has to guess
from tool_input snippets alone what counted as a "mistake". Day 14
fixes that.

- **`cortex/classify.py::_render_predict_block`**: when `render_brief`
  sees at least one `critical` tripwire in the result, it appends a
  soft-inject instruction block to the `<cortex_brief>` asking the
  agent to emit a two-field XML prediction in its next reply:

      <cortex_predict>
        <outcome>falsifiable prediction ...</outcome>
        <failure_mode>most likely technical reason this might fail</failure_mode>
      </cortex_predict>

  The two-field shape matters. A lazy "expect success" outcome is
  cheap to fake; naming a concrete failure mode forces System-2
  reasoning. When `failure_mode` diverges from the real outcome that's
  the maximum-information signal for DMN.

- **`cortex/surprise.py`** (new module, ~260 LOC):
  - `parse_prediction(text)` — regex over `re.DOTALL | re.IGNORECASE`,
    collapses whitespace inside each field, caps fields at 500 chars,
    takes the FIRST block when multiple are present, returns None
    on any malformed / missing input
  - `read_last_assistant_text(transcript_path)` — walks a Claude Code
    transcript jsonl once, returns the `text` content of the most
    recent `type: "assistant"` entry. Thinking blocks and tool_use
    blocks are ignored because the tag must appear in visible reply
    text. Fail-safe on bad lines, missing files, malformed JSON.
  - `collect_pairs(days, sessions_root)` — walks session logs,
    maintains `active_tripwires` from the most recent inject /
    fallback event, pairs each `prediction` event with the
    immediately-following `tool_call` in the same session. Orphaned
    predictions (no tool_call) are returned with `tool_name=None`
    so the table surfaces "agent predicted but never acted" as a
    separate signal.
  - `render_surprise_table(pairs, days, max_rows)` — ASCII block
    showing total / paired / orphan counts, then per-row predict /
    fail_mode / actual tool / response.

- **`cortex/watch.py` extensions**:
  - Reads `transcript_path` from the PostToolUse payload, extracts the
    last assistant text, calls `parse_prediction`, logs a `prediction`
    event **before** the `tool_call` event (so forward-scan pairing
    works).
  - **Dedup**: when one assistant message contains N tool_use blocks,
    PostToolUse fires N times against the same transcript. The
    `_already_logged` helper scans backwards through the session log
    and skips writing the prediction if the most recent prediction
    event already has matching `outcome` + `failure_mode`. Prevents
    N duplicate rows per multi-tool message.
  - `tool_call` events now carry `response_snippet` in addition to
    `input_snippet`. `_summarize_tool_response` handles the common
    PostToolUse response shapes: dict with `stdout`/`text`/`content`,
    plain string, or JSON-dumped fallback. Truncated at 500 chars.
    This is what `cortex surprise` pairs against the prediction.

- **`cortex/cli.py::cmd_surprise`**: new `cortex surprise [--days N]
  [--max-rows N]` subcommand. Pure read-only walk over session logs,
  no LLM calls. Day 15+ will add DMN match/mismatch classification
  on top of this raw substrate.

- **`cortex/stats.py::render_timeline`**: `PREDICT` rows now appear in
  the ASCII event timeline alongside INJECT / tool_call / VIOLATION.
  Respects `--anonymize` (outcome and failure_mode get redacted
  through `anonymize_snippet`).

- **Tests (+20, total 252)**:
  - `test_surprise.py` — 14 tests covering regex happy path, multiline
    fields, missing/malformed tags, first-block-wins, field length
    caps, transcript walker picking the most recent assistant entry,
    tolerating bad lines, collect_pairs pairing / orphan / two
    predictions in a row, render empty / non-empty table.
  - `test_watch.py` — 4 new tests: prediction captured from transcript
    and logged before tool_call, dedup across multiple tool_calls for
    one message, no prediction when transcript_path is absent,
    tool_response snippet captured into `tool_call` event.
  - `test_classify.py` — 2 new tests: `<cortex_predict>` footer fires
    on critical tripwires, is omitted when only medium/high present.

**Why this is Day 14 and not Day 15**: the raw substrate is the
cheap, safe half. Storing pairs costs nothing beyond disk and doesn't
touch the hook path's 60ms budget (parsing happens in `cortex-watch`,
PostToolUse, which is not user-facing latency). DMN-side surprise
classification is the expensive half — needs a Haiku call per pair
and an unambiguous scoring rubric — and gets deferred to Day 15+
when real session data accumulates.

**Fail-open preserved**: every new path in `watch.py` is wrapped in
`try/except Exception: pass`. Transcript missing, malformed JSON,
parse errors, dedup-read failures — all swallowed, `tool_call`
logging still runs, hook still exits 0. Verified by
`test_watch_no_prediction_when_transcript_absent`.

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

### Day 12 — PyPI release prep

**Wheel-ready for `pip install cortex-agent`.**

- **`pyproject.toml` enriched**:
  - Expanded `description` to include the full value prop
  - Added `[project.urls]`: Homepage, Repository, Issues, Changelog, Documentation
  - Added more classifiers (Intended Audience, Operating System, Quality Assurance, Utilities)
  - Expanded keywords (agent, memory, llm, claude, claude-code, hooks, tripwires, active-memory, prompt-injection, cost-synthesis)
- **Local build verified**: `python -m build` produces `cortex_agent-0.1.0.tar.gz`
  (91 KB) + `cortex_agent-0.1.0-py3-none-any.whl` (71 KB). Wheel contains
  21 Python modules, 2 YAML rule files, and all 4 console_scripts entries
  (`cortex`, `cortex-hook`, `cortex-watch`, `cortex-check-lookahead`).
- **Publishing process documented** in `CONTRIBUTING.md` (maintainer-only
  section): version bump, CHANGELOG migration, TestPyPI dry-run, real
  upload via `twine`, git tag, GitHub release.
- **Not yet uploaded to PyPI** — awaiting project maintainer credentials
  and TestPyPI verification. The source dist and wheel are
  reproducible with `python -m build` from a clean checkout.

### Day 11 — Haiku DMN reflection loop

**`cortex reflect [--days N] [--dry-run]`** — cheap LLM analysis of
session audit logs that proposes new tripwires to the inbox.

- **`cortex/dmn.py`** (~450 lines): full reflection pipeline
  - `build_session_summary(days)` — aggregates recent activity via
    `cortex.stats.collect_sessions` (event counts, top tripwires,
    cold tripwires, silent violations)
  - `build_existing_tripwires_summary(db_path)` — compact list for
    the anti-duplication constraint in the prompt
  - `build_prompt(summary, existing, max_proposals)` — renders a
    carefully engineered prompt that includes the existing tripwires
    (do not duplicate), recent session activity, and a strict JSON
    output schema
  - `parse_proposals(response_text)` — tolerant of leading/trailing
    prose, markdown code fences, non-dict elements; returns empty
    list on any parse error
  - `call_haiku(prompt, model, client)` — thin wrapper over
    `anthropic.messages.create` with dependency-injection for tests
  - `write_proposals_to_inbox(proposals)` — strips Haiku's `evidence`
    field into the body prefix, writes each proposal via `inbox.write_draft`
    with `source="dmn_haiku"`
  - `run_reflection(days, model, max_proposals, dry_run, client)` —
    orchestrates the full flow, handles errors, returns a structured
    result dict for rendering
  - `render_reflection_report(result)` — human-readable output with
    dry-run / error / success branches
- **Default model**: `claude-haiku-4-5-20251001` (the most recent Claude
  Haiku). Override with `--model`.
- **Optional dependency**: `pip install cortex-agent[dmn]` installs
  `anthropic>=0.40`. Without it, `cortex reflect` fails with a clear
  "install via [dmn] extra" message.
- **Budget**: measured at ~1069 input tokens on a 17-session live run
  (~$0.001 input + $0.01 output = ~$0.011 per reflection). Trivial.
- **`--dry-run` flag**: builds and prints the prompt that would be sent
  WITHOUT making an API call. Essential for reviewing prompt quality
  before paying for tokens.
- **Live dry-run verified** on 17 sessions / 1067 events from real
  BOTWA session history. The prompt includes all 13 existing tripwires,
  top injected tripwire counts (29x poly_fee_empirical, 25x
  real_entry_price, etc.), and a complete JSON schema example.
- **Mock-tested end-to-end**: 19 tests in `test_dmn.py` cover
  session summary / prompt building / proposal parsing (clean / code-fenced
  / prosed / malformed) / Haiku client via dependency injection / inbox
  write with evidence-to-body promotion / error handling / proposal
  capping / render report (dry-run / success / error branches).
- **Real API call not tested in CI**: requires `ANTHROPIC_API_KEY`, which
  is not set on the dev machine (Claude Code subscriptions use a
  different auth mechanism and cannot be used for direct API calls).
  The dry-run + mocked tests prove the flow is correct end-to-end;
  running `cortex reflect` against the live API is one env var away.
- +19 tests (total grows to 213)

### Day 10 — verifier blocking mode

**When a critical pre-flight verifier fails, block the prompt.**

- **New env var**: `CORTEX_VERIFY_BLOCK=1`. Opt-in on top of
  `CORTEX_VERIFY_ENABLE=1`. Blocking requires BOTH to be set.
- **`cortex/hook.py`**: after the verifier results are computed, scan
  for any result with `passed: False`. If blocking is enabled AND any
  verifier failed, the hook still emits the brief (so the agent sees
  the FAIL marker and can explain the block) but then **returns exit
  code 2** instead of 0. Claude Code treats non-zero
  `UserPromptSubmit` as "reject this prompt".
- **Audit log**: on block, the hook writes a `verifier_blocked` event
  with the list of failed tripwire ids. The `inject` event also gets
  a new `blocked` field. Both visible in `cortex stats --sessions`
  for Day-13+ effectiveness tracking.
- **Fail-safe**: blocking only triggers on successful verifier runs
  that reported passed=False. Any verifier crash (timeout, parse error,
  exception) is still `skipped`, never counted as a failure, never
  blocks. The hook remains fail-open on every error path.
- **Tests**: 4 new tests in `test_hook.py` cover:
  - block env unset -> normal flow
  - block env set but enable unset -> normal flow
  - block + enable + verifier fail -> exit 2 + brief still emitted
  - block + enable + verifier pass -> exit 0

### Rejected path — `cortex serve` daemon

A long-running HTTP daemon that keeps the Python interpreter + cortex
imports warm, dropping the per-prompt `cortex-hook` subprocess cost
from ~60ms (measured in Day 8.5) to <5ms.

**Rejected after measurement.**

1. **60ms is below human perception.** At 20 prompts/session × 10
   sessions/day = 200 prompts/day, the daemon saves ~11 seconds of
   wall time per day. Against 8-12 hours of actual work, that's
   immeasurable.
2. **Daemon complexity is real.** Lifecycle management (start on
   login? systemd unit? Windows service?), health checks, crash
   recovery, port conflicts, stale-state on upgrades. The
   `cortex-hook` subprocess is stateless, crashes don't persist,
   upgrades are atomic.
3. **Day 4 already taught this lesson.** The Palace semantic daemon
   was built and killed the same day because the ROI didn't justify
   the ops burden. `cortex serve` would have been the same story for
   the same reason.

If future benchmark data shows the 60ms becoming load-bearing (e.g.,
a high-throughput batch pipeline hitting the hook hundreds of times
in a row), we'll revisit. Until then: **the subprocess model is the
right abstraction** and ~60ms is an entirely acceptable tax.

Documented here in the spirit of Day 4's Palace-daemon post-mortem:
measure before you build, document what you chose not to build.

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
