"""Pattern-suggest helper: read session logs for past injections of a
given tripwire, extract the tool calls that followed, and generate
auto-regex candidates for `violation_patterns`.

The workflow:

  1. `cortex stats --sessions` shows a `[WARN]` or `[FAIL]` tripwire,
     or you want to add detection to a cold `[SKIP]` tripwire with no
     existing `violation_patterns`.
  2. `cortex suggest-patterns <tripwire_id>` reads session logs for
     past injections of that tripwire, collects the `tool_call` events
     that followed, finds the longest common substring across snippets,
     generalizes digits/whitespace, and emits a candidate regex.
  3. Optionally pass `--fix-example "snippet of the fix pattern"` so
     the tool verifies the candidate does NOT match the known fix.
  4. Copy the regex into the tripwire's `violation_patterns` list in
     `cortex/importers/memory_md.py` and run `cortex migrate`.

The auto-regex generator uses a pair-wise LCS heuristic over snippets:

  - Find the longest substring present in all snippets.
  - Escape regex metacharacters.
  - Replace runs of digits with `\\d+` (so `300` generalizes to any int).
  - Replace runs of spaces with `\\s*` (so `ts // 300` matches `ts//300`).
  - Verify the generated regex actually matches every input snippet.

It does NOT guess negative lookaheads for fix patterns. Those are the
responsibility of the user (or the `--fix-example` flag). Generated
regexes are always presented with a "confidence" marker so the user
knows how much to trust them before shipping.

The tool never writes to the store. It only reads session logs and
prints a report.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from cortex.session import read_session, sessions_dir

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_MIN_ANCHOR_LEN = 8  # substrings shorter than this are too generic


def _extract_identifiers(text: str) -> set[str]:
    """Extract Python-identifier-like tokens (>=3 chars) from a snippet."""
    return set(_WORD_RE.findall(text))


def collect_post_injection_snippets(
    tripwire_id: str,
    window: int = 10,
) -> list[dict[str, Any]]:
    """For each session where `tripwire_id` was injected (via `inject`
    or `keyword_fallback`), collect the next `window` `tool_call`
    events.

    Returns:
        list of findings, each:
            {
                "session_id": str,
                "inject_at":  str (iso),
                "inject_type": "inject" | "keyword_fallback",
                "tool_calls": list[dict]  # the raw events
            }

    Order preserved. Multiple injections in the same session produce
    multiple findings.
    """
    findings: list[dict[str, Any]] = []
    try:
        root = sessions_dir()
    except Exception:
        return findings
    if not root.exists():
        return findings

    for path in sorted(root.glob("*.jsonl")):
        session_id = path.stem
        events = read_session(session_id)
        if not events:
            continue

        for i, event in enumerate(events):
            ev_type = event.get("event", "")
            if ev_type not in ("inject", "keyword_fallback"):
                continue
            tw_ids = event.get("tripwire_ids") or []
            if tripwire_id not in tw_ids:
                continue

            # Collect the next `window` tool_call events
            following: list[dict] = []
            for j in range(i + 1, min(i + 1 + window, len(events))):
                nxt = events[j]
                if nxt.get("event") == "tool_call":
                    following.append(nxt)

            findings.append({
                "session_id": session_id,
                "inject_at": event.get("at", ""),
                "inject_type": ev_type,
                "tool_calls": following,
            })

    return findings


# ---- auto-regex generation ----


def _longest_common_substring(a: str, b: str) -> str:
    """Classic O(m*n) LCS-substring. Returns the longest substring
    present in both a and b, or empty if there is none."""
    if not a or not b:
        return ""
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best_len = 0
    best_end = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_end = i
        prev = curr
    return a[best_end - best_len : best_end]


def _lcs_across(snippets: list[str]) -> str:
    """Find the longest substring present in ALL snippets via iterative
    pair-wise LCS. Returns empty if no common substring exists."""
    if not snippets:
        return ""
    if len(snippets) == 1:
        return snippets[0]
    current = _longest_common_substring(snippets[0], snippets[1])
    for s in snippets[2:]:
        if not current:
            return ""
        current = _longest_common_substring(current, s)
    return current


def _generalize_to_regex(text: str) -> str:
    """Escape regex metacharacters in `text`, then generalize:

    - runs of literal spaces  -> `\\s*` (permits any whitespace, incl none)
    - runs of literal digits  -> `\\d+` (permits any positive integer)
    """
    escaped = re.escape(text)
    # re.escape produces `\ ` for a space; collapse runs of these.
    escaped = re.sub(r"(\\ )+", r"\\s*", escaped)
    # Digits are not escaped by re.escape (they aren't meta), so they
    # appear literally. Replace runs of literal digits with \d+.
    escaped = re.sub(r"\d+", r"\\d+", escaped)
    return escaped


def generate_regex_candidate(
    snippets: list[str],
    min_anchor_len: int = _MIN_ANCHOR_LEN,
    fix_example: str | None = None,
) -> dict[str, Any] | None:
    """Build an auto-regex candidate from a list of tool_input snippets.

    Returns None if:
      - There are no snippets.
      - The longest common substring is shorter than `min_anchor_len`.
      - The generalized regex fails to compile.
      - The regex does not actually match all input snippets (safety).

    Returns a dict with:
        anchor         str   the LCS substring used as the seed
        regex          str   the generalized regex pattern
        match_count    int   how many input snippets the regex matches
        total          int   len(snippets)
        confidence     str   "high" | "medium" | "low"
        fix_example_matches  bool | None
                             True = regex matches the given fix example
                             (bad, means the pattern is too broad)
                             False = regex does not match the fix (good)
                             None = no fix_example provided
    """
    cleaned = [s for s in snippets if s and s.strip()]
    if not cleaned:
        return None

    anchor = _lcs_across(cleaned)
    if len(anchor) < min_anchor_len:
        return None

    regex = _generalize_to_regex(anchor)

    try:
        pat = re.compile(regex)
    except re.error:
        return None

    matches = sum(1 for s in cleaned if pat.search(s))
    if matches < len(cleaned):
        # Generalization broke the match. Fall back to the anchor-only
        # (plain literal escape, no digit/space generalization).
        fallback = re.escape(anchor)
        try:
            fallback_pat = re.compile(fallback)
        except re.error:
            return None
        fallback_matches = sum(1 for s in cleaned if fallback_pat.search(s))
        if fallback_matches < len(cleaned):
            return None
        regex = fallback
        pat = fallback_pat
        matches = fallback_matches

    # Confidence heuristic
    if len(anchor) >= 25 and matches == len(cleaned):
        confidence = "high"
    elif len(anchor) >= 15 and matches == len(cleaned):
        confidence = "medium"
    else:
        confidence = "low"

    fix_matches: bool | None = None
    if fix_example:
        fix_matches = bool(pat.search(fix_example))
        if fix_matches:
            # Drop confidence if the regex flags a known fix as a bug.
            confidence = "low"

    return {
        "anchor": anchor,
        "regex": regex,
        "match_count": matches,
        "total": len(cleaned),
        "confidence": confidence,
        "fix_example_matches": fix_matches,
    }


def generate_regex_candidates(
    analysis: dict[str, Any],
    fix_example: str | None = None,
) -> dict[str, Any]:
    """Produce multiple regex candidates:
      - one global candidate across all snippets
      - one per-tool candidate (Edit, Bash, ...) when >=2 snippets

    Returns dict with:
      global:    candidate dict or None
      by_tool:   {tool_name: candidate dict or None}
    """
    all_snippets: list[str] = []
    for snippets in (analysis.get("snippets_by_tool") or {}).values():
        all_snippets.extend(snippets)

    global_candidate = generate_regex_candidate(
        all_snippets, fix_example=fix_example
    )

    by_tool: dict[str, Any] = {}
    for tool, snippets in (analysis.get("snippets_by_tool") or {}).items():
        if len(snippets) >= 2:
            cand = generate_regex_candidate(snippets, fix_example=fix_example)
            if cand is not None:
                by_tool[tool] = cand

    return {
        "global": global_candidate,
        "by_tool": by_tool,
    }


def analyze_snippets(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate tool_call data across findings.

    Returns:
        dict with keys:
            n_injections      int   # number of findings
            n_tool_calls      int   # total tool_calls captured
            n_snippets        int   # tool_calls with non-empty input_snippet
            by_tool           Counter[str]  # tool_name -> count
            snippets_by_tool  dict[str, list[str]]
            common_words      list[tuple[str, int]]  # (word, doc_count) sorted desc
    """
    by_tool: Counter[str] = Counter()
    snippets_by_tool: dict[str, list[str]] = {}
    all_snippets: list[str] = []

    for finding in findings:
        for tc in finding.get("tool_calls") or []:
            name = tc.get("tool_name", "") or "(unknown)"
            snippet = (tc.get("input_snippet") or "").strip()
            by_tool[name] += 1
            if snippet:
                all_snippets.append(snippet)
                snippets_by_tool.setdefault(name, []).append(snippet)

    common: list[tuple[str, int]] = []
    if all_snippets:
        doc_counts: Counter[str] = Counter()
        for snippet in all_snippets:
            for ident in _extract_identifiers(snippet):
                doc_counts[ident] += 1
        threshold = max(2, len(all_snippets) // 2)
        common = sorted(
            [(w, c) for w, c in doc_counts.items() if c >= threshold],
            key=lambda wc: (-wc[1], wc[0]),
        )[:20]

    return {
        "n_injections": len(findings),
        "n_tool_calls": sum(by_tool.values()),
        "n_snippets": len(all_snippets),
        "by_tool": dict(by_tool),
        "snippets_by_tool": snippets_by_tool,
        "common_words": common,
    }


def render_suggestions(
    tripwire_id: str,
    findings: list[dict[str, Any]],
    analysis: dict[str, Any],
    candidates: dict[str, Any] | None = None,
    snippet_preview_chars: int = 200,
    fix_example: str | None = None,
) -> str:
    """Render the analysis + auto-regex candidates as a human-readable report."""
    lines: list[str] = []
    lines.append(f"Pattern suggestions for: {tripwire_id}")
    lines.append("=" * 66)

    if not findings:
        lines.append("")
        lines.append(
            f"No past injections of '{tripwire_id}' found in session logs."
        )
        lines.append("")
        lines.append("This tripwire is COLD. Candidates for action:")
        lines.append("  1. Tighten or broaden its triggers so it fires on real")
        lines.append("     prompts in your project domain.")
        lines.append("  2. Remove it from the seed if the lesson no longer applies.")
        lines.append("  3. Use `cortex find <word1>,<word2>` to simulate which")
        lines.append("     tripwires fire on your typical prompt vocabulary.")
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"Found {analysis['n_injections']} past injection(s) across session logs."
    )
    lines.append(
        f"Captured {analysis['n_tool_calls']} tool calls in the next events "
        f"after each injection, of which {analysis['n_snippets']} had "
        f"tool_input snippets."
    )
    lines.append("")

    # AUTO-REGEX CANDIDATES (headline section)
    if candidates is not None:
        lines.append("## Auto-generated regex candidates")
        lines.append("")
        if fix_example is not None:
            lines.append(f"  Fix example provided: {fix_example[:80]}")
            lines.append(
                "  Candidates that match the fix are marked [LOW CONFIDENCE]."
            )
            lines.append("")

        glob = candidates.get("global")
        if glob:
            lines.extend(_render_candidate("global", glob))
        else:
            lines.append(
                "  (no global candidate — LCS across all snippets was too short)"
            )
            lines.append("")

        by_tool = candidates.get("by_tool") or {}
        if by_tool:
            lines.append("  Per-tool candidates:")
            for tool, cand in by_tool.items():
                lines.extend(_render_candidate(f"tool={tool}", cand, indent=2))

        lines.append("")
        lines.append("  Steps to adopt a candidate:")
        lines.append("  1. Verify the regex against a KNOWN FIX PATTERN:")
        lines.append(
            "     python -c \"import re; "
            "print(re.search(r'<REGEX>', '<FIX_SNIPPET>'))\""
        )
        lines.append("  2. If no match against the fix, add to the tripwire's")
        lines.append("     violation_patterns in cortex/importers/memory_md.py.")
        lines.append("  3. Run `cortex migrate` to refresh the store.")
        lines.append("  4. Day-6 `cortex stats --sessions` will start reporting")
        lines.append("     effectiveness rate for this tripwire.")
        lines.append("")
        lines.append("  Re-run with --fix-example to let cortex test the fix for you.")
        lines.append("")

    if analysis["by_tool"]:
        lines.append("## Tool call distribution")
        for tool, count in sorted(analysis["by_tool"].items(), key=lambda x: -x[1]):
            lines.append(f"  {count:>4} x {tool}")
        lines.append("")

    if analysis["snippets_by_tool"]:
        lines.append("## Tool_input snippets (grouped by tool)")
        for tool, snippets in analysis["snippets_by_tool"].items():
            lines.append("")
            lines.append(f"  == {tool} ({len(snippets)} snippet(s)) ==")
            for snippet in snippets[:5]:
                truncated = snippet[:snippet_preview_chars]
                if len(snippet) > snippet_preview_chars:
                    truncated += "..."
                lines.append(f"    {truncated}")
            if len(snippets) > 5:
                lines.append(f"    ... and {len(snippets) - 5} more")
        lines.append("")

    if analysis["common_words"]:
        lines.append("## Common identifiers across post-injection tool_inputs")
        lines.append("  (appearing in >= 50% of snippets, potential pattern anchors)")
        for word, count in analysis["common_words"]:
            lines.append(f"  {count:>3}  {word}")
        lines.append("")

    return "\n".join(lines)


def _render_candidate(
    label: str, cand: dict[str, Any], indent: int = 0,
) -> list[str]:
    """Render one regex candidate dict as a multi-line block."""
    pad = " " * indent
    lines: list[str] = []
    conf_tag = f"[{cand['confidence'].upper()}]"
    lines.append(f"{pad}  {conf_tag} {label}  ({cand['match_count']}/{cand['total']} snippets matched)")
    lines.append(f"{pad}    anchor:  {cand['anchor'][:120]}")
    lines.append(f"{pad}    regex:   {cand['regex']}")
    fix_m = cand.get("fix_example_matches")
    if fix_m is True:
        lines.append(f"{pad}    fix:     MATCHES the given fix example — too broad, narrow manually")
    elif fix_m is False:
        lines.append(f"{pad}    fix:     does not match the fix example — candidate is safe")
    lines.append("")
    return lines
