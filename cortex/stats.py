"""Session audit log analyzer -- Day 5 DMN accounting foundation.

Reads `.cortex/sessions/*.jsonl` files and produces aggregate statistics
on injection coverage, matched rules/tripwires/synthesis, cold tripwires
(never matched in the window), and tool-call density per session.

This is the passive accounting layer: it only READS the audit log, never
writes, never mutates store state. Silent-violation detection, injection
effectiveness scoring, and the DMN reflection loop all build on top of
this module by consuming the same event streams.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cortex.session import sessions_dir

# ---- anonymization helpers (Day 13) ----

_ANON_PREFIX = "anon_"


def anonymize_session_id(sid: str) -> str:
    """Hash a session id to a stable short identifier so stats reports
    can be shared publicly without leaking which project / machine
    produced them. Uses md5 (not cryptographic, just fast & consistent).

    Same input always produces the same output, so multi-session
    references stay consistent within one anonymized report.
    """
    if not sid:
        return f"{_ANON_PREFIX}empty"
    h = hashlib.md5(sid.encode("utf-8")).hexdigest()[:8]
    return f"{_ANON_PREFIX}{h}"


def anonymize_snippet(snippet: str, max_len: int = 60) -> str:
    """Redact a tool_input snippet for public sharing.

    Keeps the rough shape (file=... | old=... | new=...) but replaces
    the actual content with ``<REDACTED:NNNchars>`` markers. Identifiers
    leaking paths, values, or code structure get stripped.
    """
    if not snippet:
        return ""
    # Preserve the semicolon/pipe-separated sections but redact each value
    parts = []
    for segment in snippet.split(" | "):
        if "=" in segment:
            key, _, rest = segment.partition("=")
            parts.append(f"{key}=<REDACTED:{len(rest)}chars>")
        else:
            parts.append(f"<REDACTED:{len(segment)}chars>")
    out = " | ".join(parts)
    if len(out) > max_len * 3:
        out = out[: max_len * 3] + "..."
    return out


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string. Returns None on bad input."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _read_session_file(path: Path) -> list[dict]:
    """Read one .jsonl session file. Tolerates malformed lines."""
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def collect_sessions(
    days: int | None = None,
    sessions_root: Path | None = None,
) -> list[tuple[str, list[dict]]]:
    """Return `[(session_id, events), ...]` for all session files.

    If `days` is set, only include sessions whose most recent event is
    within the last N days (wall-clock, UTC). Sessions with no parseable
    timestamps are excluded when `days` is set.
    """
    root = sessions_root or sessions_dir()
    cutoff: datetime | None = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    out: list[tuple[str, list[dict]]] = []
    if not root.exists():
        return out

    for path in sorted(root.glob("*.jsonl")):
        events = _read_session_file(path)
        if not events:
            continue
        if cutoff is not None:
            timestamps = [_parse_iso(e.get("at", "")) for e in events]
            valid = [t for t in timestamps if t is not None]
            if not valid:
                continue
            last = max(valid)
            if last < cutoff:
                continue
        out.append((path.stem, events))
    return out


def compute_stats(sessions: list[tuple[str, list[dict]]]) -> dict[str, Any]:
    """Aggregate stats over a list of session event streams.

    Returned dict structure:
      n_sessions, n_events                  -- totals
      events_by_type                        -- Counter dict
      rules_hit, tripwires_hit,
      synthesis_hit, tool_calls             -- Counter dicts
      sessions_with_inject,
      sessions_with_fallback                -- session-level counts
      avg_tool_calls_per_session            -- float (rounded)
      potential_violations                  -- Counter per tripwire (Day 6)
      sessions_with_violations              -- int (Day 6)
      effectiveness                         -- dict per tripwire (Day 6):
          {tripwire_id: {"hits": N, "violations": M, "rate": 0.0-1.0}}
    """
    events_by_type: Counter[str] = Counter()
    rules_hit: Counter[str] = Counter()
    tripwires_hit: Counter[str] = Counter()
    synthesis_hit: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    violations_per_tripwire: Counter[str] = Counter()
    tool_calls_per_session: list[int] = []
    sessions_with_inject = 0
    sessions_with_fallback = 0
    sessions_with_violations = 0
    n_events = 0

    for _session_id, events in sessions:
        n_events += len(events)
        tc_this = 0
        has_inject = False
        has_fallback = False
        has_violation = False
        for event in events:
            ev_type = event.get("event", "") or ""
            if not ev_type:
                continue
            events_by_type[ev_type] += 1

            if ev_type == "inject":
                has_inject = True
                for r in event.get("matched_rules") or []:
                    rules_hit[r] += 1
                for t in event.get("tripwire_ids") or []:
                    tripwires_hit[t] += 1
                for s in event.get("synthesis_ids") or []:
                    synthesis_hit[s] += 1
            elif ev_type == "keyword_fallback":
                has_fallback = True
                for t in event.get("tripwire_ids") or []:
                    tripwires_hit[t] += 1
            elif ev_type == "tool_call":
                tc_this += 1
                tool_name = event.get("tool_name", "") or ""
                if tool_name:
                    tool_calls[tool_name] += 1
            elif ev_type == "potential_violation":
                has_violation = True
                tw_id = event.get("tripwire_id", "") or ""
                if tw_id:
                    violations_per_tripwire[tw_id] += 1

        tool_calls_per_session.append(tc_this)
        if has_inject:
            sessions_with_inject += 1
        if has_fallback:
            sessions_with_fallback += 1
        if has_violation:
            sessions_with_violations += 1

    avg_tc = (
        sum(tool_calls_per_session) / len(tool_calls_per_session)
        if tool_calls_per_session
        else 0.0
    )

    # Per-tripwire effectiveness: violation_rate = violations / hits.
    # Rate near 0 is good (lesson applied); rate near 1 is bad (ignored).
    effectiveness: dict[str, dict[str, Any]] = {}
    for tw_id, n_hits in tripwires_hit.items():
        n_viol = violations_per_tripwire.get(tw_id, 0)
        rate = n_viol / n_hits if n_hits > 0 else 0.0
        effectiveness[tw_id] = {
            "hits": n_hits,
            "violations": n_viol,
            "rate": round(rate, 3),
        }

    return {
        "n_sessions": len(sessions),
        "n_events": n_events,
        "events_by_type": dict(events_by_type),
        "rules_hit": dict(rules_hit),
        "tripwires_hit": dict(tripwires_hit),
        "synthesis_hit": dict(synthesis_hit),
        "tool_calls": dict(tool_calls),
        "sessions_with_inject": sessions_with_inject,
        "sessions_with_fallback": sessions_with_fallback,
        "avg_tool_calls_per_session": round(avg_tc, 1),
        "potential_violations": dict(violations_per_tripwire),
        "sessions_with_violations": sessions_with_violations,
        "effectiveness": effectiveness,
    }


def find_cold_tripwires(
    stats: dict[str, Any],
    all_tripwire_ids: list[str],
) -> list[str]:
    """Return tripwires that never appeared in any inject/fallback event
    during the analyzed window. These are candidates for removal or
    trigger tuning."""
    hit = set((stats.get("tripwires_hit") or {}).keys())
    return sorted(tw_id for tw_id in all_tripwire_ids if tw_id not in hit)


def compute_primary_vs_fallback_ratio(
    sessions: list[tuple[str, list[dict]]],
) -> dict[str, Any]:
    """How many sessions triggered primary inject vs keyword_fallback vs both.

    This metric surfaced the Day-4 TF-IDF fallback's empirical value:
    in real session data the fallback fires more often than the hand-written
    rule engine. Exposed here so users can see the ratio on their own
    data.
    """
    n_inject_only = 0
    n_fallback_only = 0
    n_both = 0
    n_neither = 0
    n_inject_events = 0
    n_fallback_events = 0
    for _sid, events in sessions:
        has_inject = False
        has_fallback = False
        for e in events:
            ev = e.get("event", "")
            if ev == "inject":
                has_inject = True
                n_inject_events += 1
            elif ev == "keyword_fallback":
                has_fallback = True
                n_fallback_events += 1
        if has_inject and has_fallback:
            n_both += 1
        elif has_inject:
            n_inject_only += 1
        elif has_fallback:
            n_fallback_only += 1
        else:
            n_neither += 1

    ratio = (
        round(n_fallback_events / n_inject_events, 2)
        if n_inject_events > 0
        else None
    )
    return {
        "sessions_inject_only": n_inject_only,
        "sessions_fallback_only": n_fallback_only,
        "sessions_both": n_both,
        "sessions_neither": n_neither,
        "inject_events": n_inject_events,
        "fallback_events": n_fallback_events,
        "fallback_to_inject_ratio": ratio,
    }


def render_stats(
    stats: dict[str, Any],
    cold_tripwires: list[str],
    days: int | None = None,
    *,
    anonymize: bool = False,
    ratio: dict[str, Any] | None = None,
) -> str:
    """Render stats dict as a human-readable text report."""
    lines: list[str] = []
    window = f"last {days} days" if days else "all-time"
    header = f"Cortex session audit ({window})"
    if anonymize:
        header += " [ANONYMIZED]"
    lines.append(header)
    lines.append("=" * 60)
    if anonymize:
        lines.append(
            "(session ids hashed, tool_input snippets redacted -- safe to share publicly)"
        )
        lines.append("")
    lines.append(f"Sessions:                  {stats['n_sessions']}")
    lines.append(f"Total events:              {stats['n_events']}")

    n_sess = stats["n_sessions"] or 1  # avoid div/0 in rate calc
    inj_rate = stats["sessions_with_inject"] * 100 / n_sess
    fb_rate = stats["sessions_with_fallback"] * 100 / n_sess
    lines.append(
        f"Sessions with inject:      {stats['sessions_with_inject']}"
        f"  ({inj_rate:.0f}%)"
    )
    lines.append(
        f"Sessions with fallback:    {stats['sessions_with_fallback']}"
        f"  ({fb_rate:.0f}%)"
    )
    lines.append(
        f"Avg tool_calls / session:  {stats['avg_tool_calls_per_session']}"
    )
    lines.append("")

    if stats["events_by_type"]:
        lines.append("Events by type:")
        for ev, n in sorted(stats["events_by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  {ev:<20} {n}")
        lines.append("")

    if stats["rules_hit"]:
        lines.append("Top matched rules:")
        ranked = sorted(stats["rules_hit"].items(), key=lambda x: -x[1])[:10]
        for rule, n in ranked:
            lines.append(f"  {n:>4} x  {rule}")
        lines.append("")

    if stats["tripwires_hit"]:
        lines.append("Top matched tripwires:")
        ranked = sorted(stats["tripwires_hit"].items(), key=lambda x: -x[1])[:10]
        for tw, n in ranked:
            lines.append(f"  {n:>4} x  {tw}")
        lines.append("")

    if stats["synthesis_hit"]:
        lines.append("Synthesis rules fired:")
        ranked = sorted(stats["synthesis_hit"].items(), key=lambda x: -x[1])
        for sr, n in ranked:
            lines.append(f"  {n:>4} x  {sr}")
        lines.append("")

    if stats["tool_calls"]:
        lines.append("Tool call distribution (top 10):")
        ranked = sorted(stats["tool_calls"].items(), key=lambda x: -x[1])[:10]
        for tc, n in ranked:
            lines.append(f"  {n:>4} x  {tc}")
        lines.append("")

    # Day 6: silent violation section
    potential = stats.get("potential_violations") or {}
    if potential:
        total_viol = sum(potential.values())
        n_sessions_viol = stats.get("sessions_with_violations", 0)
        lines.append(
            f"Silent violations detected: {total_viol} across "
            f"{n_sessions_viol} session(s)"
        )
        for tw_id, n in sorted(potential.items(), key=lambda x: -x[1]):
            lines.append(f"  {n:>4} x  {tw_id}")
        lines.append("")

    effectiveness = stats.get("effectiveness") or {}
    # Only show tripwires that have BOTH hits and at least one violation,
    # OR hits above some min count so we can judge effectiveness.
    judged = [
        (tw_id, e) for tw_id, e in effectiveness.items()
        if e["hits"] >= 1 and (e["violations"] > 0 or e["hits"] >= 3)
    ]
    if judged:
        lines.append("Tripwire effectiveness (violation rate = viol / hits):")
        for tw_id, e in sorted(judged, key=lambda x: -x[1]["rate"]):
            status = "OK" if e["rate"] == 0 else ("WARN" if e["rate"] < 0.5 else "FAIL")
            lines.append(
                f"  [{status:<4}] {tw_id:<32} "
                f"hits={e['hits']:<3} viol={e['violations']:<3} "
                f"rate={e['rate']:.2f}"
            )
        lines.append("")
        lines.append(
            "  Rate near 0 = lesson applied. Rate > 0.5 = lesson ignored "
            "(consider better formatting or blocking enforcement)."
        )
        lines.append("")

    if cold_tripwires:
        lines.append(
            f"Cold tripwires ({len(cold_tripwires)} never matched in window):"
        )
        for tw in cold_tripwires:
            lines.append(f"  - {tw}")
        lines.append("")
        lines.append(
            "  Cold tripwires are candidates for trigger tuning or removal."
        )
        lines.append("")

    # Day 13: primary vs fallback ratio (empirical measure of how much
    # work the Day-4 TF-IDF fallback is doing relative to the hand-written
    # rule engine)
    if ratio and (ratio["inject_events"] > 0 or ratio["fallback_events"] > 0):
        lines.append("Primary rule engine vs TF-IDF fallback:")
        lines.append(f"  Primary inject events:       {ratio['inject_events']}")
        lines.append(f"  Keyword fallback events:     {ratio['fallback_events']}")
        if ratio["fallback_to_inject_ratio"] is not None:
            lines.append(
                f"  Fallback / primary ratio:    {ratio['fallback_to_inject_ratio']}x"
            )
        lines.append(f"  Sessions: inject-only={ratio['sessions_inject_only']}, "
                     f"fallback-only={ratio['sessions_fallback_only']}, "
                     f"both={ratio['sessions_both']}, "
                     f"neither={ratio['sessions_neither']}")
        if ratio["fallback_to_inject_ratio"] and ratio["fallback_to_inject_ratio"] > 2:
            lines.append(
                "  (fallback > 2x means the rule engine vocabulary is too "
                "narrow; most briefs come from TF-IDF body scoring)"
            )
        lines.append("")

    return "\n".join(lines)


def render_timeline(
    session_id: str,
    events: list[dict[str, Any]],
    *,
    anonymize: bool = False,
    max_events: int = 200,
) -> str:
    """Render a single session's events as an ASCII timeline.

    Format:
      session: <sid>
      +HH:MM:SS  EVENT_TYPE   short description

    The timeline is relative to the first event in the session so the
    reader can see the cadence of activity. Used by the `cortex timeline`
    CLI subcommand and by documentation generators.
    """
    if not events:
        return f"session: {session_id}\n(no events)"

    first_ts = None
    for e in events:
        try:
            first_ts = datetime.fromisoformat(e.get("at", ""))
            break
        except (ValueError, TypeError):
            continue

    lines: list[str] = []
    display_sid = anonymize_session_id(session_id) if anonymize else session_id
    lines.append(f"Session timeline: {display_sid}")
    if len(events) > max_events:
        lines.append(f"  (showing first {max_events} of {len(events)} events)")
    lines.append("=" * 66)

    for event in events[:max_events]:
        ts_str = event.get("at", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if first_ts:
                delta = ts - first_ts
                rel = f"+{int(delta.total_seconds() // 3600):02d}:{int((delta.total_seconds() % 3600) // 60):02d}:{int(delta.total_seconds() % 60):02d}"
            else:
                rel = ts.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            rel = "?????????"

        ev_type = event.get("event", "?") or "?"

        if ev_type == "inject":
            rules = event.get("matched_rules") or []
            tws = event.get("tripwire_ids") or []
            synth = bool(event.get("synthesis_ids"))
            synth_tag = "  [SYNTH]" if synth else ""
            lines.append(f"  {rel}  INJECT      rules={','.join(rules)[:40]}")
            lines.append(f"             {len(tws)} tripwires: {','.join(tws)[:60]}{synth_tag}")
        elif ev_type == "keyword_fallback":
            tws = event.get("tripwire_ids") or []
            scores = event.get("scores") or []
            lines.append(f"  {rel}  FALLBACK    {len(tws)} tripwires: {','.join(tws)[:48]}")
            if scores:
                lines.append(f"             scores={scores}")
        elif ev_type == "tool_call":
            tool = event.get("tool_name", "?") or "?"
            snippet = event.get("input_snippet", "") or ""
            if anonymize and snippet:
                snippet = anonymize_snippet(snippet)
            elif snippet and len(snippet) > 80:
                snippet = snippet[:80] + "..."
            lines.append(f"  {rel}  tool_call   {tool}: {snippet}")
        elif ev_type == "potential_violation":
            tw_id = event.get("tripwire_id", "?")
            lines.append(f"  {rel}  VIOLATION!  {tw_id}")
        elif ev_type == "verifier_blocked":
            failed = event.get("failed_tripwires") or []
            lines.append(f"  {rel}  BLOCKED     verifier fail: {','.join(failed)[:50]}")
        else:
            lines.append(f"  {rel}  {ev_type:<12}")

    return "\n".join(lines)
