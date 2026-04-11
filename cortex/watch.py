"""Claude Code PostToolUse hook entry point.

Reads the hook JSON payload from stdin and appends a `tool_call` event to
the session log. Never modifies agent behavior -- this is passive audit
logging for Day-4 DMN accounting.

Install by adding to `.claude/settings.json`:

    {
      "hooks": {
        "PostToolUse": [
          {"hooks": [{"type": "command", "command": "cortex-watch"}]}
        ]
      }
    }

Fails open: any error results in exit 0 / empty output so that a broken
cortex never blocks tool execution.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        session_id = (payload.get("session_id") or "").strip()
        tool_name = (payload.get("tool_name") or "").strip()
        if not session_id or not tool_name:
            return 0

        from cortex.session import log_event
        from cortex.violation_detect import detect_violations, summarize_tool_input

        tool_input = payload.get("tool_input")
        snippet = summarize_tool_input(tool_name, tool_input)

        log_event(
            session_id,
            "tool_call",
            {
                "tool_name": tool_name,
                "input_snippet": snippet,
            },
        )

        # Silent violation detection: match the snippet against active
        # tripwires in this session. One event per matched tripwire.
        try:
            violations = detect_violations(session_id, tool_name, snippet)
            for v in violations:
                log_event(session_id, "potential_violation", v)
        except Exception:
            pass

        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
