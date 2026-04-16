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
    # Tier 1.5 Wire B: every invocation emits one predict_scan diagnostic
    # event in addition to the tool_call. Filter it out for tool_call
    # assertions so existing expectations on input_snippet etc. remain
    # framed on the real audit payload.
    tool_calls = [e for e in events if e["event"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "Bash"
    assert tool_calls[0]["input_snippet"] == "echo hello"


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
    kinds = [e["event"] for e in events]
    # Tier 1.5 Wire B: predict_scan precedes prediction; tool_call last.
    assert kinds == ["predict_scan", "prediction", "tool_call"]
    assert events[0]["prediction_found"] is True
    assert events[0]["transcript_path_present"] is True
    assert events[1]["outcome"] == "all tests pass"
    assert events[1]["failure_mode"] == "stale cache"
    assert events[2]["tool_name"] == "Bash"
    assert events[2]["response_snippet"] == "all ok"


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
    # One prediction (dedup), two tool_calls, two predict_scan diagnostics.
    assert kinds.count("prediction") == 1
    assert kinds.count("tool_call") == 2
    assert kinds.count("predict_scan") == 2


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
    assert kinds == ["predict_scan", "prediction", "tool_call"]
    assert events[1]["outcome"] == "all green"
    assert events[1]["failure_mode"] == "env broken"


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
    # predict_scan fires (prediction_found=False because scope guard
    # rejects the stale block); no prediction event; tool_call follows.
    assert kinds == ["predict_scan", "tool_call"]
    assert events[0]["prediction_found"] is False


def test_watch_no_prediction_when_transcript_absent(tmp_path, monkeypatch):
    """No transcript_path -> predict_scan with path_present=False,
    no prediction event, still logs tool_call."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    payload = {
        "session_id": "sess-notranscript",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-notranscript")
    kinds = [e["event"] for e in events]
    assert kinds == ["predict_scan", "tool_call"]
    scan = events[0]
    assert scan["transcript_path_present"] is False
    assert scan["assistant_text_len"] == 0
    assert scan["prediction_found"] is False


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
    tool_calls = [e for e in events if e["event"] == "tool_call"]
    assert tool_calls[0]["response_snippet"] == "3 passed in 0.1s"


# ---- Tier 1.5 Wire A: persist violations into the store ----


def test_watch_persists_violation_into_store(tmp_path, monkeypatch):
    """When detect_violations returns a hit, watch must not only log
    the jsonl event but also record_violation() on the store so the
    per-tripwire counter moves off zero.
    """
    from cortex.importers.memory_md import run_migration
    from cortex.session import log_event
    from cortex.store import CortexStore

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(sessions_dir))
    db = tmp_path / ".cortex" / "store.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    run_migration(str(db))
    monkeypatch.setenv("CORTEX_DB", str(db))

    # Prime the session with an `inject` event naming the tripwire we
    # want detect_violations to match against. `lookahead_parquet` has
    # a real violation_pattern in the seed so this test uses the real
    # detector, not a stub.
    log_event(
        "sess-wire-a",
        "inject",
        {
            "matched_rules": ["poly_backtest_task"],
            "tripwire_ids": ["lookahead_parquet"],
        },
    )

    # Bash command that hits the seeded pattern
    # r"slot_ts[^\n]*?=[^\n]*?//\s*\d+[^\n]*?\*\s*\d+\b(?!\s*\+)"
    payload = {
        "session_id": "sess-wire-a",
        "tool_name": "Bash",
        "tool_input": {"command": "python -c \"slot_ts = (ts // 300) * 300\""},
    }
    assert _run_watch(json.dumps(payload)) == 0

    events = read_session("sess-wire-a")
    violations = [e for e in events if e["event"] == "potential_violation"]
    assert violations, "detect_violations should have fired on the seeded pattern"

    # Counter moved off zero in the store — this is the new behavior.
    store = CortexStore(str(db))
    try:
        tw = store.get_tripwire("lookahead_parquet")
        assert tw is not None
        assert tw["violation_count"] >= 1
        rows = store.list_violations("lookahead_parquet")
        assert rows, "violations table should also have the row"
        assert rows[0]["session_id"] == "sess-wire-a"
    finally:
        store.close()


def test_watch_violation_persist_failure_does_not_break_log(tmp_path, monkeypatch):
    """Store failure (e.g. locked db) must not prevent the jsonl
    potential_violation event from being written. Fail-open contract.
    """
    from cortex.importers.memory_md import run_migration
    from cortex.session import log_event

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(sessions_dir))
    db = tmp_path / ".cortex" / "store.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    run_migration(str(db))
    monkeypatch.setenv("CORTEX_DB", str(db))

    log_event(
        "sess-wire-a-fail",
        "inject",
        {
            "matched_rules": ["poly_backtest_task"],
            "tripwire_ids": ["lookahead_parquet"],
        },
    )

    # Break CortexStore so any attempt to record violations raises.
    import cortex.store as _store_mod

    orig_record = _store_mod.CortexStore.record_violation

    def _boom(self, **_kw):
        raise RuntimeError("simulated store lock")

    monkeypatch.setattr(_store_mod.CortexStore, "record_violation", _boom)
    try:
        payload = {
            "session_id": "sess-wire-a-fail",
            "tool_name": "Bash",
            "tool_input": {"command": "python -c \"slot_ts = (ts // 300) * 300\""},
        }
        assert _run_watch(json.dumps(payload)) == 0
    finally:
        _store_mod.CortexStore.record_violation = orig_record

    events = read_session("sess-wire-a-fail")
    kinds = [e["event"] for e in events]
    # jsonl still has the violation event even though the store call blew up
    assert "potential_violation" in kinds


# ---- Tier 1.5 Wire B: predict_scan diagnostic event ----


def test_predict_scan_always_fires_when_payload_reaches_watch(tmp_path, monkeypatch):
    """predict_scan is the `why-is-the-prediction-log-empty` diagnostic.
    It must fire on every watch invocation that passes the session_id /
    tool_name guards, so over a week of data the operator can answer
    "is transcript_path missing?" vs "does Claude just not emit blocks?".
    """
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    payload = {
        "session_id": "sess-scan",
        "tool_name": "Bash",
        "tool_input": {"command": "echo"},
    }
    assert _run_watch(json.dumps(payload)) == 0
    events = read_session("sess-scan")
    scans = [e for e in events if e["event"] == "predict_scan"]
    assert len(scans) == 1
    for key in ("transcript_path_present", "assistant_text_len", "prediction_found"):
        assert key in scans[0]
