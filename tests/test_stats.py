import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cortex.stats import (
    collect_sessions,
    compute_stats,
    find_cold_tripwires,
    render_stats,
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
