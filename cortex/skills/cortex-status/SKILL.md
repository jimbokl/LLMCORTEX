---
name: cortex-status
description: Use this skill at the start of any session in a project where Cortex may be installed, to quickly verify the install is healthy and the active-memory hook is actually working. Triggers include "is cortex working here", "cortex status", "cortex health check", "проверь cortex", "cortex installed?", or any new-project orientation pass where you (the agent) want to know whether Cortex is part of the toolchain. Read-only audit, no writes.
---

# Cortex health check

You are checking whether Cortex is installed and functional in the current
project. This is the orientation skill — a fast read-only audit so you
know what you're working with before you reason about the codebase.

## Six checks (run them all)

### 1. Package installed?
```bash
cortex --help
```
If this errors with "command not found", Cortex is not installed in
this environment. Tell the user `pip install llmcortex-agent`.

### 2. Store exists?
```bash
cortex list
```
If "(no tripwires)" → store is initialized but empty. Bootstrap is
incomplete. Suggest the cortex-bootstrap skill.
If error about missing `.cortex/store.db` → run `cortex init` first.
Otherwise: note the count and severity distribution.

### 3. Hooks wired?
Read `.claude/settings.json` (if it exists). Look for:
- `hooks.UserPromptSubmit` containing `cortex-hook`
- `hooks.PostToolUse` containing `cortex-watch`

If both are missing, the active-memory layer is silent — tell the
user. If only `cortex-hook` is wired without `cortex-watch`, the audit
log won't get tool_call events and Phase-0 fitness will be useless.

### 4. Recent activity?
```bash
cortex stats --sessions --days 7
```
Look at:
- `Sessions:` count — anything above 0 means the hook fired recently
- `Sessions with inject:` — primary rule engine matches
- `Sessions with fallback:` — TF-IDF body fallback matches
- `Silent violations detected:` — any agent ignoring briefs

If sessions = 0 over 7 days, either the hooks aren't wired or the
project is dormant. Cross-check with #3.

### 5. Latency healthy?
```bash
cortex bench --no-subprocess
```
Look at the classify p50. Anything under 100 ms is healthy. Anything
above 200 ms means the rule engine has bloated YAML or the store is
huge — flag it.

### 6. Phase-0 fitness signals?
The `cortex stats --sessions` output above also has a "Tripwire
composite fitness" section. Skim it. Look for:
- Strongly negative rows (candidates for tuning — see cortex-tune)
- Cold tripwires (never matched in window)
- High `ignored` rate on critical rules (these are the most dangerous
  failure mode — agent reads the brief and ignores it)

## Output format

Give the user a single short summary table:

```
cortex health
─────────────
package:    installed (v0.1.0)
store:      12 tripwires (3 critical, 5 high, 4 medium)
hooks:      cortex-hook ✓   cortex-watch ✓
sessions:   8 in last 7 days
injects:    14 primary + 6 fallback
violations: 1 ignored (lookahead_parquet)
latency:    classify p50 = 42 ms
fitness:    9 positive, 2 candidates for tuning
```

If anything is wrong, end with one or two sentences pointing the user
at the right next skill (cortex-bootstrap if not installed,
cortex-tune if rules need maintenance, etc).

## Hard rules

- **Read-only.** Do not run `cortex init`, `cortex add`, `cortex
  migrate`, `cortex inbox approve`, or any write command. The user
  decides if they want changes; you only report.
- **Do not edit `.claude/settings.json`** even if the hooks are
  unwired. Tell the user the exact JSON to add and let them paste it.
- **Do not modify `.cortex/sessions/*`** for any reason.

## Done criteria

The user has the six-line summary table and a clear next action
(or confirmation that everything is healthy and no action is needed).
