import io
import json
import sys
from unittest.mock import patch

from cortex import watch
from cortex.session import read_session


def _run_watch(stdin_text: str) -> int:
    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return watch.main()


def test_watch_empty_stdin_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert _run_watch("") == 0


def test_watch_invalid_json_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert _run_watch("not json") == 0


def test_watch_logs_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    payload = {
        "session_id": "test-sess",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("test-sess")
    assert len(events) == 1
    assert events[0]["event"] == "tool_call"
    assert events[0]["tool_name"] == "Bash"
    assert events[0]["input_snippet"] == "echo hello"


def test_watch_missing_tool_name_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert _run_watch(json.dumps({"session_id": "abc"})) == 0
    assert read_session("abc") == []


def test_watch_missing_session_id_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    assert _run_watch(json.dumps({"tool_name": "Bash"})) == 0
