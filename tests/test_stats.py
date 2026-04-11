import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cortex.stats import (
    anonymize_session_id,
    anonymize_snippet,
    collect_sessions,
    compute_primary_vs_fallback_ratio,
    compute_stats,
    find_cold_tripwires,
    render_stats,
    render_timeline,
)


def _write_session(root: Path, sid: str, events: list[dict]) -> None:
    path = root / f"{sid}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_collect_sessions_reads_all_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "2026-04-11T10:00:00+00:00", "event": "tool_call", "tool_name": "Bash"},
    ])
    _write_session(tmp_path, "s2", [
        {"at": "2026-04-11T11:00:00+00:00", "event": "inject",
         "matched_rules": ["r1"], "tripwire_ids": ["t1"], "synthesis_ids": []},
    ])
    sessions = collect_sessions()
    ids = [s[0] for s in sessions]
    assert set(ids) == {"s1", "s2"}


def test_collect_sessions_skips_empty_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    _write_session(tmp_path, "s1", [
        {"at": "2026-04-11T10:00:00+00:00", "event": "tool_call", "tool_name": "Bash"},
    ])
    sessions = collect_sessions()
    assert len(sessions) == 1
    assert sessions[0][0] == "s1"


def test_collect_sessions_tolerates_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        "not json\n"
        '{"at":"2026-04-11T10:00:00+00:00","event":"tool_call","tool_name":"X"}\n'
        "also not json\n",
        encoding="utf-8",
    )
    sessions = collect_sessions()
    assert len(sessions) == 1
    assert len(sessions[0][1]) == 1  # one good line out of three


def test_collect_sessions_days_filter_excludes_old(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    _write_session(tmp_path, "old_s", [
        {"at": old, "event": "tool_call", "tool_name": "X"},
    ])
    _write_session(tmp_path, "new_s", [
        {"at": new, "event": "tool_call", "tool_name": "Y"},
    ])
    sessions = collect_sessions(days=7)
    ids = [s[0] for s in sessions]
    assert "new_s" in ids
    assert "old_s" not in ids


def test_collect_sessions_empty_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert collect_sessions() == []


def test_compute_stats_aggregates_correctly(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "2026-04-11T10:00:00+00:00", "event": "inject",
         "matched_rules": ["rule_a", "rule_b"],
         "tripwire_ids": ["tw1", "tw2"],
         "synthesis_ids": ["syn1"]},
        {"at": "2026-04-11T10:01:00+00:00", "event": "tool_call", "tool_name": "Bash"},
        {"at": "2026-04-11T10:02:00+00:00", "event": "tool_call", "tool_name": "Edit"},
    ])
    _write_session(tmp_path, "s2", [
        {"at": "2026-04-11T10:10:00+00:00", "event": "keyword_fallback",
         "n_hits": 2, "tripwire_ids": ["tw3"], "scores": [3.0]},
        {"at": "2026-04-11T10:11:00+00:00", "event": "tool_call", "tool_name": "Bash"},
    ])

    sessions = collect_sessions()
    stats = compute_stats(sessions)

    assert stats["n_sessions"] == 2
    assert stats["n_events"] == 5
    assert stats["sessions_with_inject"] == 1
    assert stats["sessions_with_fallback"] == 1
    assert stats["rules_hit"]["rule_a"] == 1
    assert stats["rules_hit"]["rule_b"] == 1
    assert stats["tripwires_hit"]["tw1"] == 1
    assert stats["tripwires_hit"]["tw3"] == 1
    assert stats["tool_calls"]["Bash"] == 2
    assert stats["tool_calls"]["Edit"] == 1
    assert stats["synthesis_hit"]["syn1"] == 1
    assert stats["avg_tool_calls_per_session"] == 1.5  # (2 + 1) / 2


def test_compute_stats_empty_sessions():
    stats = compute_stats([])
    assert stats["n_sessions"] == 0
    assert stats["n_events"] == 0
    assert stats["rules_hit"] == {}
    assert stats["avg_tool_calls_per_session"] == 0.0


def test_find_cold_tripwires_identifies_unmatched():
    stats = {"tripwires_hit": {"tw_warm1": 5, "tw_warm2": 2}}
    all_ids = ["tw_warm1", "tw_warm2", "tw_cold1", "tw_cold2"]
    cold = find_cold_tripwires(stats, all_ids)
    assert cold == ["tw_cold1", "tw_cold2"]


def test_find_cold_tripwires_empty_stats():
    stats = {"tripwires_hit": {}}
    cold = find_cold_tripwires(stats, ["a", "b", "c"])
    assert cold == ["a", "b", "c"]


def test_find_cold_tripwires_all_warm():
    stats = {"tripwires_hit": {"a": 1, "b": 2}}
    cold = find_cold_tripwires(stats, ["a", "b"])
    assert cold == []


def test_render_stats_includes_all_sections():
    stats = {
        "n_sessions": 3,
        "n_events": 10,
        "events_by_type": {"inject": 2, "tool_call": 7, "keyword_fallback": 1},
        "rules_hit": {"rule_a": 2},
        "tripwires_hit": {"tw1": 3, "tw2": 1},
        "synthesis_hit": {"syn1": 1},
        "tool_calls": {"Bash": 5, "Edit": 2},
        "sessions_with_inject": 2,
        "sessions_with_fallback": 1,
        "avg_tool_calls_per_session": 2.3,
    }
    cold = ["tw_cold"]
    out = render_stats(stats, cold, days=7)
    assert "last 7 days" in out
    assert "Sessions:" in out
    assert "rule_a" in out
    assert "tw1" in out
    assert "syn1" in out
    assert "Bash" in out
    assert "tw_cold" in out
    assert "Cold tripwires" in out


def test_render_stats_all_time_window():
    stats = {
        "n_sessions": 1,
        "n_events": 1,
        "events_by_type": {"tool_call": 1},
        "rules_hit": {},
        "tripwires_hit": {},
        "synthesis_hit": {},
        "tool_calls": {"Bash": 1},
        "sessions_with_inject": 0,
        "sessions_with_fallback": 0,
        "avg_tool_calls_per_session": 1.0,
    }
    out = render_stats(stats, [], days=None)
    assert "all-time" in out


# ---- Day 13: anonymize ----


def test_anonymize_session_id_is_stable():
    sid = "618b3f2a-ff6a-4baa-bd8c-add3f3d12fd6"
    a = anonymize_session_id(sid)
    b = anonymize_session_id(sid)
    assert a == b
    assert a.startswith("anon_")
    assert len(a) == len("anon_") + 8  # anon_ + 8 hex chars


def test_anonymize_session_id_different_inputs_differ():
    a = anonymize_session_id("session-a")
    b = anonymize_session_id("session-b")
    assert a != b


def test_anonymize_session_id_empty():
    assert anonymize_session_id("") == "anon_empty"


def test_anonymize_snippet_preserves_structure():
    snippet = "file=DETECTOR/bug.py | old=raw | new=df['slot_ts'] = (df['ts'] // 300) * 300"
    redacted = anonymize_snippet(snippet)
    # Key names survive, values are redacted with char counts
    assert "file=" in redacted
    assert "old=" in redacted
    assert "new=" in redacted
    assert "DETECTOR" not in redacted
    assert "slot_ts" not in redacted
    assert "REDACTED" in redacted


def test_anonymize_snippet_empty():
    assert anonymize_snippet("") == ""


# ---- Day 13: primary vs fallback ratio ----


def test_ratio_no_events(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    sessions = collect_sessions()
    ratio = compute_primary_vs_fallback_ratio(sessions)
    assert ratio["inject_events"] == 0
    assert ratio["fallback_events"] == 0
    assert ratio["fallback_to_inject_ratio"] is None


def test_ratio_fallback_dominates(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    _write_session(tmp_path, "s1", [
        {"at": "2026-04-11T10:00:00+00:00", "event": "inject",
         "matched_rules": ["r"], "tripwire_ids": ["t"], "synthesis_ids": []},
    ])
    for i in range(2, 8):
        _write_session(tmp_path, f"s{i}", [
            {"at": "2026-04-11T10:00:00+00:00", "event": "keyword_fallback",
             "n_hits": 1, "tripwire_ids": ["t"], "scores": [3.0]},
        ])
    sessions = collect_sessions()
    ratio = compute_primary_vs_fallback_ratio(sessions)
    assert ratio["inject_events"] == 1
    assert ratio["fallback_events"] == 6
    assert ratio["fallback_to_inject_ratio"] == 6.0
    assert ratio["sessions_inject_only"] == 1
    assert ratio["sessions_fallback_only"] == 6
    assert ratio["sessions_both"] == 0


def test_render_stats_with_ratio(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    stats = {
        "n_sessions": 3, "n_events": 5,
        "events_by_type": {"inject": 1, "keyword_fallback": 4},
        "rules_hit": {"r": 1}, "tripwires_hit": {"t": 5},
        "synthesis_hit": {}, "tool_calls": {},
        "sessions_with_inject": 1, "sessions_with_fallback": 2,
        "avg_tool_calls_per_session": 0.0,
    }
    ratio = {
        "inject_events": 1, "fallback_events": 4,
        "fallback_to_inject_ratio": 4.0,
        "sessions_inject_only": 0, "sessions_fallback_only": 2,
        "sessions_both": 1, "sessions_neither": 0,
    }
    out = render_stats(stats, [], days=7, ratio=ratio)
    assert "Primary rule engine vs TF-IDF fallback" in out
    assert "4.0x" in out
    assert "rule engine vocabulary is too narrow" in out


def test_render_stats_anonymize_header():
    stats = {
        "n_sessions": 0, "n_events": 0, "events_by_type": {},
        "rules_hit": {}, "tripwires_hit": {}, "synthesis_hit": {},
        "tool_calls": {}, "sessions_with_inject": 0,
        "sessions_with_fallback": 0, "avg_tool_calls_per_session": 0.0,
    }
    out = render_stats(stats, [], days=7, anonymize=True)
    assert "ANONYMIZED" in out
    assert "safe to share publicly" in out


# ---- Day 13: timeline ----


def test_render_timeline_empty():
    out = render_timeline("sid1", [])
    assert "no events" in out


def test_render_timeline_events():
    events = [
        {"at": "2026-04-11T10:00:00+00:00", "event": "inject",
         "matched_rules": ["poly_backtest_task"],
         "tripwire_ids": ["poly_fee_empirical", "real_entry_price"],
         "synthesis_ids": ["pm_5m_directional_block"]},
        {"at": "2026-04-11T10:00:05+00:00", "event": "tool_call",
         "tool_name": "Bash", "input_snippet": "echo hello"},
        {"at": "2026-04-11T10:05:30+00:00", "event": "potential_violation",
         "tripwire_id": "lookahead_parquet", "tool_name": "Edit"},
    ]
    out = render_timeline("my_session", events)
    assert "my_session" in out
    assert "INJECT" in out
    assert "poly_fee_empirical" in out
    assert "[SYNTH]" in out
    assert "tool_call" in out
    assert "Bash" in out
    assert "VIOLATION" in out
    assert "+00:00:05" in out  # relative time


def test_render_timeline_anonymize_hashes_sid():
    events = [
        {"at": "2026-04-11T10:00:00+00:00", "event": "tool_call",
         "tool_name": "Bash", "input_snippet": "secret command with value"},
    ]
    out = render_timeline("618b3f2a-ff6a-4baa-bd8c-add3f3d12fd6", events, anonymize=True)
    assert "618b3f2a" not in out
    assert "anon_" in out
    assert "secret command" not in out
    assert "REDACTED" in out


def test_render_timeline_truncates_at_max_events():
    events = [
        {"at": f"2026-04-11T10:00:0{i}+00:00", "event": "tool_call",
         "tool_name": "Bash", "input_snippet": f"echo {i}"}
        for i in range(10)
    ]
    out = render_timeline("sid", events, max_events=3)
    assert "showing first 3 of 10" in out


def test_render_stats_omits_empty_sections():
    stats = {
        "n_sessions": 1,
        "n_events": 1,
        "events_by_type": {"tool_call": 1},
        "rules_hit": {},
        "tripwires_hit": {},
        "synthesis_hit": {},
        "tool_calls": {"Bash": 1},
        "sessions_with_inject": 0,
        "sessions_with_fallback": 0,
        "avg_tool_calls_per_session": 1.0,
    }
    out = render_stats(stats, [], days=None)
    # Should not contain headers for empty rule/tripwire/synthesis sections
    assert "Top matched rules:" not in out
    assert "Top matched tripwires:" not in out
    assert "Synthesis rules fired:" not in out
    assert "Cold tripwires" not in out


def test_compute_stats_handles_null_fields():
    """Events with missing optional fields should not crash aggregation."""
    sessions = [
        ("s1", [
            {"at": "2026-04-11T10:00:00+00:00", "event": "inject",
             "matched_rules": None, "tripwire_ids": None, "synthesis_ids": None},
            {"at": "2026-04-11T10:01:00+00:00", "event": "tool_call"},  # no tool_name
        ]),
    ]
    stats = compute_stats(sessions)
    assert stats["n_sessions"] == 1
    assert stats["sessions_with_inject"] == 1
    assert stats["rules_hit"] == {}
