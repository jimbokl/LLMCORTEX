---
name: cortex-tune
description: Use this skill when the user wants to audit the health of their Cortex tripwires, find lessons that are not pulling their weight, or propose tuning for rules that fire too often / never / get ignored. Triggers include "cortex tuning", "which tripwires aren't working", "cortex health", "найди слабые правила", "почисти cortex", "any tripwires we should retire", "cortex maintenance". Read the Phase-0 fitness scores; propose changes via inbox; never depreciate or delete on your own.
---

# Tune the Cortex tripwire set using Phase-0 fitness data

Phase 0 of Cortex tracks per-tripwire fitness from implicit signals in
the audit log: `caught` (warned, agent obeyed), `ignored` (warned, agent
violated anyway), `surprise_ok` (the agent's `<cortex_predict>` failure
mode matched the body), `frustration` (the next user prompt scored high
on corrective language), and `cost_weight` (log-dampened cost_usd).

Composite formula:
```
fitness = +1.0*caught -2.0*ignored +0.5*surprise -0.3*frustration + cost_weight
```

Strongly negative fitness = the rule is doing harm or being ignored.
Cold tripwires (zero hits in the window) = the trigger words don't match
real prompts. High `ignored` = the agent reads the brief and does the
forbidden thing anyway, which usually means the body is too vague.

## Procedure

1. **Pull the fitness report**:
   ```bash
   cortex stats --sessions --days 30
   ```
   Look at the **Tripwire composite fitness** section. Note three
   buckets:
   - **Negative fitness**: rules where `ignored > caught/2`. Candidates
     for rewording or depreciation.
   - **Cold (never matched)**: rules listed under "Cold tripwires".
     Trigger words are wrong; the rule never fires.
   - **High `ignored` rate (≥0.5)**: rules listed in "Tripwire
     effectiveness" with `[FAIL]` or `[WARN]`. The brief is being
     ignored — the body needs to be more directive or a verifier
     should be added.

2. **For each candidate**, run `cortex show <id>` to see the full body.
   Read the `body`, `triggers`, `severity`, `cost_usd`. Form a hypothesis
   for **why** the metric looks bad. Common patterns:
   - Trigger words are too narrow (rule never fires) → widen `triggers`
   - Trigger words are too broad (rule fires on irrelevant prompts and
     gets ignored) → tighten `triggers` or split into two rules
   - Body is vague ("be careful") → rewrite as imperative `How to apply:`
   - Lesson is genuinely obsolete (the underlying code/system is gone)
     → recommend depreciation via `cortex status <id> archived`

3. **Stage proposals as inbox drafts** (not direct edits). For each
   candidate, write a draft JSON with the proposed new triggers / body
   and a comment explaining the reasoning. The user reviews and
   approves.

4. **Summarize for the user** in one short table:
   ```
   tripwire_id              fit    issue                 proposal
   migration_drop_column   -2.4    high ignored rate     rewrite body
   foo_old_endpoint        -1.0    cold (never matched)  archive
   bar_too_broad           -0.5    fires on noise        narrow triggers
   ```

## Hard rules

- **Never run `cortex status <id> archived` yourself.** Depreciation
  is a write to the canonical store; only the user does it.
- **Never edit a tripwire body directly via `cortex add` overwrite.**
  Stage the proposed body in an inbox draft and let the user approve.
- **Never tune a rule with fewer than 5 total hits.** The fitness signal
  is too noisy at low N. Tell the user the rule needs more data.
- **Never delete `.cortex/sessions/*.jsonl` files.** The audit log is
  the source of truth for fitness. If it gets corrupted, the user can
  decide to rotate it; you cannot.

## Edge cases

- **Brand-new install**: less than a week of audit data. Fitness is
  meaningless. Tell the user "come back after 7+ days of real use".
- **All fitness positive**: no candidates. Report cleanly: "current
  tripwire set is healthy, N rules with positive fitness, no rules
  meeting tuning thresholds. Re-check in 1–2 weeks."
- **Conflict with cost_usd**: a high-cost tripwire with `ignored > 0`
  is the most expensive failure mode. Flag it explicitly as priority
  #1 even if the composite fitness is still positive.

## Done criteria

You have produced either:
- A short table of N tuning candidates with proposed fixes, plus draft
  JSON files in `.cortex/inbox/` for each proposal, OR
- A clean health report saying nothing needs tuning right now and
  why (which thresholds were checked).
