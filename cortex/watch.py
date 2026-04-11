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

        from cortex.session import log_event, read_session
        from cortex.violation_detect import detect_violations, summarize_tool_input

        tool_input = payload.get("tool_input")
        snippet = summarize_tool_input(tool_name, tool_input)
        response_snippet = _summarize_tool_response(payload.get("tool_response"))

        # Day 14: Surprise Engine. Scan the CURRENT agent turn (every
        # assistant message since the last real human user prompt) for
        # a <cortex_predict> block and log it as a `prediction` event
        # BEFORE the `tool_call` event so the `cortex surprise`
        # collector can pair them by forward-scan.
        #
        # We cannot just look at the last assistant message: the agent
        # often emits predict in a text-only preamble and only issues
        # tool_use in a LATER message of the same turn. The original
        # `read_last_assistant_text` path missed every such case and
        # left the Surprise Engine starved of data (Day 14 bug, found
        # 2026-04-11 during live validation).
        #
        # De-duplicated against the most recent prediction in the
        # session log to avoid multi-tool_use messages writing N copies.
        try:
            from cortex.surprise import parse_prediction, read_last_prediction_text

            transcript_path = payload.get("transcript_path")
            assistant_text = read_last_prediction_text(transcript_path)
            prediction = parse_prediction(assistant_text)
            if prediction is not None and not _already_logged(
                session_id, read_session, prediction
            ):
                log_event(
                    session_id,
                    "prediction",
                    {
                        "outcome": prediction["outcome"],
                        "failure_mode": prediction["failure_mode"],
                    },
                )
        except Exception:
            pass

        log_event(
            session_id,
            "tool_call",
            {
                "tool_name": tool_name,
                "input_snippet": snippet,
                "response_snippet": response_snippet,
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


def _summarize_tool_response(response: object, max_len: int = 500) -> str:
    """Render tool_response as a compact single-line snippet for audit.

    PostToolUse payloads vary by tool. We try a few common shapes:
      * dict with `stdout` / `stderr` (Bash)
      * dict with `content` / `text` (Read / editors)
      * plain string
      * anything else -> JSON dump, truncated

    Fail-safe: any exception returns an empty string.
    """
    try:
        if response is None:
            return ""
        if isinstance(response, str):
            return _truncate(response, max_len)
        if isinstance(response, dict):
            for key in ("stdout", "text", "content", "output"):
                val = response.get(key)
                if isinstance(val, str) and val:
                    return _truncate(val, max_len)
            return _truncate(json.dumps(response, ensure_ascii=False), max_len)
        return _truncate(str(response), max_len)
    except Exception:
        return ""


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _already_logged(
    session_id: str,
    read_session_fn,  # type: ignore[no-untyped-def]
    prediction: dict,
) -> bool:
    """Return True if the most recent prediction in the session log
    already matches `prediction`. Prevents N copies of the same block
    when one assistant message contains multiple tool_use calls.
    """
    try:
        events = read_session_fn(session_id)
    except Exception:
        return False
    # Walk backwards; consider only the latest prediction event.
    for event in reversed(events):
        if event.get("event") != "prediction":
            continue
        return (
            event.get("outcome", "") == prediction.get("outcome", "")
            and event.get("failure_mode", "") == prediction.get("failure_mode", "")
        )
    return False


if __name__ == "__main__":
    sys.exit(main())
