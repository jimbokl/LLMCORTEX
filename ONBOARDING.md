# Onboarding: bootstrap Cortex on a new project

> **You are a Claude (or Claude-Code) agent reading this file because the user
> wants to install Cortex into a fresh project and seed it with lessons
> distilled from the project's existing local documentation.**
>
> Read the whole file before acting. Then follow the steps in order. The
> instructions are written so you can execute them end-to-end with no further
> clarification from the user.

---

## What Cortex actually does

Cortex is a `UserPromptSubmit` hook for Claude Code. When the user submits a
prompt, a ~60 ms Python subprocess matches the prompt against a curated store
of **tripwires** (structured lessons from past failures) and injects the
matched lessons into the agent's working context **before the agent reasons
about the task**.

The point is not RAG. The point is that the agent walks into every task
already pre-briefed on the mistakes that the team paid for in the past.
The store is small (10–50 hand-curated lessons is normal), the matching is
fast (keyword + TF-IDF body fallback), and the contract is fail-open: if
anything in Cortex breaks, the prompt path is unaffected.

Your job during onboarding is to **build the initial tripwire set** by reading
whatever documentation already exists in the project, distilling it into the
tripwire schema, and loading it into the store.

---

## Tripwire schema (read this carefully)

A tripwire is a single structured lesson. It has these fields:

| field         | type           | description                                                                 |
|---------------|----------------|-----------------------------------------------------------------------------|
| `id`          | snake_case str | Stable identifier. Lowercase, underscores, ≤32 chars. Used in CLI + logs.  |
| `title`       | str ≤80 chars  | One-line summary the agent reads first. Imperative or declarative.         |
| `severity`    | enum           | `critical` · `high` · `medium` · `low`. Critical = real money / data loss. |
| `domain`      | str            | Free tag like `auth`, `payments`, `infra`, `generic`. Used for filtering.  |
| `triggers`    | list[str]      | 3–10 keyword tokens that, when present in the prompt, make the rule fire.  |
| `body`        | str            | The lesson itself. Three short paragraphs: rule, **Why**, **How to apply**.|
| `cost_usd`    | float          | Historical cost of the incident this lesson came from. 0 if not financial. |
| `verify_cmd`  | str or null    | Optional shell command to programmatically verify compliance. Usually null.|
| `source_file` | str or null    | Path to the doc this was distilled from. For traceability.                 |

### A good tripwire body

```
Database migrations must be backwards-compatible for at least one deploy
cycle. Adding a NOT NULL column without a default will crash old workers
that are still running during a rolling deploy.

Why: incident on 2025-09-12, the `users.last_login_at` migration ran
before the workers picked up the new code. 14 minutes of 500s during
peak traffic before rollback.

How to apply: (1) Add new columns as nullable first, deploy code that
writes to them, then add the NOT NULL constraint in a follow-up migration.
(2) Never use `DROP COLUMN` in the same migration that the code change
ships in -- always two deploys apart. (3) For any migration touching
tables > 1M rows, add a runbook to the PR description.
```

This is what you are aiming for. Concrete, dated, three actionable rules,
explains the mechanism rather than the symptom.

### A bad tripwire body

```
Be careful with database migrations. They can break things.
```

Vague, no mechanism, no actionable rule. Do not produce these. If you cannot
find enough detail in the source doc to write a real one, **skip it** and tell
the user which docs you found insufficient.

---

## Bootstrap procedure

### Step 1 — Install and initialize

```bash
pip install llmcortex-agent
cortex install-skills      # one-time: copy bundled SKILL.md into ~/.claude/skills/
cd /path/to/the/new/project
cortex init                # creates .cortex/store.db
```

The store is a single SQLite file under `.cortex/store.db`. Cortex walks up
from CWD looking for it, so subcommands work from any subdirectory.

`cortex install-skills` is idempotent and runs once per machine. After it
runs, every Claude Code session — in any project — gets access to the
five Cortex skills (`cortex-bootstrap`, `cortex-capture-lesson`,
`cortex-search`, `cortex-tune`, `cortex-status`). The skills auto-activate
based on description matching; the user never has to remember a CLI
command.

### Step 2 — Inventory the project's documentation

Before writing any tripwires, **list every document that might contain a
distillable lesson**. Common sources, in priority order:

1. `CLAUDE.md` / `AGENTS.md` / `.cursorrules` — explicit agent guidance.
2. `README.md` — gotchas section, "known issues", "do not do this".
3. `docs/` and `ARCHITECTURE.md` — design constraints, hidden invariants.
4. `POSTMORTEM*.md` / `INCIDENTS.md` / `RUNBOOK*.md` — incident reports.
5. Top-level `.md` files with names like `LESSONS.md`, `PITFALLS.md`,
   `WHY_NOT.md`, `HISTORY.md`, `CHANGELOG.md` for breaking-change notes.
6. `tests/README` and test docstrings for "this test exists because…" hints.
7. Git log for `fix:` / `revert:` / `hotfix:` commits with detailed bodies.

Use `Glob` and `Grep` to find them. **Do not** read every code file — that's
not the source. Code is the *current* truth; documentation is the *history*
that explains why the code is shaped the way it is. Cortex captures history.

### Step 3 — Distill candidates

For each doc you found, extract every concrete *don't-do-this* / *gotcha* /
*lesson-learned* you can find. For each one, ask:

- **Is it a real failure mode?** "Don't forget to handle errors" is not.
  "When the upstream API returns 429, we have to wait at least 60s before
  retrying or the rate-limit window restarts" is.
- **Is it narrow enough to verify?** A good tripwire fires on a recognizable
  prompt shape. "Be careful with concurrency" is too broad.
- **Could the agent realistically encounter this in a future task?** If the
  lesson only applies to a deprecated subsystem nobody touches, skip it.

Aim for **5–20 tripwires** in the initial seed. More than 30 means you're
including noise; fewer than 5 means you didn't find enough real history and
should ask the user for more sources.

### Step 4 — Stage drafts via the inbox

Cortex has a built-in inbox workflow so the user can review your distillations
before they go live. **Always use the inbox.** Never push tripwires straight
into the store from an automated pass — the user must approve each one.

For each candidate, write a draft JSON file under `.cortex/inbox/`:

```bash
mkdir -p .cortex/inbox
```

Each draft is a JSON file with this shape:

```json
{
  "draft_id": "draft_001_migration_safety",
  "source": "agent_onboarding",
  "created_at": "2026-04-11T12:00:00+00:00",
  "draft": {
    "id": "migration_backwards_compat",
    "title": "Migrations must stay backwards-compatible across one deploy",
    "severity": "high",
    "domain": "infra",
    "triggers": ["migration", "alter", "schema", "rolling", "deploy", "drop"],
    "body": "Database migrations must be backwards-compatible for at least one deploy cycle...\n\nWhy: incident on 2025-09-12...\n\nHow to apply: (1) Add new columns as nullable first... (2) Never use DROP COLUMN in the same migration... (3) For any migration touching tables > 1M rows...",
    "cost_usd": 0.0,
    "verify_cmd": null,
    "source_file": "docs/postmortems/2025-09-12-migration-incident.md"
  }
}
```

When you've staged everything, tell the user:

```
Staged N drafts in .cortex/inbox/ from M source documents.
Review with:    cortex inbox list
                cortex inbox show <draft_id>
Approve with:   cortex inbox approve <draft_id>
Reject with:    cortex inbox reject <draft_id>
```

Wait for the user to approve. **Do not approve drafts on the user's behalf**
unless they explicitly tell you to.

### Step 5 — Wire the hooks

Once at least one tripwire is in the store, install the Claude Code hooks.
Add to `.claude/settings.json` (create the file if it doesn't exist):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "cortex-hook" }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "cortex-watch" }] }
    ]
  }
}
```

`cortex-hook` is the read-before-you-reason injection. `cortex-watch` is the
passive PostToolUse audit logger that feeds the Phase-0 fitness metrics.

### Step 6 — Smoke-test the install

Run all of these and confirm none of them error:

```bash
cortex list                              # show approved tripwires
cortex stats                             # store-level counts
cortex stats --sessions                  # audit-log analysis (empty at first)
cortex bench --no-subprocess             # confirm 60-ms classify path
```

If `cortex list` is empty, the user hasn't approved any drafts yet — go back
to Step 4 and remind them.

Then ask the user to submit one prompt that should match a tripwire trigger,
and verify the brief appears. If it doesn't, run:

```bash
cortex find <one_of_the_trigger_words>
```

to confirm the tripwire is searchable.

---

## Decision tree: should this become a tripwire?

```
Is the lesson tied to a specific past failure?
├── No  → Skip. Cortex stores history, not opinions.
└── Yes → Can you state a concrete rule?
          ├── No  → Skip. Vague lessons are noise.
          └── Yes → Will an agent realistically encounter this in
                    future tasks (look at the project's typical work)?
                    ├── No  → Skip. Dead-code lessons rot.
                    └── Yes → Write the tripwire. Use the schema strictly.
```

---

## What Cortex is NOT for

- **Style preferences** (`use 2-space indents`, `no semicolons`). Use the
  linter / `.editorconfig` / `CLAUDE.md` instead.
- **Project setup instructions** (`run npm install first`). Use README.
- **Architectural overview** (`auth lives in src/auth/`). The agent will
  read the code; don't bloat the brief.
- **Things you wish were true but never enforced**. If the team routinely
  breaks the rule, the brief won't fix that — better tooling will.

A good rule of thumb: if the lesson is something a senior engineer would
**stop a code review for**, it belongs in Cortex. If it's something they'd
sigh about and let through, it doesn't.

---

## After the seed: Phase 0 fitness scoring

Cortex tracks per-tripwire fitness automatically from the audit log. After
a few days of real use, run:

```bash
cortex stats --sessions
```

Look at the **Tripwire composite fitness** section. Tripwires with strongly
negative fitness (high `ignored`, frequent next-prompt frustration) are
candidates for rewording or depreciation. Tripwires with high `caught`
counts are doing real work.

This is the mechanism by which Cortex audits itself. You don't need to
manually label anything — the metric is derived from implicit signals
(`potential_violation` events, `<cortex_predict>` failure-mode token
overlap, prompt frustration regex). See `cortex/fitness.py` for the
formula and weights.

---

## When to ask the user for help

- The project has zero documentation files of any kind. Ask where the
  team's tribal knowledge lives (Slack? Notion? Linear postmortems?
  PR descriptions?). Don't fabricate tripwires from code alone.
- A doc references a system the user must explain (private dashboards,
  internal services, vendors). Ask before you guess at semantics.
- You distilled fewer than 5 tripwires from the available docs. Tell the
  user the seed is too thin and ask for more sources.

---

## End-of-onboarding checklist

Before you tell the user "done", verify:

- [ ] `cortex init` ran successfully
- [ ] All draft JSON files under `.cortex/inbox/` validate (use
      `cortex inbox list` and confirm every row is `READY` not `MISSING`)
- [ ] The user has been told what to review and how
- [ ] `.claude/settings.json` has both hooks wired
- [ ] `cortex bench --no-subprocess` reports classify p50 under 100 ms
- [ ] You have NOT approved any drafts on the user's behalf
- [ ] You have NOT committed any of the user's confidential incident details
      to a public location (the store is local; that's fine)

When all boxes are ticked, summarize: how many drafts you staged, which
source files you read, and which sources (if any) you found insufficient.
