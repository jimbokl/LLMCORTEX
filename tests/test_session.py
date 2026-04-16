import os
import time

import pytest

from cortex.session import log_event, prune_sessions, read_session


def test_log_event_creates_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert log_event("abc-123", "inject", {"foo": "bar"}) is True
    events = read_session("abc-123")
    assert len(events) == 1
    assert events[0]["event"] == "inject"
    assert events[0]["foo"] == "bar"
    assert "at" in events[0]


def test_log_event_empty_session_id_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert log_event("", "inject", {}) is False


def test_log_event_appends_multiple_events(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    log_event("sess-1", "inject", {"n": 1})
    log_event("sess-1", "tool_call", {"n": 2})
    log_event("sess-1", "inject", {"n": 3})
    events = read_session("sess-1")
    assert len(events) == 3
    assert [e["n"] for e in events] == [1, 2, 3]


def test_log_event_sanitizes_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    # Path traversal attempt -- all non-alnum/-/_ chars stripped
    log_event("../evil/../path", "inject", {})
    # No file outside sessions dir should be created
    assert not (tmp_path.parent / "evil").exists()
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1


def test_read_missing_session_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert read_session("nonexistent") == []


def test_read_session_empty_id_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert read_session("") == []


def _make_aged_jsonl(path, age_days: float) -> None:
    path.write_text('{"at":"x","event":"test"}\n', encoding="utf-8")
    past = time.time() - age_days * 86400
    os.utime(path, (past, past))


def test_prune_sessions_removes_old_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    old1 = tmp_path / "old-a.jsonl"
    old2 = tmp_path / "old-b.jsonl"
    fresh = tmp_path / "fresh.jsonl"
    _make_aged_jsonl(old1, age_days=45)
    _make_aged_jsonl(old2, age_days=60)
    _make_aged_jsonl(fresh, age_days=1)

    n, deleted = prune_sessions(30)
    assert n == 2
    assert set(deleted) == {"old-a.jsonl", "old-b.jsonl"}
    assert not old1.exists()
    assert not old2.exists()
    assert fresh.exists()


def test_prune_sessions_zero_days_deletes_everything(tmp_path, monkeypatch):
    # Edge case: --days 0 means "keep only files from the future", which
    # reduces to deleting every existing log. Useful for clean resets.
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    _make_aged_jsonl(a, age_days=0.1)
    _make_aged_jsonl(b, age_days=0.01)

    n, _ = prune_sessions(0)
    # Both files have non-positive age relative to "now - 0 days" boundary.
    assert n == 2


def test_prune_sessions_negative_days_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        prune_sessions(-1)


def test_prune_sessions_missing_dir_is_noop(tmp_path, monkeypatch):
    # Fail-safe: pointing the prune at a non-existent directory must
    # report zero deletions rather than raise.
    missing = tmp_path / "does_not_exist"
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(missing))
    n, deleted = prune_sessions(7, sessions_path=missing)
    assert n == 0
    assert deleted == []


def test_prune_sessions_preserves_recent_writes(tmp_path, monkeypatch):
    # A fresh log_event should never be touched by a same-second prune —
    # mtime is the last-write time, and files freshly written have
    # mtime ~ now.
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    log_event("sess-live", "inject", {"n": 1})
    n, _ = prune_sessions(30)
    assert n == 0
    assert (tmp_path / "sess-live.jsonl").exists()
