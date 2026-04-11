"""Day 14 -- Surprise Engine unit tests.

Covers parse_prediction (regex edge cases), read_last_assistant_text
(Claude Code transcript jsonl shapes), collect_pairs (prediction/
tool_call pairing), and render_surprise_table (human output).
"""
import json
from pathlib import Path

from cortex import surprise
from cortex.session import log_event


def test_parse_prediction_happy_path():
    text = (
        "Let me check this.\n"
        "<cortex_predict>\n"
        "  <outcome>tests pass with 0 lookahead warnings</outcome>\n"
        "  <failure_mode>slot_ts uses (ts // N) * N without forward shift</failure_mode>\n"
        "</cortex_predict>\n"
    )
    p = surprise.parse_prediction(text)
    assert p is not None
    assert p["outcome"] == "tests pass with 0 lookahead warnings"
    assert "slot_ts uses" in p["failure_mode"]


def test_parse_prediction_multiline_inside_fields():
    text = (
        "<cortex_predict>\n"
        "  <outcome>\n"
        "    Multi-line outcome\n"
        "    with newlines inside\n"
        "  </outcome>\n"
        "  <failure_mode>\n"
        "    Also spanning\n"
        "    multiple lines\n"
        "  </failure_mode>\n"
        "</cortex_predict>"
    )
    p = surprise.parse_prediction(text)
    assert p is not None
    # Whitespace is collapsed to single spaces.
    assert p["outcome"] == "Multi-line outcome with newlines inside"
    assert p["failure_mode"] == "Also spanning multiple lines"


def test_parse_prediction_missing_tag_returns_none():
    assert surprise.parse_prediction("no tag here") is None
    assert surprise.parse_prediction("") is None
    assert surprise.parse_prediction(None) is None  # type: ignore[arg-type]


def test_parse_prediction_malformed_returns_none():
    # Missing failure_mode tag.
    text = "<cortex_predict><outcome>just this</outcome></cortex_predict>"
    assert surprise.parse_prediction(text) is None


def test_parse_prediction_first_block_wins():
    """If the agent somehow emits two blocks in one message, take the first."""
    text = (
        "<cortex_predict><outcome>first</outcome>"
        "<failure_mode>fm1</failure_mode></cortex_predict>"
        "<cortex_predict><outcome>second</outcome>"
        "<failure_mode>fm2</failure_mode></cortex_predict>"
    )
    p = surprise.parse_prediction(text)
    assert p is not None
    assert p["outcome"] == "first"
    assert p["failure_mode"] == "fm1"


def test_parse_prediction_caps_field_length():
    big = "x" * 2000
    text = (
        f"<cortex_predict><outcome>{big}</outcome>"
        f"<failure_mode>{big}</failure_mode></cortex_predict>"
    )
    p = surprise.parse_prediction(text)
    assert p is not None
    assert len(p["outcome"]) == 500
    assert len(p["failure_mode"]) == 500


def test_read_last_assistant_text_picks_most_recent(tmp_path):
    """The walker must return the LAST assistant entry, skipping user
    entries, thinking blocks, and tool_use blocks."""
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"content": "hi"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden reasoning"},
                    {"type": "text", "text": "older assistant message"},
                ],
            },
        },
        {"type": "user", "message": {"content": "follow up"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "latest visible reply"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        },
    ]
    with open(transcript, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    text = surprise.read_last_assistant_text(str(transcript))
    assert text == "latest visible reply"


def test_read_last_assistant_text_missing_file_returns_empty(tmp_path):
    assert surprise.read_last_assistant_text(tmp_path / "nope.jsonl") == ""
    assert surprise.read_last_assistant_text("") == ""
    assert surprise.read_last_assistant_text(None) == ""


def test_read_last_assistant_text_tolerates_bad_lines(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "garbage line\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "survived"}],
                },
            }
        )
        + "\n"
        + "another garbage\n",
        encoding="utf-8",
    )
    assert surprise.read_last_assistant_text(str(transcript)) == "survived"


def test_collect_pairs_pairs_prediction_with_next_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    log_event("sess1", "inject", {"tripwire_ids": ["t_a", "t_b"]})
    log_event(
        "sess1",
        "prediction",
        {"outcome": "all green", "failure_mode": "rate limit"},
    )
    log_event(
        "sess1",
        "tool_call",
        {"tool_name": "Bash", "input_snippet": "pytest -q", "response_snippet": "OK"},
    )
    pairs = surprise.collect_pairs(sessions_root=Path(tmp_path))
    assert len(pairs) == 1
    p = pairs[0]
    assert p["session_id"] == "sess1"
    assert p["outcome"] == "all green"
    assert p["failure_mode"] == "rate limit"
    assert p["tool_name"] == "Bash"
    assert p["tool_snippet"] == "pytest -q"
    assert p["tool_response"] == "OK"
    assert p["tripwire_ids"] == ["t_a", "t_b"]


def test_collect_pairs_orphan_prediction_without_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    log_event(
        "sess_orphan",
        "prediction",
        {"outcome": "never ran", "failure_mode": "abort"},
    )
    pairs = surprise.collect_pairs(sessions_root=Path(tmp_path))
    assert len(pairs) == 1
    assert pairs[0]["tool_name"] is None
    assert pairs[0]["outcome"] == "never ran"


def test_collect_pairs_two_predictions_in_row_ship_the_earlier(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    log_event(
        "sess_two",
        "prediction",
        {"outcome": "first", "failure_mode": "fm1"},
    )
    log_event(
        "sess_two",
        "prediction",
        {"outcome": "second", "failure_mode": "fm2"},
    )
    log_event(
        "sess_two",
        "tool_call",
        {"tool_name": "Edit", "input_snippet": "path=x.py"},
    )
    pairs = surprise.collect_pairs(sessions_root=Path(tmp_path))
    assert len(pairs) == 2
    # First prediction is orphaned, second is paired with the tool_call.
    assert pairs[0]["outcome"] == "first"
    assert pairs[0]["tool_name"] is None
    assert pairs[1]["outcome"] == "second"
    assert pairs[1]["tool_name"] == "Edit"


def test_render_surprise_table_empty_message(tmp_path):
    out = surprise.render_surprise_table([], days=7)
    assert "no <cortex_predict> blocks captured yet" in out
    assert "last 7 days" in out


def test_render_surprise_table_shows_recent_pairs():
    pairs = [
        {
            "session_id": "abcdef123456",
            "at": "2026-04-14T10:00:00+00:00",
            "outcome": "tests pass",
            "failure_mode": "lookahead leak",
            "tool_name": "Bash",
            "tool_snippet": "pytest -q",
            "tool_response": "OK",
            "tripwire_ids": ["lookahead_parquet"],
        }
    ]
    out = surprise.render_surprise_table(pairs)
    assert "tests pass" in out
    assert "lookahead leak" in out
    assert "Bash" in out
    assert "Predictions total:         1" in out
    assert "paired with tool_call:   1" in out
