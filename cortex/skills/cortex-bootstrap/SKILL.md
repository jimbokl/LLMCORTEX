---
name: cortex-bootstrap
description: Use this skill when the user wants to install Cortex into a new project, seed it with lessons from local documentation, or set up the active-memory hook for the first time. Triggers include "install cortex", "set up cortex", "bootstrap cortex", "обучи cortex на этом проекте", "seed cortex from local docs", "wire cortex hooks". Do NOT use for adding individual lessons to an already-bootstrapped project — use cortex-capture-lesson for that.
---

# Bootstrap Cortex on a new project

You are setting up Cortex (active-memory hook for Claude Code) on a project
that does not yet have it. Your goal is to install the package, initialize
the store, distill lessons from the project's existing documentation into
tripwires, stage them via the inbox workflow, and wire the hooks.

The full procedure lives in [ONBOARDING.md](../../../ONBOARDING.md) at the
repo root. **Read it before acting** — it has the tripwire schema, the
decision tree for what counts as a tripwire, and the end-of-onboarding
checklist.

## Compressed step list

1. `pip install llmcortex-agent` (skip if already installed)
2. `cd` to the project root, run `cortex init` to create `.cortex/store.db`
3. Inventory documentation in this priority order:
   - `CLAUDE.md` / `AGENTS.md` / `.cursorrules`
   - `README.md` (gotchas / known issues sections)
   - `docs/`, `ARCHITECTURE.md`
   - `POSTMORTEM*.md`, `INCIDENTS.md`, `RUNBOOK*.md`
   - `LESSONS.md`, `PITFALLS.md`, `WHY_NOT.md`
   - `tests/README` and test docstrings explaining "this exists because…"
4. Distill 5–20 candidate tripwires from those docs. **Quality over quantity.**
   Each must have: a concrete failure mode, a why-it-happened, and 1–3
   actionable rules. Vague advice → skip.
5. Stage each candidate as a JSON draft in `.cortex/inbox/` (see
   ONBOARDING.md for the exact JSON shape). **Do not approve drafts on
   the user's behalf.**
6. Wire hooks in `.claude/settings.json`:
   ```json
   {
     "hooks": {
       "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "cortex-hook"}]}],
       "PostToolUse":      [{"hooks": [{"type": "command", "command": "cortex-watch"}]}]
     }
   }
   ```
7. Tell the user how many drafts you staged, which sources you used, and
   how to review/approve: `cortex inbox list`, `cortex inbox show <id>`,
   `cortex inbox approve <id>`.

## Hard rules

- **Never auto-approve drafts.** Inbox is the human gate. The user must
  approve each tripwire before it goes live.
- **Never fabricate tripwires from code alone.** If documentation is
  insufficient, tell the user and ask where the project's tribal
  knowledge lives (Slack threads, Notion pages, PR descriptions).
- **Never copy domain-specific tripwires from another project's store**
  unless the user explicitly says the projects share infrastructure.
- **Never commit `.cortex/store.db` or `.cortex/sessions/` to git** —
  these contain the user's incident history. They should already be in
  `.gitignore` after `cortex init`; verify it.

## Edge cases

- **Empty docs**: project has no markdown documentation. Ask the user
  for sources before staging anything. Do not invent.
- **Already bootstrapped**: `.cortex/store.db` already exists with
  tripwires inside. Run `cortex list` first to see what's there. Do not
  re-run `migrate` without user consent — it preserves stats but may
  add unwanted defaults.
- **Mono-repo**: ask whether Cortex should live at the monorepo root
  (one shared store) or per-package (multiple stores). Default to root.

## Done criteria

The skill is finished when the user has:
1. A `.cortex/store.db` with at least one approved tripwire
2. Both hooks wired in `.claude/settings.json`
3. A list of which drafts are staged for their review
4. `cortex bench --no-subprocess` reporting classify p50 < 100 ms
