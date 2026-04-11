"""Silent violation detection: match tool calls against `violation_patterns`
of tripwires that were injected earlier in the same session.

The use case: Cortex injected `lookahead_parquet` at prompt time, the agent
saw the brief, and then proceeded to run a Bash command that matches the
lookahead regex. That's a silent violation -- the lesson was shown and
ignored. Day-5 stats only counted how often tripwires were INJECTED;
Day-6 counts how often they were APPLIED vs IGNORED.

Scope notes:
- Detection is pattern-based, not semantic. If a tripwire cannot be
  encoded as a regex on tool_input, it simply never detects violations.
  This is fine -- we'd rather miss ambiguous cases than false-positive.
- Only tripwires that appeared in `inject` or `keyword_fallback` events
  earlier in the same session are considered "active". New tripwires
  don't trigger retroactively.
- One violation per tripwire per tool call. If a single tool call matches
  three patterns of the same tripwire, we still log one potential
  violation for that tripwire.
"""
from __future__ import annotations

import re
from typing import Any

from cortex.classify import find_db
from cortex.session import read_session
from cortex.store import CortexStore


def get_active_tripwires(
    session_id: str,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return tripwires that were injected or keyword-fallback-matched
    earlier in this session AND have at least one violation pattern.
    Tripwires without patterns are silently skipped (we can't detect
    violations for them)."""
    events = read_session(session_id)
    if not events:
        return []

    active_ids: set[str] = set()
    for event in events:
        ev_type = event.get("event", "")
        if ev_type in ("inject", "keyword_fallback"):
            for tw_id in event.get("tripwire_ids") or []:
                active_ids.add(tw_id)
    if not active_ids:
        return []

    store = CortexStore(db_path or find_db())
    try:
        out: list[dict[str, Any]] = []
        for tw_id in active_ids:
            tw = store.get_tripwire(tw_id)
            if tw and tw.get("violation_patterns"):
                out.append(tw)
        return out
    finally:
        store.close()


def detect_violations(
    session_id: str,
    tool_name: str,
    tool_input_snippet: str,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Scan `tool_input_snippet` against active tripwires' violation patterns.

    Returns a list of violation descriptors, one per matched tripwire:
        {
            "tripwire_id": str,
            "tool_name": str,
            "pattern": str,
            "snippet": str (truncated),
        }

    Never raises. Empty snippet or no active tripwires -> empty list.
    """
    if not tool_input_snippet or not session_id:
        return []

    try:
        active = get_active_tripwires(session_id, db_path=db_path)
    except Exception:
        return []
    if not active:
        return []

    violations: list[dict[str, Any]] = []
    for tw in active:
        patterns = tw.get("violation_patterns") or []
        for pat_str in patterns:
            try:
                pat = re.compile(pat_str)
            except re.error:
                continue
            if pat.search(tool_input_snippet):
                violations.append({
                    "tripwire_id": tw["id"],
                    "tool_name": tool_name,
                    "pattern": pat_str,
                    "snippet": tool_input_snippet[:200],
                })
                break  # one violation per tripwire is enough
    return violations


def summarize_tool_input(tool_name: str, tool_input: dict | None) -> str:
    """Extract a compact, loggable summary from the Claude Code tool_input
    JSON object. Truncates at 500 chars to keep session logs manageable
    and avoid accidentally storing huge pastes.

    The summary is what detection regexes see. If a pattern can't match
    against the summary, it can't detect that kind of violation -- this
    is a trade-off for privacy and log size.
    """
    if not isinstance(tool_input, dict):
        return ""
    try:
        if tool_name == "Bash":
            return (tool_input.get("command") or "")[:500]
        if tool_name in ("Edit", "Write", "MultiEdit"):
            parts = []
            fp = tool_input.get("file_path")
            if fp:
                parts.append(f"file={fp}")
            old = tool_input.get("old_string")
            if old:
                parts.append(f"old={old[:200]}")
            new = tool_input.get("new_string") or tool_input.get("content")
            if new:
                parts.append(f"new={new[:200]}")
            return " | ".join(parts)[:500]
        if tool_name in ("Read", "Glob", "Grep"):
            # Low-risk tools: log path/pattern only
            parts = []
            for key in ("file_path", "pattern", "path"):
                val = tool_input.get(key)
                if val:
                    parts.append(f"{key}={val}")
            return " | ".join(parts)[:500]
        # Generic fallback: JSON-serialize and truncate
        import json as _json
        return _json.dumps(tool_input, ensure_ascii=False)[:500]
    except Exception:
        return ""
