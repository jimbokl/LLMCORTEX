"""Tests for silent violation detection (Day 6)."""
from pathlib import Path

from cortex.importers.memory_md import run_migration
from cortex.session import log_event
from cortex.violation_detect import (
    detect_violations,
    get_active_tripwires,
    summarize_tool_input,
)


def _seeded_store(tmp_path: Path) -> str:
    db = str(tmp_path / "seed.db")
    run_migration(db)
    return db


def test_summarize_bash_truncates():
    snippet = summarize_tool_input("Bash", {"command": "echo " + "x" * 1000})
    assert len(snippet) <= 500
    assert snippet.startswith("echo ")


def test_summarize_edit_includes_file_and_diff():
    snippet = summarize_tool_input(
        "Edit",
        {
            "file_path": "foo.py",
            "old_string": "a = 1",
            "new_string": "a = 2",
        },
    )
    assert "file=foo.py" in snippet
    assert "old=a = 1" in snippet
    assert "new=a = 2" in snippet


def test_summarize_write_with_content():
    snippet = summarize_tool_input(
        "Write",
        {"file_path": "x.py", "content": "print('hi')"},
    )
    assert "file=x.py" in snippet
    assert "new=print('hi')" in snippet


def test_summarize_read_logs_path_only():
    snippet = summarize_tool_input("Read", {"file_path": "/path/to/file.py"})
    assert "file_path=/path/to/file.py" in snippet


def test_summarize_unknown_tool_falls_back_to_json():
    snippet = summarize_tool_input("Weird", {"arg1": "x", "arg2": 42})
    assert '"arg1"' in snippet
    assert '"arg2"' in snippet


def test_summarize_empty_or_none():
    assert summarize_tool_input("Bash", None) == ""
    assert summarize_tool_input("Bash", {}) == ""


def test_get_active_tripwires_empty_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)
    assert get_active_tripwires("nonexistent") == []


def test_get_active_tripwires_returns_only_pattern_tripwires(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    # Inject a tripwire WITH patterns and one WITHOUT
    log_event("sess1", "inject", {
        "matched_rules": ["r"],
        "tripwire_ids": ["lookahead_parquet", "never_single_strategy"],
        "synthesis_ids": [],
    })

    active = get_active_tripwires("sess1")
    ids = {tw["id"] for tw in active}
    # lookahead_parquet has patterns (seeded in Day 6)
    assert "lookahead_parquet" in ids
    # never_single_strategy has no patterns so it's filtered out
    assert "never_single_strategy" not in ids


def test_detect_violations_matches_lookahead_pattern(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    log_event("sess_bug", "inject", {
        "matched_rules": ["poly_backtest_task"],
        "tripwire_ids": ["lookahead_parquet"],
        "synthesis_ids": [],
    })

    bug_snippet = "df['slot_ts'] = (df['ts'] // 300) * 300"
    violations = detect_violations("sess_bug", "Bash", bug_snippet)
    assert len(violations) == 1
    assert violations[0]["tripwire_id"] == "lookahead_parquet"
    assert violations[0]["tool_name"] == "Bash"
    assert "slot_ts" in violations[0]["pattern"]


def test_detect_violations_skips_honest_shift(tmp_path, monkeypatch):
    """The fix pattern `(ts // N) * N + N` must NOT be flagged."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    log_event("sess_fixed", "inject", {
        "matched_rules": ["poly_backtest_task"],
        "tripwire_ids": ["lookahead_parquet"],
        "synthesis_ids": [],
    })

    fixed_snippet = "df['slot_ts'] = (df['ts'] // 300) * 300 + 300"
    violations = detect_violations("sess_fixed", "Bash", fixed_snippet)
    assert violations == []


def test_detect_violations_matches_0_50_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    log_event("sess_entry", "inject", {
        "matched_rules": ["poly_backtest_task"],
        "tripwire_ids": ["real_entry_price"],
        "synthesis_ids": [],
    })

    bug_snippet = "entry_price = 0.5"
    violations = detect_violations("sess_entry", "Edit", bug_snippet)
    assert len(violations) == 1
    assert violations[0]["tripwire_id"] == "real_entry_price"


def test_detect_violations_no_inject_means_no_detect(tmp_path, monkeypatch):
    """Detection should not fire for tripwires that weren't injected."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    # No inject events in this session
    log_event("sess_clean", "tool_call", {"tool_name": "Bash"})

    bug_snippet = "df['slot_ts'] = (df['ts'] // 300) * 300"
    violations = detect_violations("sess_clean", "Bash", bug_snippet)
    assert violations == []


def test_detect_violations_empty_snippet_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    assert detect_violations("x", "Bash", "") == []
    assert detect_violations("", "Bash", "anything") == []


def test_detect_violations_one_per_tripwire(tmp_path, monkeypatch):
    """Multiple pattern matches on the same tripwire count once."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    log_event("sess_multi", "inject", {
        "matched_rules": ["r"],
        "tripwire_ids": ["real_entry_price"],
        "synthesis_ids": [],
    })

    # Two patterns of real_entry_price would both match this
    bug_snippet = "entry = 0.5 and up_ask = 0.5"
    violations = detect_violations("sess_multi", "Edit", bug_snippet)
    assert len(violations) == 1


def test_detect_violations_via_keyword_fallback(tmp_path, monkeypatch):
    """Tripwires surfaced by keyword_fallback count as active."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = _seeded_store(tmp_path)
    monkeypatch.setenv("CORTEX_DB", db)

    log_event("sess_fb", "keyword_fallback", {
        "n_hits": 1,
        "tripwire_ids": ["lookahead_parquet"],
        "scores": [3.0],
    })

    bug_snippet = "df['slot_ts'] = (df['ts'] // 300) * 300"
    violations = detect_violations("sess_fb", "Bash", bug_snippet)
    assert len(violations) == 1
