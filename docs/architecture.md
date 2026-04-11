# Cortex architecture

> Why Cortex exists, how it's designed, and what it deliberately isn't.

## The problem

AI coding agents forget.

They have access to vector memory stores (ChromaDB, Palace, RAG) populated
with thousands of lessons from past work, but those stores are **passive**
— the agent must *know* to query them, and in practice usually doesn't.
Critical lessons sit in storage while the same mistake gets made again.

Cortex was built after three independent failures in a single week, each
with a different root cause. The three-failure taxonomy drives the whole
design.

## Three failure modes

### Failure 1 — Blindness: the lesson did not exist yet

A lookahead bug in `DETECTOR/backfill_features.py:461` labeled feature bars
with `slot_ts = (ts // 300) * 300` — floor-of-open-time — so every row's
computed values actually reflected the window **after** `slot_ts`. Backtests
showed 100% / 98.6% WR. Two bots deployed live. **−$7.78 in 82 minutes,
auto-killed on the 3rd consecutive loss.**

The lesson documentation (`feedback_lookahead_in_features_parquet.md`) was
created **2.5 hours after the kill**. Memory didn't fail — there was
nothing in memory to fail with.

**Prevention requires code-level pre-flight verification, not memory.**

### Failure 2 — Laziness: the lesson existed but was never loaded

The narrative "Polymarket taker fee is 10% flat" appeared in code comments,
logs, and docs. The empirical formula
`fee = 0.072 × min(p, 1−p) × size_shares` was derivable from a single
on-chain round-trip test that the agent had access to. **The test was
never run.** Weeks of mid-price classical-alpha research produced PnL
numbers that were wrong by ~60% until the test was finally executed.

Memory had relevant hints. The agent never queried them.

**Prevention requires active injection of lessons into working context.**

### Failure 3 — Ignored: lessons were loaded but never synthesized

A single session launched 9 replay tests of directional 5m Polymarket
hypotheses. Three independent warnings were **already in the loaded
context** at the start of the session:

- `feedback_information_decay_5m.md` — 1.45pp of edge lost per minute of delay
- `feedback_adverse_selection_maker.md` — 10–14pp WR drop on maker fills
- `feedback_late_lock_replay_traps.md` — 2.4pp spread+slip floor per trade

None of the 9 tests survived structural costs. The three warnings sum to
approximately **19.65pp of cumulative drag**, which kills any directional
signal under 20pp pre-fee — and that ceiling is unreachable on 5m data.
**Nobody ever summed them.** Each warning was seen as independent, so none
triggered the "walk away" decision individually.

**Prevention requires cost-component summation across loaded lessons.**

## Mapping failure modes to subsystems

| Failure mode | Cause | Cortex subsystem |
|---|---|---|
| Blindness | lesson does not exist | **Verifier** (pre-flight code check) |
| Laziness | lesson exists but unretrieved | **Classifier** (hook injection) |
| Ignored | lessons loaded but uncomposed | **Synthesizer** (cost summation) |

One mechanism cannot fix all three. Rule-based keyword injection — the
pattern most hook-based systems use — only addresses Laziness. Cortex has
a separate subsystem for each mode plus a TF-IDF fallback for rule-engine
gaps, all over a single SQLite store.

## Architecture overview

```
         ┌──────────────────────────────────────────┐
         │              SQLite store                │
         │ tripwires · cost_components ·            │
         │ synthesis_rules · violations             │
         └──────────────────────────────────────────┘
              │            │            │
              ▼            ▼            ▼
         ┌────────┐  ┌──────────┐  ┌──────────┐
         │Classify│  │Synthesize│  │ Fallback │
         │ rules  │  │ sum drag │  │ TF-IDF   │
         └────────┘  └──────────┘  └──────────┘
              │            │            │
              └────────────┼────────────┘
                           ▼
                 ┌───────────────────┐
                 │   cortex-hook     │  ← stdin JSON from Claude Code
                 │   UserPrompt      │    UserPromptSubmit event
                 │   Submit          │
                 └───────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │ <cortex_brief>       │  ← stdout JSON
                │ injected into        │    hookSpecificOutput
                │ agent context        │    .additionalContext
                └──────────────────────┘


         ┌────────────────┐
         │  cortex-watch  │  ← passive PostToolUse audit log
         │  PostToolUse   │    (feeds Day-5 DMN accounting)
         └────────────────┘

         ┌──────────────────────┐
         │  cortex-check-X      │  ← standalone verifier CLIs
         │  (pre-flight checks) │    (e.g. check_feature_lookahead)
         └──────────────────────┘
```

### Classify

`cortex/classify.py` + `cortex/rules/*.yml`. Rule files are YAML documents
containing a list of rules; each rule has `match_any`, `and_any`, and
`inject`. A rule fires when the prompt tokens contain at least one word
from each of the first two sets. The `inject` list is added to the
matched-tripwire set.

Tokenization is `[a-z0-9_\-]+` case-insensitive. Cyrillic is silently
dropped — mixed Russian+English prompts still work because the English
domain terms get extracted.

### Synthesize

`cortex/synthesize.py`. After the classifier produces a matched-tripwire
set, the synthesizer checks each synthesis rule. For each rule, it sums
the `cost_components` whose `tripwire_id` is in the matched set (signed
by `drag` / `boost`). If the sum crosses the rule's threshold, the rule
fires.

Fired rules appear at the **top** of the injected brief as a
`SYNTHESIS (cumulative cost)` section, before any individual tripwire
detail. The math is shown explicitly so the agent sees
`2.4 + 7.25 + 10.0 = 19.65pp >= 5.0pp` rather than three separate bullet
points.

Partial matches work: if only 2 of 3 components are active but the sum
still crosses the threshold, the rule fires. This matches real-world
behavior where missing one data point doesn't rescue a doomed strategy.

**This is the novel contribution.** Most memory systems list matched
lessons; Cortex composes them.

### Fallback (TF-IDF)

`cortex/tfidf_fallback.py`. When the rule engine returns zero matches, the
hook falls through to keyword scoring. Every tripwire gets scored by
weighted token overlap:

- **Trigger match**: +3.0
- **Title match**: +3.0
- **Body match**: +1.0

Each unique prompt token contributes at most once, at its highest-weighted
location. Threshold defaults to 3.0 (one trigger or one title hit).
Results sort by score descending then severity. Top-3 inject as a compact
`<cortex_brief source="keyword_fallback">` block, explicitly marked
advisory so the agent can distinguish fallback from authoritative matches.

### Verify

`cortex/verifiers/`. Each verifier is a standalone CLI that scans code for
a specific failure pattern. `check_feature_lookahead` greps for
`slot_ts = (ts // N) * N` without a forward shift — the canonical
lookahead bug from Failure 1.

When a tripwire has `verify_cmd` set, users can re-check whether the
lesson still applies against the current codebase. Verifiers are invoked
manually or via external scheduling; they are **not** auto-run from the
hook yet (Day 5 may add this, gated).

## Cortex vs Palace: separate stores, by design

A common question: does Cortex automatically see new content I add to
Palace memory? **No.** And the separation is intentional.

| | Palace | Cortex |
|---|---|---|
| **Location** | `.mempalace/palace/` (ChromaDB + HNSW, 21K+ drawers) | `.cortex/store.db` (SQLite, ~11 tripwires) |
| **Ingestion** | Auto-mined from `*.md` / `*.py` via `mine_palace.py` | Hand-curated via `cortex/importers/*.py` or `cortex add` |
| **Query mode** | Semantic cosine similarity over embeddings | Exact-keyword classifier + TF-IDF fallback |
| **Cold-start cost** | ~2-3 seconds (ONNX + chromadb load) | <1 ms |
| **Coverage profile** | Wide, noisy, semantically-driven | Narrow, curated, keyword-driven |
| **Failure-mode target** | Reference material for ad-hoc research | Active alerts at task start |

**11 tripwires ≠ 21,000 drawers, and that's the point.** A tripwire is a
lesson distilled from a specific past failure with a quantifiable cost,
actionable "how to apply" instructions, and validated triggers. Palace
drawers are raw text chunks. Automating Palace → Cortex would dilute the
signal with noise.

**If you learn something new and want Cortex to alert on it**, you add
a tripwire manually — either by editing `cortex/importers/memory_md.py`
or via `cortex add`. See [authoring.md](authoring.md) for the workflow.

**A future `cortex import-palace --room findings --min-sim 0.6 --to-inbox`**
helper is on the Day-5 roadmap for semi-automatic drawer → tripwire
promotion with a human approval step.

## Why SQLite

- Diffable in git (changes visible in PR review)
- Inspectable with any SQLite browser
- Atomic UPSERTs preserve violation counts across re-migrations
- Ships with Python (zero install friction)
- Handles 10k+ tripwires without consideration

Alternatives considered:

- **JSON file**: no concurrency, no atomicity, harder to query
- **Postgres**: operational overhead, authentication, process management
- **Redis**: lost durability, requires server, wrong durability profile

SQLite is the unexciting correct answer.

## Why rule-based, not semantic

Rules are **predictable**. If the user asks about backtesting, they get
the backtesting tripwires, always. When a new false positive appears, the
rule is a diffable YAML line you can tighten. Semantic search is harder
to debug — when it misses or over-fires, you can't grep for "why".

The TF-IDF fallback exists for the case where the rule engine misses due
to vocabulary gaps. It's still deterministic (weighted token overlap), so
you can debug it the same way as the rules.

## Rejected path: Palace semantic daemon

Built Day 4 morning as `cortex/palace_client.py` + `cortex/server.py` +
`cortex/fallback.py`. Killed Day 4 afternoon. Three reasons:

1. **Narrow coverage.** The ONNX embedding model is English-only and
   generic. Short queries (`show top features`) scored zero hits.
   Russian queries scored zero hits. Only long, domain-specific English
   queries worked.
2. **Infrastructure cost.** Required a long-running daemon holding
   ~200MB of ONNX + chromadb in memory, a warmup phase (~30s), and an
   HTTP layer between the hook and the daemon.
3. **Replacement was trivial.** Weighted token overlap over the 11
   tripwire bodies gave strictly better coverage at 1% of the
   complexity — 130 lines of pure Python vs 350 lines plus a daemon plus
   an ONNX model.

**Lesson: measure before you build.** A 20-minute TF-IDF prototype would
have answered the question "is rule-engine plus keyword scoring enough?"
before half a day went into daemon plumbing. The delete commit for the
Palace path is in the Day 4 history.

The Palace layer itself remains useful as the underlying substrate for
ad-hoc research queries via `palace_search.py`. It's just not in Cortex's
hot path.

## Fail-open contract

Every component is fail-open. The hierarchy:

1. Hook reads empty stdin → exit 0, no output
2. JSON parse fails → exit 0, no output
3. Classifier error → exit 0, no output
4. Store missing or locked → exit 0, no output
5. Verifier crash → exit 0, no output
6. Any other exception → exit 0, no output

**Principle: a broken Cortex must never block the user's interaction.**
If the hook throws, Claude Code sees empty `additionalContext` and
proceeds normally. The user doesn't notice.

## What Cortex is NOT

- **Not a replacement for vector memory.** Palace still answers "what's in
  my knowledge base"; Cortex answers "what should I be warned about right
  now."
- **Not a linter.** It operates on task intent, not code correctness.
- **Not an RL feedback loop.** Violation tracking is passive audit; rule
  tuning is human-driven.
- **Not a general agent framework.** It's a Claude Code integration point.

## Day 5 (shipped)

- **`cortex stats --sessions [--days N]`** — session audit analyzer reading
  `.cortex/sessions/*.jsonl`. Reports injection rate, top matched rules/
  tripwires/synthesis, tool-call distribution, and cold tripwires (never
  matched in window — candidates for tuning or removal).
- **`cortex import-palace "query"`** — smart-search helper that queries
  Palace via `mempalace.searcher` and emits tripwire draft templates. Human
  reviews and pastes into `cortex/importers/memory_md.py`. No automatic
  promotion — the curation step is intentional.

## Day 6 (shipped) — silent violation detection

The question Day 5 left open: *are injected tripwires actually applied, or
silently ignored?* Day 6 gives a data answer by closing the feedback loop
between `inject` events and subsequent tool calls.

- **Schema delta**: new `tripwires.violation_patterns` column (JSON array
  of regex strings), applied via idempotent `ALTER TABLE` migration.
- **`cortex/violation_detect.py`**: reads the session's event stream,
  collects tripwires that were injected or fallback-matched earlier in
  the same session, and pattern-matches their regexes against
  `tool_input` snippets from subsequent `PostToolUse` events.
- **Enriched `cortex-watch`**: logs `tool_call` events with a 500-char
  tool-specific snippet (Bash=command, Edit=file+diff, Read=path). When
  a violation regex matches, emits a separate `potential_violation`
  event with the tripwire id and matched pattern.
- **`cortex stats --sessions` effectiveness report**: new section shows
  per-tripwire `hits / violations / rate` with OK/WARN/FAIL status. Rate
  near 0 = lesson applied, rate > 0.5 = lesson ignored.
- **Seeded 2 patterns**: `lookahead_parquet` and `real_entry_price`.
  Tripwires without patterns can still be injected; they just don't
  contribute to effectiveness measurement. Authoring patterns is optional.

**The loop now closes.** Day 1-2 injected lessons blindly. Day 3 let us
synthesize across them. Day 5 measured injection coverage. Day 6 measures
injection *effectiveness*. Day 7+ can tune rules and bodies based on
which tripwires have high violation rates (ignored by the agent) vs low
(applied).

## Roadmap (Day 7+)

1. **Weekly reflection loop (DMN)** — cheap LLM (Haiku) processes session
   logs and proposes new tripwires to an inbox for human approval
2. **Verifier auto-run from hook** — critical tripwires with `verify_cmd`
   run the command live during hook invocation; block on verifier failure
3. **Inbox workflow** — `cortex inbox list/approve/reject` for DMN-proposed
   tripwire drafts
4. **Pattern authoring helper** — `cortex suggest-patterns <tripwire_id>`
   that reads session logs and proposes regex candidates from observed
   tool calls that the user retroactively marks as violations
