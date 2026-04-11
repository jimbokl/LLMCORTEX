"""Draft tripwire inbox: the human-approval step between "I found something
that might be worth a tripwire" and "it's in the store firing at hook time".

Drafts live as JSON files under `.cortex/inbox/*.json`. Each file is one
pending draft. The CLI (`cortex inbox list/show/approve/reject`) reads and
mutates this directory; no SQLite is involved.

The inbox exists because automatic drawer-to-tripwire promotion would
dilute the curated signal. Palace semantic search finds *many* plausible
lessons; only a small fraction are true tripwires (specific past failure
+ quantifiable cost + actionable "how to apply"). The inbox forces a
review step:

    Palace query  -->  cortex import-palace --to-inbox  -->  draft file
                                                              |
                                                              v
                   editor (fix TODO placeholders, triggers)  <-+
                                                              |
                                                              v
                          cortex inbox approve <id>  -->  store.add_tripwire

Future Day-9 DMN (Haiku reflection loop) will write to the same inbox,
keeping the human-approval step intact while automating the discovery.

Fail-safe: all I/O errors are swallowed so the hook path never sees
exceptions from inbox code. The inbox is opt-in and non-critical.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _sanitize_id(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:80]


def inbox_dir() -> Path:
    """Locate (or create) the inbox directory.

    Resolution order:
      1. `$CORTEX_INBOX_DIR` env var
      2. Walk up from CWD looking for a `.cortex/` folder
      3. Fall back to `.cortex/inbox` under CWD
    """
    env = os.environ.get("CORTEX_INBOX_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".cortex").exists():
            target = parent / ".cortex" / "inbox"
            target.mkdir(parents=True, exist_ok=True)
            return target
    fallback = cwd / ".cortex" / "inbox"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def write_draft(
    draft: dict[str, Any],
    source: str = "manual",
    draft_id: str | None = None,
) -> str | None:
    """Write a draft to the inbox. Returns the draft_id, or None on failure.

    If `draft_id` is not provided, one is generated as `{source}_{timestamp}`.
    An existing draft with the same id is overwritten (idempotent — useful
    when the same Palace query is re-run).
    """
    try:
        if draft_id is None:
            # uuid suffix guarantees uniqueness across rapid-fire invocations
            # (e.g. `import-palace --to-inbox` staging multiple hits in the
            # same second)
            draft_id = (
                f"{_sanitize_id(source)}_{_now_compact()}_{uuid.uuid4().hex[:6]}"
            )
        else:
            draft_id = _sanitize_id(draft_id)
        if not draft_id:
            return None

        path = inbox_dir() / f"{draft_id}.json"
        payload = {
            "draft_id": draft_id,
            "created_at": _now_iso(),
            "source": source,
            "draft": draft,
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return draft_id
    except Exception:
        return None


def list_drafts() -> list[dict[str, Any]]:
    """Return all drafts in the inbox, sorted by created_at ascending."""
    out: list[dict[str, Any]] = []
    try:
        root = inbox_dir()
    except Exception:
        return out
    for path in sorted(root.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda d: d.get("created_at", ""))
    return out


def read_draft(draft_id: str) -> dict[str, Any] | None:
    """Read one draft by id. Returns None if missing or unreadable."""
    try:
        safe_id = _sanitize_id(draft_id)
        if not safe_id:
            return None
        path = inbox_dir() / f"{safe_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_draft(draft_id: str) -> bool:
    """Delete one draft by id. Returns True if removed, False if missing."""
    try:
        safe_id = _sanitize_id(draft_id)
        if not safe_id:
            return False
        path = inbox_dir() / f"{safe_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True
    except Exception:
        return False


# ---- validation helpers for the approve step ----

_REQUIRED_TRIPWIRE_FIELDS = ("id", "title", "severity", "domain", "triggers", "body")
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}


def validate_draft(draft: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate a draft's tripwire fields.

    Returns (missing_fields, todo_fields) where:
      - missing_fields: required fields that are absent or empty
      - todo_fields: string fields that still contain "TODO" placeholders
                     (a signal that the human hasn't edited the draft yet)

    Both lists empty -> the draft is ready to approve.
    """
    missing: list[str] = []
    todos: list[str] = []

    for f in _REQUIRED_TRIPWIRE_FIELDS:
        val = draft.get(f)
        if val is None or val == "" or val == []:
            missing.append(f)
        elif isinstance(val, str) and "TODO" in val or isinstance(val, list) and any(
            isinstance(v, str) and "TODO" in v for v in val
        ):
            todos.append(f)

    # Severity must be valid
    sev = draft.get("severity")
    if sev and sev not in _VALID_SEVERITIES and "severity" not in missing:
        missing.append("severity")

    # Tripwire id must be snake_case-ish
    tid = draft.get("id")
    if tid and not _VALID_ID_RE.match(tid) and "id" not in todos:
        todos.append("id")

    return missing, todos


def draft_to_tripwire_kwargs(draft: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields accepted by `CortexStore.add_tripwire()` from a
    draft. Unknown keys are silently ignored so drafts can carry metadata
    fields that don't exist on the tripwire schema."""
    allowed = {
        "id", "title", "severity", "domain", "triggers", "body",
        "verify_cmd", "cost_usd", "source_file", "violation_patterns",
    }
    return {k: v for k, v in draft.items() if k in allowed}
