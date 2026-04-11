"""Tests for the Day 11 Haiku DMN reflection loop.

All API calls are mocked via dependency injection on `call_haiku`'s
`client` parameter. No real Anthropic API calls happen in the test
suite -- tests are fast, deterministic, and free.
"""
from __future__ import annotations

import json

from cortex.dmn import (
    build_existing_tripwires_summary,
    build_prompt,
    build_session_summary,
    call_haiku,
    estimate_prompt_tokens,
    parse_proposals,
    render_reflection_report,
    run_reflection,
    write_proposals_to_inbox,
)
from cortex.importers.memory_md import run_migration
from cortex.session import log_event

# ---- build_session_summary ----


def test_build_session_summary_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = str(tmp_path / "seed.db")
    run_migration(db)
    s = build_session_summary(days=7, db_path=db)
    assert s["n_sessions"] == 0
    assert s["n_events"] == 0
    assert s["sessions_with_inject"] == 0
    assert len(s["cold_tripwires"]) == 11  # all seeded tripwires are cold


def test_build_session_summary_with_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    db = str(tmp_path / "seed.db")
    run_migration(db)

    log_event("s1", "inject", {
        "matched_rules": ["poly_backtest_task"],
        "tripwire_ids": ["poly_fee_empirical", "real_entry_price"],
        "synthesis_ids": [],
    })
    log_event("s1", "tool_call", {"tool_name": "Bash", "input_snippet": "echo"})
    log_event("s1", "tool_call", {"tool_name": "Edit", "input_snippet": "x"})

    s = build_session_summary(days=7, db_path=db)
    assert s["n_sessions"] == 1
    assert s["sessions_with_inject"] == 1
    assert ("poly_fee_empirical", 1) in s["top_tripwires_hit"]
    # Cold = 11 seeded minus the 2 that were hit = 9
    assert len(s["cold_tripwires"]) == 9


# ---- build_existing_tripwires_summary ----


def test_build_existing_tripwires_summary(tmp_path):
    db = str(tmp_path / "seed.db")
    run_migration(db)
    existing = build_existing_tripwires_summary(db_path=db)
    assert len(existing) == 11
    assert all("id" in tw and "title" in tw for tw in existing)
    ids = {tw["id"] for tw in existing}
    assert "poly_fee_empirical" in ids


# ---- build_prompt ----


def test_build_prompt_contains_required_sections():
    summary = {
        "window_days": 7,
        "n_sessions": 14,
        "n_events": 344,
        "sessions_with_inject": 5,
        "sessions_with_fallback": 9,
        "sessions_silent": 0,
        "n_silent_violations": 1,
        "top_tripwires_hit": [("poly_fee_empirical", 14)],
        "top_rules_hit": [("poly_backtest_task", 3)],
        "top_tools": [("Bash", 169)],
        "cold_tripwires": ["book_holography_failed"],
    }
    existing = [
        {"id": "poly_fee_empirical", "title": "...", "severity": "critical", "domain": "polymarket"},
    ]
    prompt = build_prompt(summary, existing, max_proposals=3)

    assert "Existing tripwires" in prompt
    assert "poly_fee_empirical" in prompt
    assert "Recent session activity" in prompt
    assert "last 7 days" in prompt
    assert "Bash" in prompt
    assert "book_holography_failed" in prompt
    assert "up to 3 NEW tripwires" in prompt
    assert "JSON array" in prompt
    assert '"id"' in prompt  # schema shown


# ---- parse_proposals ----


def test_parse_proposals_clean_json():
    text = """[
  {"id": "x", "title": "Test", "severity": "high", "domain": "test",
   "triggers": ["a"], "body": "..."}
]"""
    result = parse_proposals(text)
    assert len(result) == 1
    assert result[0]["id"] == "x"


def test_parse_proposals_with_code_fence():
    text = """```json
[{"id": "x", "title": "T", "severity": "high", "domain": "d", "triggers": ["a"], "body": "b"}]
```"""
    result = parse_proposals(text)
    assert len(result) == 1
    assert result[0]["id"] == "x"


def test_parse_proposals_with_surrounding_prose():
    text = """Here are my proposals based on the session data:

[
  {"id": "x", "title": "T", "severity": "high", "domain": "d", "triggers": ["a"], "body": "b"}
]

I hope these are helpful!"""
    result = parse_proposals(text)
    assert len(result) == 1


def test_parse_proposals_empty_array():
    result = parse_proposals("[]")
    assert result == []


def test_parse_proposals_malformed_returns_empty():
    assert parse_proposals("not json") == []
    assert parse_proposals("{incomplete") == []
    assert parse_proposals("") == []


def test_parse_proposals_skips_non_dict_elements():
    text = '[{"id":"x"}, "not a dict", 42, null]'
    result = parse_proposals(text)
    assert len(result) == 1
    assert result[0]["id"] == "x"


# ---- estimate_prompt_tokens ----


def test_estimate_prompt_tokens():
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("x" * 400) == 100  # ~4 chars per token
    assert estimate_prompt_tokens("abc") == 0


# ---- call_haiku (with mock client) ----


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeClient:
    def __init__(self, response_text: str):
        self._response = response_text
        self.calls: list[dict] = []
        self.messages = self

    def create(self, *, model: str, max_tokens: int, messages: list) -> _FakeMessage:
        self.calls.append({
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        })
        return _FakeMessage(self._response)


def test_call_haiku_with_injected_client():
    client = _FakeClient("[]")
    result = call_haiku("test prompt", client=client)
    assert result == "[]"
    assert len(client.calls) == 1
    assert client.calls[0]["model"]  # default model passed through


def test_call_haiku_joins_multiple_content_blocks():
    class Multi(_FakeClient):
        def create(self, **kw):
            msg = _FakeMessage("")
            msg.content = [_FakeBlock("hello "), _FakeBlock("world")]
            return msg
    client = Multi("ignored")
    assert call_haiku("x", client=client) == "hello world"


# ---- write_proposals_to_inbox ----


def test_write_proposals_to_inbox_strips_evidence_into_body(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    proposals = [{
        "id": "new_rule",
        "title": "Test",
        "severity": "high",
        "domain": "test",
        "triggers": ["x"],
        "body": "Rule body.",
        "evidence": "observed 5 Bash calls",
    }]
    ids = write_proposals_to_inbox(proposals)
    assert len(ids) == 1

    # Read back the draft and verify evidence is prepended to body
    from cortex.inbox import read_draft
    d = read_draft(ids[0])
    assert d is not None
    assert "evidence" not in d["draft"]  # removed from top-level
    assert "observed 5 Bash calls" in d["draft"]["body"]


# ---- run_reflection (end to end with mock) ----


def test_run_reflection_dry_run_no_api_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path / "inbox"))
    db = str(tmp_path / "seed.db")
    run_migration(db)

    result = run_reflection(days=7, db_path=db, dry_run=True)
    assert result["dry_run"] is True
    assert result["n_drafts_written"] == 0
    assert result["prompt"]
    assert result["prompt_tokens_est"] > 0
    assert result.get("raw_response") is None


def test_run_reflection_end_to_end_with_mock_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path / "inbox"))
    db = str(tmp_path / "seed.db")
    run_migration(db)

    # Log some activity so the summary isn't empty
    log_event("s1", "inject", {
        "matched_rules": ["poly_backtest_task"],
        "tripwire_ids": ["poly_fee_empirical"],
        "synthesis_ids": [],
    })

    fake_response = json.dumps([{
        "id": "proposed_from_haiku",
        "title": "Haiku-proposed tripwire",
        "severity": "high",
        "domain": "polymarket",
        "triggers": ["foo", "bar", "baz"],
        "body": "Rule. Why: evidence. How to apply: (1). (2).",
        "evidence": "observed pattern X in session s1",
    }])
    client = _FakeClient(fake_response)

    result = run_reflection(days=7, db_path=db, client=client)
    assert result["error"] is None
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["id"] == "proposed_from_haiku"
    assert result["n_drafts_written"] == 1
    assert len(result["draft_ids"]) == 1

    # Verify the draft was actually written
    from cortex.inbox import list_drafts
    drafts = list_drafts()
    assert any(d["draft"].get("id") == "proposed_from_haiku" for d in drafts)


def test_run_reflection_handles_api_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path / "inbox"))
    db = str(tmp_path / "seed.db")
    run_migration(db)

    class _Boom:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("rate limit")

    result = run_reflection(days=7, db_path=db, client=_Boom())
    assert result["error"] is not None
    assert "rate limit" in result["error"]
    assert result["n_drafts_written"] == 0


def test_run_reflection_caps_at_max_proposals(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path / "inbox"))
    db = str(tmp_path / "seed.db")
    run_migration(db)

    fake_response = json.dumps([
        {"id": f"p{i}", "title": f"T{i}", "severity": "high",
         "domain": "test", "triggers": ["a"], "body": "b"}
        for i in range(10)
    ])
    client = _FakeClient(fake_response)

    result = run_reflection(
        days=7, db_path=db, max_proposals=3, client=client,
    )
    assert len(result["proposals"]) == 3


# ---- render_reflection_report ----


def test_render_report_dry_run():
    result = {
        "days": 7,
        "model": "claude-haiku-4-5-20251001",
        "session_summary": {"n_sessions": 5, "n_events": 20},
        "existing_tripwires_count": 11,
        "prompt": "FAKE_PROMPT_CONTENT",
        "prompt_tokens_est": 500,
        "dry_run": True,
        "proposals": [],
        "draft_ids": [],
        "n_drafts_written": 0,
        "error": None,
    }
    text = render_reflection_report(result)
    assert "DRY RUN" in text
    assert "FAKE_PROMPT_CONTENT" in text


def test_render_report_with_proposals():
    result = {
        "days": 7,
        "model": "claude-haiku-4-5-20251001",
        "session_summary": {"n_sessions": 5, "n_events": 20},
        "existing_tripwires_count": 11,
        "prompt": "...",
        "prompt_tokens_est": 500,
        "dry_run": False,
        "proposals": [
            {
                "id": "proposed_x",
                "title": "A new tripwire",
                "severity": "high",
                "domain": "polymarket",
                "triggers": ["a", "b"],
            },
        ],
        "draft_ids": ["dmn_haiku_xxx"],
        "n_drafts_written": 1,
        "error": None,
    }
    text = render_reflection_report(result)
    assert "1 new tripwire" in text
    assert "proposed_x" in text
    assert "dmn_haiku_xxx" in text
    assert "cortex inbox list" in text


def test_render_report_error():
    result = {
        "days": 7,
        "model": "claude-haiku-4-5-20251001",
        "session_summary": {},
        "existing_tripwires_count": 0,
        "prompt": "",
        "prompt_tokens_est": 0,
        "dry_run": False,
        "proposals": [],
        "draft_ids": [],
        "n_drafts_written": 0,
        "error": "Haiku call failed: RuntimeError: boom",
    }
    text = render_reflection_report(result)
    assert "ERROR" in text
    assert "boom" in text
