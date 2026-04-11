# Hook integration

How Cortex plugs into Claude Code's hook system.

## `.claude/settings.json`

Project-level hooks live in `.claude/settings.json` at your project root.
Create it (or merge into an existing file):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "cortex-hook"}
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {"type": "command", "command": "cortex-watch"}
        ]
      }
    ]
  }
}
```

This wires two separate entry points:

- **`cortex-hook`** runs on every user prompt submission, injects context
- **`cortex-watch`** runs after every tool call, appends audit events

Both must be on `PATH`. After `pip install -e ".[dev]"` they are installed
as console scripts under your Python's `Scripts/` (Windows) or `bin/`
(Linux/Mac) directory.

Verify:

```bash
where cortex-hook       # Windows
which cortex-hook       # Linux/Mac
```

## The hook contract

### Input (stdin, JSON)

Claude Code sends a JSON object on the hook's stdin. Relevant fields for
`UserPromptSubmit`:

```json
{
  "session_id": "a4d5a9e9-abc3-49ed-8585-fc9e9673cf66",
  "hook_event_name": "UserPromptSubmit",
  "cwd": "/c/code/BOTWA",
  "prompt": "user's message text"
}
```

For `PostToolUse`:

```json
{
  "session_id": "...",
  "hook_event_name": "PostToolUse",
  "tool_name": "Bash",
  "tool_input": { ... },
  "tool_output": { ... }
}
```

### Output (stdout, JSON)

For `UserPromptSubmit`, `cortex-hook` emits either **nothing** (no match,
silent) or a JSON payload:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "<cortex_brief>...</cortex_brief>"
  }
}
```

For `PostToolUse`, `cortex-watch` always emits nothing and returns 0.

### Exit codes

Both hooks always return **0**, even on internal errors. This is the
fail-open contract: a broken Cortex must never block Claude Code.

## Finding the database

The hook locates `.cortex/store.db` in three steps:

1. If the `CORTEX_DB` environment variable is set, use it verbatim
2. Walk up from the current working directory looking for a `.cortex/`
   folder; use `<that>/store.db`
3. Fall back to `./.cortex/store.db` (relative to CWD)

This means the hook works from any subdirectory of your project as long
as `.cortex/` exists at the project root.

## Session audit log

`cortex-watch` and `cortex-hook` both write JSONL under
`.cortex/sessions/{session_id}.jsonl`. Sample:

```jsonl
{"at": "2026-04-11T09:26:04Z", "event": "tool_call", "tool_name": "Edit"}
{"at": "2026-04-11T09:26:08Z", "event": "tool_call", "tool_name": "Bash"}
{"at": "2026-04-11T10:49:56Z", "event": "inject", "matched_rules": ["poly_directional_5m"], "tripwire_ids": ["poly_fee_empirical", "real_entry_price"], "synthesis_ids": ["pm_5m_directional_block"]}
{"at": "2026-04-11T11:02:11Z", "event": "keyword_fallback", "n_hits": 3, "tripwire_ids": ["real_entry_price", "poly_fee_empirical", "backtest_must_match_prod"], "scores": [6.0, 4.0, 3.0]}
```

### Event types

| Event | Written by | Meaning |
|---|---|---|
| `tool_call` | `cortex-watch` | Every `PostToolUse` invocation, with tool_input snippet (Day 6) |
| `inject` | `cortex-hook` | Primary rule engine match produced a brief |
| `keyword_fallback` | `cortex-hook` | Rule engine missed, TF-IDF fallback fired |
| `potential_violation` | `cortex-watch` | Tool_input matched an active tripwire's `violation_patterns` regex (Day 6) |

### Tool input logging (Day 6)

`cortex-watch` captures a 500-char summary of each tool's `tool_input`:

- **Bash**: the `command` field
- **Edit / Write / MultiEdit**: `file=<path> | old=<snippet> | new=<snippet>`
- **Read / Glob / Grep**: file_path / pattern / path only
- **Other**: JSON-serialized tool_input, truncated

The summary is logged as `input_snippet` in the `tool_call` event and
used as the substrate for silent violation detection. **Session logs may
contain code snippets** — treat `.cortex/sessions/*.jsonl` as sensitive
if your tool calls touch proprietary code.

This log is the substrate for Day-5 DMN accounting: silent-violation
detection, injection-hit-rate analysis, cold-tripwire detection.

## Environment variables

### Core

| Variable | Default | Purpose |
|---|---|---|
| `CORTEX_DB` | walk-up + `.cortex/store.db` | Absolute path to the SQLite store |
| `CORTEX_SESSIONS_DIR` | walk-up + `.cortex/sessions` | Absolute path to the session log dir |

Set these if you need Cortex to use a store outside the current project
tree (e.g. a shared store across multiple repos, or a custom layout).

### Day 7 — pre-flight verifier auto-run

`cortex-hook` can optionally execute each matched critical tripwire's
`verify_cmd` during injection, appending results to the brief. This
turns static warnings into "the bug is present in your current code
RIGHT NOW". **Disabled by default** — opt in with the env vars below.

| Variable | Default | Purpose |
|---|---|---|
| `CORTEX_VERIFY_ENABLE` | (unset) | Set to `1` to enable pre-flight verifier execution at hook time |
| `CORTEX_VERIFY_TIMEOUT` | `3` | Hard timeout in seconds per verifier command |
| `CORTEX_VERIFY_PREFIXES` | `cortex-,python -m cortex` | Comma-separated allow-list of command prefixes. Commands not matching any prefix are skipped with a `not allow-listed` marker. |
| `CORTEX_VERIFY_ALLOW_ANY` | (unset) | **DANGER** — set to `1` to disable the allow-list entirely and run any `verify_cmd`. Only use if you wrote every tripwire yourself and know every command is safe. |

Safety rules that always apply (not configurable):

- Only `critical` severity tripwires are ever considered
- Commands are parsed with `shlex.split` and executed with `shell=False`
- `stdout` truncated to 500 chars, `stderr` to 200 chars
- Any exception (timeout, OSError, parse error) results in a `skipped`
  marker — the hook never crashes, the brief still injects

**Recommended usage**: start with `CORTEX_VERIFY_ENABLE=1` only, keep the
default allow-list, write your critical tripwire verifiers as entries
under `cortex/verifiers/` with a `cortex-*` script alias, and let the
allow-list protect you from accidentally auto-running any legacy
`verify_cmd` that predates Day 7.

## Manual testing

Simulate a prompt without launching Claude Code:

```bash
echo '{"session_id":"manual-test","prompt":"your test prompt here"}' \
  | cortex-hook \
  | python -m json.tool
```

If the hook matched, you get a JSON object with
`hookSpecificOutput.additionalContext` containing the rendered brief. If
nothing matched, you get empty stdout (silent fail-open).

Simulate a tool call event:

```bash
echo '{"session_id":"manual-test","tool_name":"Bash"}' | cortex-watch
# Always exits 0 silently; inspect the session log afterward:
cat .cortex/sessions/manual-test.jsonl
```

## Troubleshooting

### The hook isn't firing at all

- Check `.claude/settings.json` syntax:

  ```bash
  python -m json.tool < .claude/settings.json
  ```

- Verify `cortex-hook` is on `PATH`: `which cortex-hook` (Linux/Mac) or
  `where cortex-hook` (Windows)
- Restart Claude Code to pick up hook configuration changes

### The hook fires but nothing gets injected

- Run the prompt through manually:

  ```bash
  echo '{"prompt":"..."}' | cortex-hook
  ```

- If that also returns nothing, no rule matched. Check triggers with
  `cortex find word1,word2,word3` to see which tripwires have those words
  in their trigger sets.
- If manual invocation returns a brief but Claude Code doesn't show it,
  the issue is Claude Code not surfacing `additionalContext`. Check the
  session transcript.

### The hook is too noisy (fires on irrelevant prompts)

- Identify the matching rule from `cortex-hook`'s output (the rule id is
  in the brief header)
- Tighten its `and_any` in `cortex/rules/*.yml`
- Re-run tests: `pytest -q`
- Consider whether the rule should be split into two narrower rules

### Session logs aren't being written

- Check directory permissions on `.cortex/sessions/`
- Run the manual test above and `ls .cortex/sessions/` afterward
- Session writes are fail-safe by design: errors are swallowed silently so
  the hook never blocks. Check `CORTEX_SESSIONS_DIR` is set correctly if
  you're using a custom path.

### Cortex is blocking my workflow

It shouldn't. Every failure path exits 0 silently. If you can reproduce a
case where a broken Cortex blocks Claude Code, **that's a bug** — file it.

Quick mitigation while debugging: delete `.claude/settings.json` or remove
the `"hooks"` key to disable Cortex entirely. Your next Claude Code prompt
will proceed normally with no Cortex involvement.
