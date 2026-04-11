"""Cortex DMN (Default Mode Network) reflection loop: cheap LLM analysis
of session audit logs to propose new tripwires for the inbox.

Uses Anthropic Haiku 4.5 by default (`claude-haiku-4-5-20251001`).
Optional dependency: `pip install cortex-agent[dmn]` installs the
anthropic SDK.

Workflow:

    $ cortex reflect [--days N] [--dry-run] [--max-proposals M]

    1. Read session logs via cortex.stats.collect_sessions(days=N)
    2. Build a condensed summary (event counts, top tripwires hit,
       cold tripwires, matched/non-matched session ratio)
    3. Load current tripwires from the store (so we don't duplicate)
    4. Render a prompt template and call Haiku
    5. Parse the JSON response into draft dicts
    6. Write each draft to .cortex/inbox/ with source="dmn_haiku"
    7. User reviews via `cortex inbox list` and approves / rejects

Budget: one reflection call is ~10-30K input tokens + ~1-2K output.
Haiku 4.5 pricing is ~$1 / 1M input tokens and ~$5 / 1M output tokens,
so a reflection call costs about $0.02. Trivial, but the dry-run flag
lets you inspect the prompt before submitting.

Fail-safe: all API errors, JSON parse errors, and missing-SDK errors
are caught and reported without crashing. The hook path never imports
this module so DMN is strictly CLI-invoked.

Prompt engineering notes:
- We include ONE existing tripwire as a format example (not copy
  content). Haiku would otherwise invent its own structure.
- We explicitly list EVERY existing tripwire id + title so Haiku
  doesn't propose duplicates.
- We ask for strict JSON (array of dicts) with no surrounding prose.
  The parser strips leading/trailing text defensively anyway.
- We cap max_tokens at 2000 because the drafts are short (1-3 tripwires
  × ~500 tokens each).
"""
from __future__ import annotations

import json
import re
from typing import Any

from cortex.classify import find_db
from cortex.inbox import write_draft
from cortex.stats import collect_sessions, compute_stats
from cortex.store import CortexStore

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_PROPOSALS = 3


def build_session_summary(
    days: int = 7,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Compute the session summary that will be fed to Haiku.

    Returns a dict with:
      - window_days, n_sessions, n_events
      - sessions_with_inject, sessions_with_fallback, sessions_silent
      - top_tripwires_hit (list[(id, count)])
      - top_rules_hit (list[(id, count)])
      - top_tools (list[(name, count)])
      - cold_tripwires (list[id])
      - n_silent_violations (int)
    """
    sessions = collect_sessions(days=days)
    stats = compute_stats(sessions)

    db = db_path or find_db()
    store = CortexStore(db)
    try:
        all_ids = [t["id"] for t in store.list_tripwires()]
    finally:
        store.close()
    cold = sorted(
        tw_id for tw_id in all_ids
        if tw_id not in (stats.get("tripwires_hit") or {})
    )

    def _top(counter: dict, k: int) -> list[tuple[str, int]]:
        return sorted(counter.items(), key=lambda x: -x[1])[:k]

    n_sessions = stats["n_sessions"]
    sessions_silent = (
        n_sessions
        - stats["sessions_with_inject"]
        - stats["sessions_with_fallback"]
    )

    return {
        "window_days": days,
        "n_sessions": n_sessions,
        "n_events": stats["n_events"],
        "sessions_with_inject": stats["sessions_with_inject"],
        "sessions_with_fallback": stats["sessions_with_fallback"],
        "sessions_silent": max(0, sessions_silent),
        "top_tripwires_hit": _top(stats.get("tripwires_hit") or {}, 10),
        "top_rules_hit": _top(stats.get("rules_hit") or {}, 10),
        "top_tools": _top(stats.get("tool_calls") or {}, 10),
        "cold_tripwires": cold,
        "n_silent_violations": sum(
            (stats.get("potential_violations") or {}).values()
        ),
    }


def build_existing_tripwires_summary(
    db_path: str | None = None,
) -> list[dict[str, str]]:
    """Return a compact list of existing tripwires (id + title + severity +
    domain) for Haiku to avoid duplicating."""
    db = db_path or find_db()
    store = CortexStore(db)
    try:
        return [
            {
                "id": t["id"],
                "title": t["title"],
                "severity": t["severity"],
                "domain": t["domain"],
            }
            for t in store.list_tripwires()
        ]
    finally:
        store.close()


def build_prompt(
    session_summary: dict[str, Any],
    existing_tripwires: list[dict[str, str]],
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
) -> str:
    """Render the Haiku prompt from a session summary + existing store."""
    lines: list[str] = []
    lines.append(
        "You are analyzing session audit logs from Cortex, an active memory "
        "system for AI coding agents. Your job is to find patterns in recent "
        "activity that suggest a NEW tripwire (structured lesson) should be "
        "added to the store."
    )
    lines.append("")
    lines.append(
        "A tripwire has these fields: id (snake_case), title (<=80 chars), "
        "severity (critical|high|medium|low), domain (project tag), triggers "
        "(list of keywords for hook-time matching), body (WHY + HOW TO APPLY "
        "in 3-6 short paragraphs), and optionally violation_patterns (regexes "
        "for runtime detection)."
    )
    lines.append("")

    lines.append(
        "## Existing tripwires in the store (DO NOT duplicate — propose only "
        "NEW lessons that do not overlap with these)"
    )
    for tw in existing_tripwires:
        lines.append(
            f"- {tw['id']} [{tw['severity']}/{tw['domain']}]: {tw['title']}"
        )
    lines.append("")

    s = session_summary
    lines.append(f"## Recent session activity (last {s['window_days']} days)")
    lines.append(f"- Total sessions:           {s['n_sessions']}")
    lines.append(f"- Total audit events:       {s['n_events']}")
    lines.append(f"- Sessions with inject:     {s['sessions_with_inject']}")
    lines.append(f"- Sessions with fallback:   {s['sessions_with_fallback']}")
    lines.append(
        f"- Sessions silent (tool_calls but no cortex match): {s['sessions_silent']}"
    )
    lines.append(f"- Silent violations logged: {s['n_silent_violations']}")
    lines.append("")

    if s["top_tripwires_hit"]:
        lines.append("Top injected tripwires:")
        for tw_id, count in s["top_tripwires_hit"]:
            lines.append(f"  {count:>4} x {tw_id}")
        lines.append("")

    if s["top_rules_hit"]:
        lines.append("Top matched rules:")
        for r_id, count in s["top_rules_hit"]:
            lines.append(f"  {count:>4} x {r_id}")
        lines.append("")

    if s["top_tools"]:
        lines.append("Top tool calls (across all sessions):")
        for name, count in s["top_tools"]:
            lines.append(f"  {count:>4} x {name}")
        lines.append("")

    if s["cold_tripwires"]:
        lines.append("Cold tripwires (never matched in window):")
        for tw_id in s["cold_tripwires"]:
            lines.append(f"  - {tw_id}")
        lines.append("")

    lines.append("## Your task")
    lines.append(
        f"Propose up to {max_proposals} NEW tripwires that would have helped "
        "the agent in the sessions above. Each tripwire MUST:"
    )
    lines.append("")
    lines.append(
        "1. Reference a SPECIFIC observed pattern from the data above "
        "(not general wisdom)."
    )
    lines.append("2. NOT duplicate any existing tripwire in the list above.")
    lines.append(
        "3. Have a concrete WHY (what specific thing went wrong or could "
        "go wrong based on observed activity)."
    )
    lines.append(
        "4. Have actionable HOW TO APPLY steps (numbered, concrete)."
    )
    lines.append(
        "5. Have narrow triggers (between 3 and 10 specific keywords, "
        "not a handful of common English words)."
    )
    lines.append("")
    lines.append(
        "Respond with a JSON array. No prose, no markdown, no code fences — "
        "just the bare JSON. Each element has this schema:"
    )
    lines.append("")
    lines.append("""[
  {
    "id": "snake_case_id_under_40_chars",
    "title": "one-line summary under 80 chars",
    "severity": "high",
    "domain": "polymarket",
    "triggers": ["word1", "word2", "word3"],
    "body": "One-sentence rule statement.\\n\\nWhy: specific incident or pattern.\\n\\nHow to apply: (1) action. (2) action. (3) edge case.",
    "violation_patterns": [],
    "evidence": "brief citation of what in the session data prompted this proposal"
  }
]""")
    lines.append("")
    lines.append(
        "If no new tripwires are warranted by the data, return an empty "
        "array: []"
    )
    return "\n".join(lines)


def parse_proposals(response_text: str) -> list[dict[str, Any]]:
    """Parse Haiku's response into a list of draft dicts.

    Tolerant of:
      - leading/trailing prose (we find the first `[` and last `]`)
      - markdown code fences around the JSON
      - missing optional fields (evidence, violation_patterns)

    Returns an empty list on any parse error.
    """
    if not response_text:
        return []
    text = response_text.strip()

    # Strip common markdown code fence patterns
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    # Find the first JSON array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    # Keep only dict elements
    return [item for item in parsed if isinstance(item, dict)]


def estimate_prompt_tokens(prompt: str) -> int:
    """Rough token estimate (chars / 4). No tiktoken dep."""
    return len(prompt) // 4


def call_haiku(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: Any = None,
) -> str:
    """Call the Anthropic Messages API and return the response text.

    Client injection allows tests to use a mock without importing
    anthropic. In production, `client=None` lazy-imports and constructs
    a default Anthropic() client (which picks up ANTHROPIC_API_KEY from
    the environment).
    """
    if client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK not installed. Install via "
                "`pip install cortex-agent[dmn]` or `pip install anthropic`."
            ) from exc
        client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    # The SDK returns a Message object with .content = list of blocks.
    # Text blocks have a .text attribute. Concatenate all text blocks.
    parts: list[str] = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def write_proposals_to_inbox(
    proposals: list[dict[str, Any]],
    source: str = "dmn_haiku",
) -> list[str]:
    """Write each proposal to the inbox. Returns list of draft_ids."""
    draft_ids: list[str] = []
    for prop in proposals:
        # Strip Haiku's `evidence` field from the actual tripwire body
        # (it's metadata, not part of the tripwire schema). Preserve it
        # as a prefix in the body so the human reviewer sees it.
        evidence = prop.pop("evidence", "")
        if evidence and "body" in prop:
            prop["body"] = f"(evidence: {evidence})\n\n{prop['body']}"

        draft_id = write_draft(prop, source=source)
        if draft_id:
            draft_ids.append(draft_id)
    return draft_ids


def run_reflection(
    days: int = 7,
    model: str = DEFAULT_MODEL,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
    dry_run: bool = False,
    db_path: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Full reflection pipeline. Returns a result dict.

    If `dry_run` is True, builds the prompt and returns it without
    calling the API. `n_drafts_written` will be 0.
    """
    session_summary = build_session_summary(days=days, db_path=db_path)
    existing = build_existing_tripwires_summary(db_path=db_path)
    prompt = build_prompt(session_summary, existing, max_proposals=max_proposals)

    result: dict[str, Any] = {
        "days": days,
        "model": model,
        "session_summary": session_summary,
        "existing_tripwires_count": len(existing),
        "prompt": prompt,
        "prompt_tokens_est": estimate_prompt_tokens(prompt),
        "dry_run": dry_run,
        "proposals": [],
        "draft_ids": [],
        "n_drafts_written": 0,
        "error": None,
    }

    if dry_run:
        return result

    try:
        response_text = call_haiku(
            prompt, model=model, max_tokens=DEFAULT_MAX_TOKENS, client=client,
        )
    except Exception as e:
        result["error"] = f"Haiku call failed: {type(e).__name__}: {e}"
        return result

    result["raw_response"] = response_text
    proposals = parse_proposals(response_text)
    if len(proposals) > max_proposals:
        proposals = proposals[:max_proposals]
    result["proposals"] = proposals

    draft_ids = write_proposals_to_inbox(proposals)
    result["draft_ids"] = draft_ids
    result["n_drafts_written"] = len(draft_ids)
    return result


def render_reflection_report(result: dict[str, Any]) -> str:
    """Render a run_reflection result as human-readable text."""
    lines: list[str] = []
    lines.append(f"Cortex DMN reflection (last {result['days']} days)")
    lines.append("=" * 66)
    s = result.get("session_summary") or {}
    lines.append(f"Sessions analyzed:           {s.get('n_sessions', 0)}")
    lines.append(f"Events analyzed:             {s.get('n_events', 0)}")
    lines.append(f"Existing tripwires:          {result['existing_tripwires_count']}")
    lines.append(f"Model:                       {result['model']}")
    lines.append(
        f"Prompt size estimate:        ~{result['prompt_tokens_est']} tokens"
    )
    lines.append("")

    if result.get("dry_run"):
        lines.append("[DRY RUN] Prompt that would be sent to Haiku:")
        lines.append("")
        for line in result["prompt"].split("\n"):
            lines.append(f"  {line}")
        lines.append("")
        lines.append(
            "Re-run without --dry-run to submit this prompt and write drafts to the inbox."
        )
        return "\n".join(lines)

    if result.get("error"):
        lines.append(f"ERROR: {result['error']}")
        lines.append("")
        lines.append("No drafts were written to the inbox.")
        return "\n".join(lines)

    proposals = result.get("proposals") or []
    if not proposals:
        lines.append("Haiku returned no new tripwire proposals.")
        lines.append(
            "This is expected when the existing store already covers the "
            "observed session activity."
        )
        return "\n".join(lines)

    lines.append(f"Haiku proposed {len(proposals)} new tripwire(s):")
    lines.append("")
    for i, prop in enumerate(proposals, 1):
        lines.append(
            f"[{i}] {prop.get('id', '?')} "
            f"[{prop.get('severity', '?')}/{prop.get('domain', '?')}]"
        )
        lines.append(f"    {prop.get('title', '')}")
        triggers = prop.get("triggers") or []
        if triggers:
            lines.append(f"    triggers: {', '.join(triggers[:8])}")
        lines.append("")

    lines.append(f"Wrote {result['n_drafts_written']} draft(s) to inbox:")
    for draft_id in result.get("draft_ids") or []:
        lines.append(f"  - {draft_id}")
    lines.append("")
    lines.append("Review and approve via:")
    lines.append("  cortex inbox list")
    lines.append("  cortex inbox show <draft_id>")
    lines.append("  cortex inbox approve <draft_id>")
    return "\n".join(lines)
