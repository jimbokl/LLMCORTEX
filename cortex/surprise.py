"""Day 14 -- Surprise Engine (predictive coding).

The cortex brief, when it fires a critical tripwire, asks the agent to
emit a falsifiable two-field prediction in its reply text:

    <cortex_predict>
      <outcome>what I expect to happen</outcome>
      <failure_mode>most likely technical reason this might fail</failure_mode>
    </cortex_predict>

`cortex-watch` (PostToolUse) reads the transcript file passed by Claude
Code, extracts the last assistant message, parses the XML block if
present, and logs a `prediction` event *just before* the `tool_call`
event in the session audit log.

This module provides:

  * `parse_prediction(text)`          -- regex XML parser, fail-safe
  * `read_last_assistant_text(path)`  -- tail-read a Claude Code jsonl
                                         transcript, return the last
                                         assistant text content
  * `collect_pairs(days)`             -- walk session logs, pair each
                                         `prediction` with the
                                         immediately-following
                                         `tool_call` event
  * `render_surprise_table(pairs)`    -- ASCII table renderer for the
                                         `cortex surprise` CLI

No LLM calls happen here. DMN classification of each pair as
match/mismatch/partial is a Day-15+ concern. This module just stores
the raw substrate and renders it for humans.

Fail-safe contract: every public function swallows exceptions and
returns an empty default. The hook must never crash because of a
malformed transcript or prediction tag.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cortex.session import sessions_dir
from cortex.stats import _read_session_file

# ---- prediction tag parsing ----

# The two-field XML block we ask the agent to emit. DOTALL so the
# outcome / failure_mode text may span multiple lines. Non-greedy so
# only the FIRST cortex_predict block in the message is captured.
_PREDICT_RE = re.compile(
    r"<cortex_predict>\s*"
    r"<outcome>\s*(.*?)\s*</outcome>\s*"
    r"<failure_mode>\s*(.*?)\s*</failure_mode>\s*"
    r"</cortex_predict>",
    re.DOTALL | re.IGNORECASE,
)

# Cap on how much text we store per field. Predictions are not prose;
# 500 chars is already generous and keeps the audit log compact.
_FIELD_MAX = 500


def parse_prediction(text: str) -> dict[str, str] | None:
    """Extract `{outcome, failure_mode}` from the last assistant message.

    Returns None if the tag is absent or malformed. Whitespace is
    normalized, inner tags are stripped, each field is capped at 500 chars.
    """
    if not text or "<cortex_predict>" not in text.lower():
        return None
    try:
        m = _PREDICT_RE.search(text)
        if not m:
            return None
        outcome = _clean(m.group(1))
        failure_mode = _clean(m.group(2))
        if not outcome and not failure_mode:
            return None
        return {
            "outcome": outcome[:_FIELD_MAX],
            "failure_mode": failure_mode[:_FIELD_MAX],
        }
    except Exception:
        return None


def _clean(s: str) -> str:
    """Collapse whitespace runs, strip surrounding blanks."""
    return re.sub(r"\s+", " ", s or "").strip()


# ---- transcript reading ----


def read_last_assistant_text(transcript_path: str | Path | None) -> str:
    """Return the text content of the last assistant message in a
    Claude Code transcript jsonl file, or empty string on any error.

    Claude Code's transcript format (as of 2.1.x): one JSON object per
    line with a top-level `type` field. Assistant messages look like:

        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "..."},
                                 {"type": "tool_use", ...}]},
         "uuid": "...", ...}

    We walk the file once and keep the text content of the most recent
    assistant entry. Thinking blocks and tool_use blocks are ignored --
    only `type: "text"` content counts, because the `<cortex_predict>`
    tag must appear in the visible reply text, not in extended thinking.
    """
    if not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        last_text = ""
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message") or {}
                content = msg.get("content")
                text_chunks: list[str] = []
                if isinstance(content, str):
                    text_chunks.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "text":
                            continue
                        t = block.get("text") or ""
                        if t:
                            text_chunks.append(t)
                if text_chunks:
                    last_text = "\n".join(text_chunks)
        return last_text
    except Exception:
        return ""


def _is_human_user_content(content: object) -> bool:
    """True if a `type: user` transcript row is a real human prompt
    rather than a tool_result wrapper from the current agent turn.

    Claude Code stores tool results as `{"type": "user", "message":
    {"content": [{"type": "tool_result", ...}]}}`. Those messages must
    NOT reset the "agent turn" boundary, otherwise a prediction emitted
    in a preamble assistant message earlier in the same turn becomes
    invisible to the PostToolUse hook.

    Human messages arrive either as a plain string (legacy shape still
    used in some fixtures) or as a list containing at least one `text`
    block and no `tool_result` block.
    """
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = False
        has_tool_result = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                has_tool_result = True
            elif btype == "text":
                t = block.get("text") or ""
                if t.strip():
                    has_text = True
        return has_text and not has_tool_result
    return False


def read_last_prediction_text(transcript_path: str | Path | None) -> str:
    """Return the text of the most recent assistant message containing
    a `<cortex_predict>` block **within the current agent turn**.

    The current agent turn is defined as every transcript row appearing
    after the most recent real human user message. Tool-result user
    rows are treated as part of the agent turn (they are the agent's
    own prior tool invocations).

    Why this function exists (Day 14 bug, 2026-04-11): the original
    `read_last_assistant_text` always returned the *last* assistant
    row, which fails whenever the agent emits its prediction in a
    text-only preamble message and then places tool_use blocks in a
    *later* assistant message of the same turn. By the time the
    PostToolUse hook fires for the tool_use, "last assistant" is the
    tool-bearing message, which contains no predict tag, and surprise
    pairing silently loses the event.

    Returns empty string if no predict block is found, if the file is
    missing, or on any parse error. Fail-safe: this function must never
    raise -- the cortex watch hook is in the critical path of every
    tool call and cannot be allowed to break the harness.
    """
    if not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Find index just past the most recent real human user msg.
        turn_start = 0
        for i in range(len(rows) - 1, -1, -1):
            row = rows[i]
            if row.get("type") != "user":
                continue
            msg = row.get("message") or {}
            if _is_human_user_content(msg.get("content")):
                turn_start = i + 1
                break

        # Scan the current agent turn for the latest assistant text
        # that carries a cortex_predict block.
        last_predict_text = ""
        for row in rows[turn_start:]:
            if row.get("type") != "assistant":
                continue
            msg = row.get("message") or {}
            content = msg.get("content")
            text_chunks: list[str] = []
            if isinstance(content, str):
                text_chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "text":
                        continue
                    t = block.get("text") or ""
                    if t:
                        text_chunks.append(t)
            joined = "\n".join(text_chunks)
            if joined and "<cortex_predict>" in joined:
                last_predict_text = joined
        return last_predict_text
    except Exception:
        return ""


# ---- prediction/outcome pairing ----


def collect_pairs(
    days: int | None = None,
    sessions_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Walk session audit logs and pair each `prediction` with the next
    `tool_call` event in the same session.

    Returns a list of dicts, newest sessions last:

        [{
            "session_id": str,
            "at": iso timestamp of the prediction event,
            "outcome": str,              # predicted
            "failure_mode": str,         # predicted
            "tool_name": str | None,     # actually executed
            "tool_snippet": str,         # input_snippet of the call
            "tool_response": str,        # truncated tool_response
            "tripwire_ids": list[str],   # active tripwires at emission
        }, ...]

    A prediction with no following tool_call is still returned (with
    `tool_name=None`) so the `cortex surprise` table can surface
    "agent predicted but never acted" cases as a separate signal.
    """
    from datetime import datetime, timedelta, timezone

    root = sessions_root or sessions_dir()
    if not root.exists():
        return []

    cutoff: datetime | None = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    pairs: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl")):
        events = _read_session_file(path)
        if not events:
            continue
        if cutoff is not None:
            # Skip sessions whose last event predates the cutoff.
            last_at = None
            for e in reversed(events):
                try:
                    last_at = datetime.fromisoformat(e.get("at", ""))
                    break
                except (ValueError, TypeError):
                    continue
            if last_at is None or last_at < cutoff:
                continue
        session_id = path.stem
        # Track the most recent active tripwire set so each prediction
        # can be tagged with what context the agent was reacting to.
        active_tripwires: list[str] = []
        pending: dict[str, Any] | None = None
        for event in events:
            ev = event.get("event", "") or ""
            if ev in ("inject", "keyword_fallback"):
                active_tripwires = list(event.get("tripwire_ids") or [])
            elif ev == "prediction":
                if pending is not None:
                    # Two predictions in a row with no tool_call -- ship
                    # the earlier one as orphan and start a new one.
                    pairs.append(pending)
                pending = {
                    "session_id": session_id,
                    "at": event.get("at", ""),
                    "outcome": event.get("outcome", "") or "",
                    "failure_mode": event.get("failure_mode", "") or "",
                    "tool_name": None,
                    "tool_snippet": "",
                    "tool_response": "",
                    "tripwire_ids": list(active_tripwires),
                }
            elif ev == "tool_call" and pending is not None:
                pending["tool_name"] = event.get("tool_name", "") or ""
                pending["tool_snippet"] = event.get("input_snippet", "") or ""
                pending["tool_response"] = event.get("response_snippet", "") or ""
                pairs.append(pending)
                pending = None
        if pending is not None:
            pairs.append(pending)
    return pairs


def render_surprise_table(
    pairs: list[dict[str, Any]],
    *,
    days: int | None = None,
    max_rows: int = 30,
) -> str:
    """Render prediction/outcome pairs as a human-readable ASCII block."""
    window = f"last {days} days" if days else "all-time"
    lines: list[str] = []
    lines.append(f"Cortex surprise log ({window})")
    lines.append("=" * 66)
    if not pairs:
        lines.append("(no <cortex_predict> blocks captured yet)")
        lines.append("")
        lines.append("The agent has not emitted any predictions. Either:")
        lines.append("  * no critical tripwires fired in this window")
        lines.append("  * the soft-inject request was ignored by the agent")
        lines.append("  * cortex-watch is not wired to PostToolUse yet")
        return "\n".join(lines)

    n_total = len(pairs)
    n_orphan = sum(1 for p in pairs if not p.get("tool_name"))
    n_paired = n_total - n_orphan
    lines.append(f"Predictions total:         {n_total}")
    lines.append(f"  paired with tool_call:   {n_paired}")
    lines.append(f"  orphaned (no tool_call): {n_orphan}")
    lines.append("")

    shown = pairs[-max_rows:]
    if len(pairs) > max_rows:
        lines.append(f"(showing last {max_rows} of {n_total})")
        lines.append("")

    for i, p in enumerate(shown, 1):
        tw = ",".join(p.get("tripwire_ids") or []) or "(none)"
        lines.append(f"[{i}] {p.get('at', '')}  session={p.get('session_id', '')[:12]}")
        lines.append(f"    tripwires: {tw[:60]}")
        outcome = _truncate(p.get("outcome", ""), 120)
        fm = _truncate(p.get("failure_mode", ""), 120)
        lines.append(f"    predict:   {outcome}")
        lines.append(f"    fail_mode: {fm}")
        tool = p.get("tool_name") or "(none)"
        snippet = _truncate(p.get("tool_snippet", ""), 120)
        response = _truncate(p.get("tool_response", ""), 120)
        lines.append(f"    actual:    {tool}  {snippet}")
        if response:
            lines.append(f"    response:  {response}")
        lines.append("")

    lines.append(
        "Pairs are raw data. DMN match/mismatch classification is"
    )
    lines.append("planned for Day 15+.")
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 3] + "..."
