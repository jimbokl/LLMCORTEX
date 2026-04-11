from cortex.session import log_event, read_session


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
