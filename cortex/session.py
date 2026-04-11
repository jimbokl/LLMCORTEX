"""Session audit log: jsonl files under `.cortex/sessions/`.

Each hook invocation (inject, tool_call) appends one JSON line to the session
log. The session log is the raw substrate for Day-4 DMN accounting: silent
violation detection and injection-hit-rate analysis will read from here.

Fail-safe: any IO error during logging is swallowed so that hooks remain
fail-open.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_session_id(session_id: str) -> str:
    """Strip anything that is not alnum / dash / underscore. Bound length."""
    return "".join(c for c in session_id if c.isalnum() or c in "-_")[:80]


def sessions_dir() -> Path:
    """Locate (or create) the sessions directory.

    Resolution order:
      1. `$CORTEX_SESSIONS_DIR` env var
      2. Walk up from CWD looking for a `.cortex/` folder; use `.cortex/sessions`
      3. Fall back to `.cortex/sessions` under CWD
    """
    env = os.environ.get("CORTEX_SESSIONS_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".cortex").exists():
            target = parent / ".cortex" / "sessions"
            target.mkdir(parents=True, exist_ok=True)
            return target
    fallback = cwd / ".cortex" / "sessions"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def log_event(session_id: str, event_type: str, data: dict[str, Any]) -> bool:
    """Append an event to the session log. Returns False silently on any error."""
    try:
        if not session_id:
            return False
        safe_id = _sanitize_session_id(session_id)
        if not safe_id:
            return False
        log_path = sessions_dir() / f"{safe_id}.jsonl"
        event: dict[str, Any] = {"at": _now_iso(), "event": event_type}
        event.update(data)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_session(session_id: str) -> list[dict[str, Any]]:
    """Read all events for a session. Returns empty list on any error."""
    try:
        safe_id = _sanitize_session_id(session_id)
        if not safe_id:
            return []
        log_path = sessions_dir() / f"{safe_id}.jsonl"
        if not log_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
    except Exception:
        return []
