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

A lookahead bug in a feature pipeline labeled bars with
`slot_ts = (ts // 300) * 300` — floor-of-open-time — so every row's
computed values actually reflected the window **after** `slot_ts`.
Backtests showed inflated win rates near 100%. Bots deployed live
underperformed their backtest WR by ~30 percentage points and
auto-killed on consecutive losses.

The forensic report documenting the bug was written hours after the
incident. Memory didn't fail — there was nothing in memory to fail with.

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

## Day 10 (shipped) — verifier blocking mode

Day 7 added optional pre-flight verifier execution from the hook. Day
10 makes it **blocking** when opted in: set `CORTEX_VERIFY_BLOCK=1`
(on top of `CORTEX_VERIFY_ENABLE=1`), and any verifier that reports
`passed=False` on a matched critical tripwire causes the hook to
exit with code 2 — Claude Code's signal for "reject this prompt".

The brief is still emitted before the exit so the agent sees the FAIL
marker and can explain the rejection. The `inject` audit event gets
a `blocked: true` field; a separate `verifier_blocked` event records
the failed tripwire ids for Day-13+ accounting.

Blocking is additive to the existing fail-open contract — any verifier
crash (timeout, OS error, parse error) is still `skipped`, never
counted as a failure, never triggers a block.

## Day 11 (shipped) — Haiku DMN reflection loop

Day 6 answered "are my tripwires being applied?" Day 9 answered
"what should I pattern-match to detect violations?" Day 11 closes the
last loop: **what tripwires am I missing?**

`cortex reflect` reads recent session audit logs, builds a summary
(event counts, top injected tripwires, cold tripwires, silent
violations, non-matching sessions), loads the current store so the
LLM doesn't duplicate, and asks Claude Haiku 4.5 to propose new
tripwires. Proposals are written to the `.cortex/inbox/` directory
(Day 8) for human review via `cortex inbox approve`.

```
          ┌──────────────────────┐
          │  cortex reflect      │
          │  [--days N]          │
          │  [--dry-run]         │
          └──────────────────────┘
                    │
                    ▼
    ┌─────────────────────────────────┐
    │  build_session_summary(days)    │
    │  build_existing_tripwires(...)  │
    │  build_prompt(...)              │
    └─────────────────────────────────┘
                    │
                    ▼
    ┌─────────────────────────────────┐
    │  call_haiku(prompt)             │  <- Anthropic SDK
    │  parse_proposals(response)      │
    └─────────────────────────────────┘
                    │
                    ▼
    ┌─────────────────────────────────┐
    │  write_proposals_to_inbox(...)  │
    └─────────────────────────────────┘
                    │
                    ▼
       .cortex/inbox/dmn_haiku_*.json
                    │
                    ▼
        cortex inbox list/show/approve
```

**Budget**: measured at ~1069 input tokens on a 17-session real-world
sample. At Haiku 4.5 pricing (~$1/1M input, ~$5/1M output), a
reflection call costs about $0.011. Negligible.

**Optional dependency**: `anthropic>=0.40` under the `[dmn]` extra.
Cortex's core install (just `pyyaml`) stays minimal. `cortex reflect`
fails with a clear install-instruction message if the SDK is absent.

**Why Haiku and not Sonnet/Opus?** Cost, latency, and the task
profile. Reflection is a pattern-extraction job over structured data
with a clear output schema. Haiku 4.5 handles it well and costs ~10x
less than Sonnet. The dry-run mode + inbox approval gate provide
human-in-the-loop quality control.

**Why inbox and not direct store writes?** Autopromoted LLM proposals
would dilute the curated signal in the store, which is the one thing
Cortex is built to protect. The inbox step is non-negotiable: a human
reads each proposal, edits TODO fields, and runs `cortex inbox approve`.

## Rejected path — `cortex serve` daemon

Day 8.5 measured the full `cortex-hook` subprocess cost at ~60ms
(p50), dominated by Python startup + module imports. A long-running
daemon would drop that to <5ms.

Rejected after measurement:

1. **60ms is below human perception.** Wall-clock savings per day are
   on the order of 10 seconds. Against a full workday, that's noise.
2. **Daemon complexity (lifecycle, health checks, port conflicts,
   upgrades) significantly exceeds the savings.** The current
   subprocess model is stateless, crash-safe, and upgrade-atomic.
3. **Day 4 already taught this lesson** with the Palace semantic
   daemon. Measure, then build only when the measurement justifies
   the complexity.

If a future load profile (high-throughput batch pipelines hitting the
hook hundreds of times per minute) ever shifts the ROI calculation,
we'll revisit. Until then: the subprocess model is the right
abstraction.

## Day 8 (shipped) — inbox workflow

The `cortex import-palace` command (Day 5) surfaces Palace drawers as
tripwire draft templates. Until Day 8 those templates were printed to
stdout and the user had to copy-paste them into
`cortex/importers/memory_md.py` and re-run `cortex migrate`. The
inbox workflow formalizes the approval step:

```
    palace query
         |
         v
    cortex import-palace --to-inbox
         |
         v                            fail-safe at every I/O
    .cortex/inbox/*.json  <----+
         |                     |
         v                     |
    editor (fill TODO fields)  |
         |                     |
         v                     |
    cortex inbox list          |
    cortex inbox show <id>     |
    cortex inbox approve <id>  +---->  store.add_tripwire(...)
    cortex inbox reject <id>
```

Each draft is a JSON file with:

- `draft_id`: unique identifier (auto-generated as
  `<source>_<timestamp>_<uuid6>` to avoid collisions)
- `source`: provenance tag (`manual`, `palace_polymarket`, `dmn_haiku`)
- `created_at`: ISO timestamp
- `draft`: the tripwire fields themselves

`validate_draft()` returns `(missing_fields, todo_fields)` for a given
draft. A draft is READY to approve when both lists are empty; the
`approve` command refuses to promote a draft with `TODO` placeholders
unless `--force` is passed.

The inbox is the foundation for the Day-9 Haiku reflection loop:
instead of the loop writing directly to the store, it writes to the
inbox and the human stays in the approval loop. Automatic promotion of
LLM-proposed tripwires would dilute the curated signal.

## Day 7 (shipped) — pre-flight verifier auto-run

The `verify_cmd` field on a tripwire has existed since Day 1, but until
Day 7 it was documentation only — a command the user could run by hand.
Day 7 wires it into the hook path: when a critical tripwire matches, the
hook runs `verify_cmd` with safety rails and appends the result to the
brief. "This is a known bug" becomes "this is a known bug AND your current
code has 3 instances of it right now — fix before proceeding."

Opt-in via `CORTEX_VERIFY_ENABLE=1`. Allow-list guards commands against
the prefix list `cortex-` / `python -m cortex` by default so that a
legacy `verify_cmd` pointing at a destructive binary (e.g. a real trade
executor) can't accidentally run. `shlex.split` + `shell=False` +
3-second hard timeout + captured-output truncation. Any exception
results in a `skipped` marker — the hook never crashes.

See [hooks.md#environment-variables](hooks.md#environment-variables) for
the full env var reference.

## Roadmap (Day 8+)

1. **Weekly reflection loop (DMN)** — cheap LLM (Haiku) processes session
   logs and proposes new tripwires to an inbox for human approval
2. **Inbox workflow** — `cortex inbox list/approve/reject` for DMN-proposed
   tripwire drafts
3. **Pattern authoring helper** — `cortex suggest-patterns <tripwire_id>`
   that reads session logs and proposes regex candidates from observed
   tool calls that the user retroactively marks as violations
4. **Verifier blocking mode** — when a `critical` pre-flight verifier
   fails, return a non-zero exit from the hook so Claude Code surfaces a
   hard stop rather than advisory context

## Autonomy roadmap (Day 14 -> 18)

Cortex has always been read-mostly. The agent reads the brief; a
human approves every change to the store via the inbox. That is fine
at small scale but leaves a question: can the system propose AND grade
its own rules without a human in every loop, without degenerating
into the auto-regex overfitting failure we saw on Day 9?

The answer is to copy two more tricks from neuroscience and wire them
on top of the substrate that already exists — **predictive coding**
(Day 14, shipped) and **shadow probation with cost-weighted LTD
pruning** (Day 15 shipped, Day 16-17 pending). The key insight: the
agent needs a *ground-truth signal* before Cortex can safely
auto-promote anything, and that signal only becomes available once
we are capturing prediction/outcome pairs.

### Day 14 — Surprise Engine (shipped)

When a `critical` tripwire fires, the brief asks the agent to emit

    <cortex_predict>
      <outcome>falsifiable prediction</outcome>
      <failure_mode>most likely technical reason this might fail</failure_mode>
    </cortex_predict>

`cortex-watch` reads the Claude Code transcript on every PostToolUse,
extracts the block, and logs a `prediction` event immediately before
the `tool_call` event. `cortex surprise` renders the paired
`{prediction, tool_call, tool_response}` timeline. The two-field shape
forces System-2 reasoning: a lazy `outcome: "success"` is trivial; a
concrete `failure_mode` is not. When `failure_mode` diverges from the
real outcome that is the maximum-information signal for DMN.

No LLM calls are made here. DMN scoring of the pairs as
match / partial / mismatch is a Day-16 concern. Day 14 stores the
raw substrate.

### Day 15 — Shadow Mode (shipped)

Tripwires gain a `status` column with values `active` / `shadow` /
`archived`. `classify_prompt` splits matched rows into
`tripwires` (rendered into the brief) and `shadow_tripwires` (logged
as `shadow_hit` audit events, never rendered).

`cortex inbox approve --shadow <draft_id>` is the intended path for
DMN-proposed drafts: the operator reviews the JSON, hits approve with
the flag, and the rule starts accumulating `shadow_hit` entries
without touching the agent's context window. This replaces the
all-or-nothing "approve to active" flow while preserving the inbox
review step.

Upsert in `add_tripwire` intentionally omits `status` from the
`ON CONFLICT DO UPDATE SET` clause, so `cortex migrate` re-runs never
clobber a manual shadow decision. Likewise, the only code path that
mutates `status` after creation is `CortexStore.set_status()`, used
by `cortex status` CLI and (eventually) the Day-16 promoter.

### Day 16 — `cortex promote` (deferred, needs data)

The offline promoter reads the Day-14 surprise pairs plus the Day-15
`shadow_hit` audit events. For each shadow tripwire, it computes a
`confidence` score from the evidence:

- In sessions where this shadow rule matched, how often did the
  `{prediction, tool_response}` pair classify as "mistake"? Each hit
  increments confidence by 1.
- Promotion criterion: `confidence >= 3 AND cost_usd_estimate > 50`.
- Promoted rules transition `status='shadow' -> status='active'` via
  `CortexStore.set_status()`.

The blocker is **data volume**, not code. Picking the promotion
threshold blind is guessing; we need >=14 days of real Surprise
Engine traffic to calibrate. Until then the shadow bucket just
accumulates drafts while we watch what accrues.

### Day 17 — Cost-weighted LTD pruning (deferred)

"Use it or lose it" auto-archival, with a hard constraint: NEVER
auto-demote `severity` based purely on violation count. The
`poly_fee_empirical` tripwire cost $500 on one occurrence; its
`violation_count` may stay low forever and still be worth keeping
critical. Safe formulation:

- A tripwire is a candidate for archival if:
  `cost_usd < 50 AND days_since_last_match > 30 AND hits_ever < 3`
- Cost-critical rows (`cost_usd >= 100`) are exempt from any
  automatic status transition, forever.
- `cortex decay --dry-run` is the default; flipping to real
  mutations requires an explicit `--apply` flag.

The goal is to keep the store compact without the silent loss of
high-value / low-frequency lessons.

### Day 18+ — Auto-mutation of YAML triggers (may never ship)

This is the Day-9 failure mode waiting to happen. DMN observes
"`r_poly_backtest` fired on a session where it was irrelevant" and
wants to add `not_any: ["summary"]`. Auto-writing to
`cortex/rules/*.yml` here is exactly how auto-regex overfit on Day 9.

If this ever ships, mutations MUST flow through `inbox/` as
diff-proposals, never direct YAML writes, and approve is human-gated
by default. A `--auto-apply` flag may exist but is off by default.
Likely verdict: the recall/precision trade-off on rule triggers is
rare enough that manual tuning during `cortex stats --sessions`
reviews is cheaper than building an auto-mutation pipeline.
