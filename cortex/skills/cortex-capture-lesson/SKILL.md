---
name: cortex-capture-lesson
description: Use this skill when the user describes an incident, bug, or hard-won lesson and wants it captured so the agent will see it on future tasks. Triggers include "we just lost X", "post-mortem", "don't ever do this again", "remember this", "запомни этот урок", "зафиксируй этот баг", "make sure cortex catches this next time", "this cost us N hours", or any narrative that has the shape (failure → root cause → rule). Stage the result as a draft in cortex inbox; never auto-approve.
---

# Capture a lesson into Cortex

The user just described a real failure / incident / hard-won insight. Your
job is to distill it into a single tripwire and stage it as a draft in
`.cortex/inbox/` so the user can approve it.

## Tripwire schema (the one you must produce)

| field         | required | value                                                            |
|---------------|----------|------------------------------------------------------------------|
| `id`          | yes      | snake_case, ≤32 chars, descriptive (e.g. `migration_drop_column`)|
| `title`       | yes      | imperative one-liner ≤80 chars                                   |
| `severity`    | yes      | `critical` (data/money loss) · `high` · `medium` · `low`         |
| `domain`      | yes      | short tag: `auth`, `db`, `infra`, `payments`, `generic`, …       |
| `triggers`    | yes      | 3–10 lowercase keyword tokens an agent prompt will contain       |
| `body`        | yes      | three short paragraphs: rule, **Why:**, **How to apply:**        |
| `cost_usd`    | optional | financial cost of the past incident; 0 if non-financial          |
| `verify_cmd`  | optional | shell command that fails if the rule is violated; usually null   |
| `source_file` | optional | path to the doc / chat / PR where this came from                 |

## Body template (follow this exactly)

```
<one-sentence rule statement>.

Why: <what happened, when, what broke, what it cost>.

How to apply: (1) <concrete action>. (2) <concrete action>. (3) <edge case>.
```

The `Why:` paragraph is the most important — it's what makes the lesson
non-vague. If the user has not given you enough detail to write a real
`Why:`, **ask them for it before producing the draft**. Do not fabricate
context to fill the slot.

## Procedure

1. **Read the user's narrative carefully.** Identify: the failure mode,
   the root cause, the actionable rule, the cost (if any).
2. **Extract trigger keywords.** What words would appear in a future
   prompt that this lesson should fire on? Be specific — `migration`
   plus `drop` is better than `database` alone. 3–10 tokens, lowercase.
3. **Pick a severity.**
   - `critical` — real money loss, data loss, security breach, prod outage
   - `high` — significant time loss, hard-to-debug class of bug
   - `medium` — annoyance, recurring rework, hidden gotcha
   - `low` — minor cleanup, style-adjacent
4. **Pick a stable id.** snake_case, descriptive, no version numbers.
   Bad: `bug_fix_42`. Good: `migration_drop_column_blocks_workers`.
5. **Stage the draft** as a JSON file in `.cortex/inbox/`:
   ```bash
   mkdir -p .cortex/inbox
   ```
   The file shape:
   ```json
   {
     "draft_id": "draft_<short_unique>",
     "source": "agent_capture",
     "created_at": "<ISO 8601 UTC>",
     "draft": {
       "id": "...",
       "title": "...",
       "severity": "...",
       "domain": "...",
       "triggers": ["...", "..."],
       "body": "...\n\nWhy: ...\n\nHow to apply: (1) ... (2) ... (3) ...",
       "cost_usd": 0.0,
       "verify_cmd": null,
       "source_file": "<chat | path | null>"
     }
   }
   ```
6. **Tell the user** the draft id and how to review:
   ```
   cortex inbox show <draft_id>
   cortex inbox approve <draft_id>     # or reject
   ```

## Hard rules

- **Never approve** the draft yourself. Inbox is the human gate.
- **Never invent the Why:**. If the user did not provide enough detail,
  ask. Wrong is worse than missing.
- **One incident → one tripwire.** Do not bundle multiple unrelated
  lessons into one draft. If the user describes two failures, stage two
  drafts.
- **Do not add a verify_cmd unless the user explicitly asks.** Verifiers
  run on every matching prompt; a wrong one is a footgun.
- **Use the existing schema fields only.** Do not invent new fields.

## Done criteria

- A draft JSON file exists in `.cortex/inbox/`
- `cortex inbox show <draft_id>` shows status `READY` (no missing
  fields, no `TODO` placeholders)
- The user has been told the draft id and how to approve
