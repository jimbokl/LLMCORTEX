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


# ---- Day 14: Surprise Engine transcript capture ----


def _write_transcript(path, assistant_text: str) -> None:
    """Minimal Claude Code transcript jsonl with one assistant message."""
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_watch_logs_prediction_from_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        "Let me verify.\n"
        "<cortex_predict>\n"
        "  <outcome>all tests pass</outcome>\n"
        "  <failure_mode>stale cache</failure_mode>\n"
        "</cortex_predict>",
    )
    payload = {
        "session_id": "sess-predict",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stdout": "all ok\n"},
        "transcript_path": str(transcript),
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-predict")
    # prediction must come before tool_call so collect_pairs can pair them.
    kinds = [e["event"] for e in events]
    assert kinds == ["prediction", "tool_call"]
    assert events[0]["outcome"] == "all tests pass"
    assert events[0]["failure_mode"] == "stale cache"
    assert events[1]["tool_name"] == "Bash"
    assert events[1]["response_snippet"] == "all ok"


def test_watch_dedup_prediction_across_multiple_tool_calls(tmp_path, monkeypatch):
    """If one assistant message has two tool_use blocks, PostToolUse
    fires twice and both invocations read the same transcript. The
    prediction event must NOT be duplicated."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        "<cortex_predict>"
        "<outcome>both tools succeed</outcome>"
        "<failure_mode>path mismatch</failure_mode>"
        "</cortex_predict>",
    )
    payload1 = {
        "session_id": "sess-dedup",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "transcript_path": str(transcript),
    }
    payload2 = {
        "session_id": "sess-dedup",
        "tool_name": "Read",
        "tool_input": {"file_path": "a.py"},
        "transcript_path": str(transcript),
    }
    assert _run_watch(json.dumps(payload1)) == 0
    assert _run_watch(json.dumps(payload2)) == 0
    events = read_session("sess-dedup")
    kinds = [e["event"] for e in events]
    # One prediction, two tool_calls.
    assert kinds.count("prediction") == 1
    assert kinds.count("tool_call") == 2


def test_watch_captures_predict_from_earlier_turn_message(tmp_path, monkeypatch):
    """Day 14 bug regression: agent emits <cortex_predict> in a
    text-only preamble message, then tool_use fires in a LATER
    assistant message of the same agent turn. The prediction must
    still be logged when PostToolUse runs."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    rows = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "do X"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Preamble.\n<cortex_predict>"
                            "<outcome>all green</outcome>"
                            "<failure_mode>env broken</failure_mode>"
                            "</cortex_predict>"
                        ),
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running Bash now."},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        },
    ]
    with open(transcript, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    payload = {
        "session_id": "sess-predict-earlier",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "transcript_path": str(transcript),
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-predict-earlier")
    kinds = [e["event"] for e in events]
    assert kinds == ["prediction", "tool_call"]
    assert events[0]["outcome"] == "all green"
    assert events[0]["failure_mode"] == "env broken"


def test_watch_ignores_predict_from_previous_human_turn(tmp_path, monkeypatch):
    """Day 14 scope guard: a prediction from a previous agent turn
    (before a new human message) must NOT be paired with the current
    tool call -- it would pollute the surprise log with cross-task
    noise."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    rows = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "first task"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<cortex_predict>"
                            "<outcome>stale</outcome>"
                            "<failure_mode>old</failure_mode>"
                            "</cortex_predict>"
                        ),
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "new task"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "no predict here"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        },
    ]
    with open(transcript, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    payload = {
        "session_id": "sess-stale-predict",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "transcript_path": str(transcript),
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-stale-predict")
    kinds = [e["event"] for e in events]
    assert kinds == ["tool_call"]


def test_watch_no_prediction_when_transcript_absent(tmp_path, monkeypatch):
    """No transcript_path -> no prediction event, still logs tool_call."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    payload = {
        "session_id": "sess-notranscript",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-notranscript")
    assert [e["event"] for e in events] == ["tool_call"]


def test_watch_logs_tool_response_snippet(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    payload = {
        "session_id": "sess-resp",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"stdout": "3 passed in 0.1s"},
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-resp")
    assert events[0]["response_snippet"] == "3 passed in 0.1s"
