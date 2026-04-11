"""Tests for cortex/suggest_patterns.py (Day 9)."""
from __future__ import annotations

import json
from pathlib import Path

from cortex.suggest_patterns import (
    _extract_identifiers,
    analyze_snippets,
    collect_post_injection_snippets,
    render_suggestions,
)


def _write_session(root: Path, sid: str, events: list[dict]) -> None:
    path = root / f"{sid}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


# ---------- _extract_identifiers ----------


def test_extract_identifiers_catches_snake_case():
    s = "file=DETECTOR/backfill_features.py | new=slot_ts = ts // 300"
    idents = _extract_identifiers(s)
    assert "slot_ts" in idents
    assert "DETECTOR" in idents
    assert "backfill_features" in idents


def test_extract_identifiers_skips_short():
    s = "a b cd efg hij"
    idents = _extract_identifiers(s)
    assert "efg" in idents
    assert "hij" in idents
    assert "a" not in idents
    assert "cd" not in idents  # 2 chars, below min of 3


def test_extract_identifiers_ignores_pure_numbers():
    s = "300 600 foo_bar"
    idents = _extract_identifiers(s)
    assert "foo_bar" in idents
    # Pure numbers do not start with [A-Za-z_] so they're skipped
    assert "300" not in idents


# ---------- collect_post_injection_snippets ----------


def test_collect_returns_empty_for_no_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert collect_post_injection_snippets("lookahead_parquet") == []


def test_collect_finds_tool_calls_after_inject(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "2026-04-11T00:00:00Z", "event": "inject",
         "matched_rules": ["r1"],
         "tripwire_ids": ["lookahead_parquet"],
         "synthesis_ids": []},
        {"at": "2026-04-11T00:00:01Z", "event": "tool_call",
         "tool_name": "Edit",
         "input_snippet": "file=DETECTOR/backfill.py | new=slot_ts = (ts // 300) * 300"},
        {"at": "2026-04-11T00:00:02Z", "event": "tool_call",
         "tool_name": "Bash",
         "input_snippet": "python DETECTOR/backfill.py"},
    ])
    findings = collect_post_injection_snippets("lookahead_parquet")
    assert len(findings) == 1
    assert findings[0]["session_id"] == "s1"
    assert findings[0]["inject_type"] == "inject"
    assert len(findings[0]["tool_calls"]) == 2


def test_collect_respects_window(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    events = [
        {"at": "t0", "event": "inject", "tripwire_ids": ["tw"],
         "matched_rules": [], "synthesis_ids": []},
    ]
    # Add 20 tool_calls after the inject
    for i in range(20):
        events.append({
            "at": f"t{i+1}",
            "event": "tool_call",
            "tool_name": "Bash",
            "input_snippet": f"echo {i}",
        })
    _write_session(tmp_path, "s1", events)
    findings = collect_post_injection_snippets("tw", window=5)
    assert len(findings[0]["tool_calls"]) == 5
    assert findings[0]["tool_calls"][0]["input_snippet"] == "echo 0"
    assert findings[0]["tool_calls"][4]["input_snippet"] == "echo 4"


def test_collect_matches_keyword_fallback_events(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "t0", "event": "keyword_fallback",
         "n_hits": 1, "tripwire_ids": ["lookahead_parquet"],
         "scores": [3.0]},
        {"at": "t1", "event": "tool_call", "tool_name": "Edit",
         "input_snippet": "something"},
    ])
    findings = collect_post_injection_snippets("lookahead_parquet")
    assert len(findings) == 1
    assert findings[0]["inject_type"] == "keyword_fallback"


def test_collect_skips_other_tripwires(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "t0", "event": "inject",
         "matched_rules": [], "tripwire_ids": ["other_tripwire"],
         "synthesis_ids": []},
        {"at": "t1", "event": "tool_call", "tool_name": "Edit", "input_snippet": "x"},
    ])
    findings = collect_post_injection_snippets("lookahead_parquet")
    assert findings == []


def test_collect_multiple_injections_in_same_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "t0", "event": "inject",
         "matched_rules": [], "tripwire_ids": ["tw"], "synthesis_ids": []},
        {"at": "t1", "event": "tool_call", "tool_name": "Bash", "input_snippet": "a"},
        {"at": "t2", "event": "inject",
         "matched_rules": [], "tripwire_ids": ["tw"], "synthesis_ids": []},
        {"at": "t3", "event": "tool_call", "tool_name": "Bash", "input_snippet": "b"},
    ])
    findings = collect_post_injection_snippets("tw", window=10)
    assert len(findings) == 2
    # First finding gets all 3 remaining tool_calls? No -- only tool_calls,
    # and only in window 10. Let's check: from i=0, next 10 events are
    # indices 1-3 (3 events, only 2 tool_calls). Second finding starts at i=2,
    # next 10 events are indices 3 (1 tool_call).
    assert len(findings[0]["tool_calls"]) == 2  # indices 1, 3 are tool_calls
    assert len(findings[1]["tool_calls"]) == 1


# ---------- analyze_snippets ----------


def test_analyze_empty_findings():
    result = analyze_snippets([])
    assert result["n_injections"] == 0
    assert result["n_tool_calls"] == 0
    assert result["common_words"] == []


def test_analyze_counts_by_tool():
    findings = [{
        "session_id": "s1",
        "inject_at": "t0",
        "inject_type": "inject",
        "tool_calls": [
            {"tool_name": "Edit", "input_snippet": "a"},
            {"tool_name": "Edit", "input_snippet": "b"},
            {"tool_name": "Bash", "input_snippet": "c"},
        ],
    }]
    result = analyze_snippets(findings)
    assert result["n_tool_calls"] == 3
    assert result["by_tool"] == {"Edit": 2, "Bash": 1}


def test_analyze_finds_common_words():
    findings = [{
        "session_id": "s1",
        "inject_at": "t0",
        "inject_type": "inject",
        "tool_calls": [
            {"tool_name": "Edit",
             "input_snippet": "file=DETECTOR/a.py | new=slot_ts = x // 300"},
            {"tool_name": "Edit",
             "input_snippet": "file=DETECTOR/b.py | new=slot_ts = y // 300"},
            {"tool_name": "Edit",
             "input_snippet": "file=DETECTOR/c.py | new=slot_ts = z // 300"},
        ],
    }]
    result = analyze_snippets(findings)
    common = dict(result["common_words"])
    # These should all show up in >= 50% of 3 snippets
    assert common.get("slot_ts") == 3
    assert common.get("DETECTOR") == 3
    assert common.get("file") == 3


def test_analyze_ignores_empty_snippets():
    findings = [{
        "session_id": "s1",
        "inject_at": "t0",
        "inject_type": "inject",
        "tool_calls": [
            {"tool_name": "Edit", "input_snippet": ""},
            {"tool_name": "Bash"},  # missing input_snippet
        ],
    }]
    result = analyze_snippets(findings)
    assert result["n_snippets"] == 0
    assert result["common_words"] == []


# ---------- render_suggestions ----------


def test_render_empty_findings_shows_cold_tripwire_message():
    text = render_suggestions("tw_cold", [], {"n_injections": 0, "by_tool": {},
                                               "snippets_by_tool": {},
                                               "common_words": []})
    assert "COLD" in text
    assert "No past injections" in text
    assert "tw_cold" in text


def test_render_populated_report_includes_sections():
    findings = [{
        "session_id": "s1",
        "inject_at": "t0",
        "inject_type": "inject",
        "tool_calls": [
            {"tool_name": "Edit", "input_snippet": "slot_ts = ts // 300"},
        ],
    }]
    analysis = analyze_snippets(findings)
    text = render_suggestions("lookahead_parquet", findings, analysis)
    assert "lookahead_parquet" in text
    assert "1 past injection" in text
    assert "Tool call distribution" in text
    assert "Edit" in text


def test_render_truncates_long_snippets():
    long = "x" * 500
    findings = [{
        "session_id": "s1",
        "inject_at": "t0",
        "inject_type": "inject",
        "tool_calls": [{"tool_name": "Edit", "input_snippet": long}],
    }]
    analysis = analyze_snippets(findings)
    text = render_suggestions("tw", findings, analysis, snippet_preview_chars=50)
    # Should not contain the full 500 xs
    assert "..." in text
    xs = text.count("x")
    assert xs < 200
