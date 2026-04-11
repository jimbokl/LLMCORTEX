---
name: cortex-search
description: Use this skill when you (the agent) want to actively check what Cortex knows about a topic before committing to an approach, or when the user asks "what does cortex know about X", "any tripwires for Y", "show me past lessons about Z", "есть ли правило про …", "проверь cortex по этой теме". This is the read-side counterpart to cortex-capture-lesson — query the store, do not write to it.
---

# Search Cortex for relevant lessons

You want to know what Cortex already has on a topic. This is the read-only
side of Cortex: nothing in the store changes, no drafts are staged.

## When to use this proactively (without being asked)

Even if the user did not explicitly ask "search cortex", reach for this
skill when:

- You are about to commit to an approach for a non-trivial task and want
  to verify there is no past lesson that contradicts it.
- The user mentioned a domain (auth, payments, migrations, deploys,
  caching, retries, fees, …) where past failures are likely.
- The cortex hook brief was empty or thin and the task feels risky —
  the rule engine may have missed; do a manual lookup before acting.

The goal is not to second-guess the hook on every task. It is to catch
the cases where the hook silently missed.

## Procedure

1. **Pick 1–5 keyword tokens** that describe the topic. Lowercase, single
   words or short bigrams. Example: for "should I add a retry around
   this stripe webhook" → `stripe webhook retry idempotency`.

2. **Search by triggers**:
   ```bash
   cortex find <comma_separated_words>
   ```
   This matches the `triggers` field of every tripwire. Returns id,
   severity, title.

3. **If `find` returns nothing**, the rule engine has no exact trigger
   match. Try a broader related term. If still nothing, the topic is
   genuinely uncovered — note it and proceed cautiously.

4. **Read the full body** of any interesting hit:
   ```bash
   cortex show <tripwire_id>
   ```
   This dumps title, severity, cost, body, triggers, source file.

5. **Decide whether to act on it**. The body has a `Why:` and a
   `How to apply:` section. If the lesson is relevant to your current
   task, follow the `How to apply:` rules.

6. **Tell the user what you found** in one or two sentences. Example:
   "Cortex has a tripwire `migration_drop_column_blocks_workers` that
   says we need a two-deploy split for any DROP COLUMN. I'll structure
   the migration that way."

## Hard rules

- **Read-only.** Do not run `cortex add`, `cortex inbox approve`,
  `cortex migrate`, or any other write command.
- **Do not paste the full tripwire body** into your response unless the
  user asks. Summarize. The body is for your reasoning, not for the
  user's screen.
- **If a tripwire seems wrong or out of date**, do NOT silently skip
  it. Tell the user explicitly: "Cortex says X but the current code
  shows Y, which one is right?" Stale tripwires are a known failure
  mode and the user needs to know.

## Done criteria

You have either:
- Found one or more relevant tripwires and let them inform your
  approach (and told the user which ones), OR
- Confirmed via 2+ different keyword searches that Cortex has nothing
  on the topic, and proceeded with explicit awareness of that gap.
