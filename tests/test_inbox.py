"""Tests for the Day 8 inbox workflow."""
from __future__ import annotations

from pathlib import Path

from cortex.inbox import (
    delete_draft,
    draft_to_tripwire_kwargs,
    inbox_dir,
    list_drafts,
    read_draft,
    validate_draft,
    write_draft,
)

# ---------- inbox_dir resolution ----------


def test_inbox_dir_honors_env_var(tmp_path, monkeypatch):
    target = tmp_path / "custom_inbox"
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(target))
    assert inbox_dir() == target
    assert target.exists()


def test_inbox_dir_walks_up_from_cwd(tmp_path, monkeypatch):
    # Create a fake project tree: tmp_path/.cortex/, plus sub/sub2/
    (tmp_path / ".cortex").mkdir()
    sub = tmp_path / "sub" / "sub2"
    sub.mkdir(parents=True)
    monkeypatch.delenv("CORTEX_INBOX_DIR", raising=False)
    monkeypatch.chdir(sub)
    found = inbox_dir()
    assert found == tmp_path / ".cortex" / "inbox"
    assert found.exists()


# ---------- write / read / list / delete ----------


def test_write_draft_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    draft = {
        "id": "test_rule",
        "title": "Test",
        "severity": "high",
        "domain": "test",
        "triggers": ["a", "b"],
        "body": "Test body",
    }
    draft_id = write_draft(draft, source="manual")
    assert draft_id is not None
    assert draft_id.startswith("manual_")
    path = Path(tmp_path) / f"{draft_id}.json"
    assert path.exists()


def test_write_draft_custom_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    draft_id = write_draft({}, draft_id="my_custom_id")
    assert draft_id == "my_custom_id"
    assert (tmp_path / "my_custom_id.json").exists()


def test_write_draft_sanitizes_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    # Path traversal / weird chars
    draft_id = write_draft({}, draft_id="../evil/../path")
    assert draft_id is not None
    assert "/" not in draft_id
    assert "\\" not in draft_id
    assert not (tmp_path.parent / "evil").exists()


def test_read_draft_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    assert read_draft("nonexistent") is None


def test_read_draft_returns_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    draft = {"id": "x", "title": "X"}
    draft_id = write_draft(draft, draft_id="test_draft")
    loaded = read_draft(draft_id)
    assert loaded is not None
    assert loaded["draft"]["id"] == "x"
    assert loaded["source"] == "manual"
    assert "created_at" in loaded


def test_list_drafts_returns_all(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    write_draft({"n": 1}, draft_id="a")
    write_draft({"n": 2}, draft_id="b")
    write_draft({"n": 3}, draft_id="c")
    drafts = list_drafts()
    assert len(drafts) == 3
    ids = {d["draft_id"] for d in drafts}
    assert ids == {"a", "b", "c"}


def test_list_drafts_skips_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    write_draft({"n": 1}, draft_id="good")
    (tmp_path / "corrupt.json").write_text("not json at all", encoding="utf-8")
    drafts = list_drafts()
    assert len(drafts) == 1
    assert drafts[0]["draft_id"] == "good"


def test_list_drafts_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    assert list_drafts() == []


def test_delete_draft_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    draft_id = write_draft({}, draft_id="to_delete")
    assert delete_draft(draft_id) is True
    assert not (tmp_path / "to_delete.json").exists()
    assert delete_draft(draft_id) is False  # already gone


def test_delete_draft_missing_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path))
    assert delete_draft("nonexistent") is False


# ---------- validate_draft ----------


def test_validate_draft_all_fields_ok():
    draft = {
        "id": "my_rule",
        "title": "A title",
        "severity": "critical",
        "domain": "test",
        "triggers": ["foo", "bar"],
        "body": "A body with no placeholders",
    }
    missing, todos = validate_draft(draft)
    assert missing == []
    assert todos == []


def test_validate_draft_missing_required():
    draft = {"id": "x", "title": "X"}
    missing, _ = validate_draft(draft)
    assert "severity" in missing
    assert "domain" in missing
    assert "triggers" in missing
    assert "body" in missing


def test_validate_draft_todo_placeholders():
    draft = {
        "id": "TODO_set_id",
        "title": "TODO one-line summary",
        "severity": "medium",
        "domain": "polymarket",
        "triggers": ["TODO", "extract"],
        "body": "TODO rule statement",
    }
    missing, todos = validate_draft(draft)
    assert missing == []
    assert set(todos) >= {"id", "title", "triggers", "body"}


def test_validate_draft_bad_severity():
    draft = {
        "id": "x",
        "title": "X",
        "severity": "invalid",
        "domain": "d",
        "triggers": ["a"],
        "body": "b",
    }
    missing, _ = validate_draft(draft)
    assert "severity" in missing


def test_validate_draft_bad_id_format():
    draft = {
        "id": "has spaces and !@#",
        "title": "X",
        "severity": "high",
        "domain": "d",
        "triggers": ["a"],
        "body": "b",
    }
    _, todos = validate_draft(draft)
    assert "id" in todos


def test_validate_draft_empty_triggers():
    draft = {
        "id": "x",
        "title": "X",
        "severity": "high",
        "domain": "d",
        "triggers": [],
        "body": "b",
    }
    missing, _ = validate_draft(draft)
    assert "triggers" in missing


# ---------- draft_to_tripwire_kwargs ----------


def test_draft_to_tripwire_kwargs_filters_unknown():
    draft = {
        "id": "x",
        "title": "X",
        "severity": "high",
        "domain": "d",
        "triggers": ["a"],
        "body": "b",
        "unknown_field": "should be dropped",
        "palace_hit_score": 0.82,
        "verify_cmd": "cortex-check-x",
        "cost_usd": 42.0,
    }
    kwargs = draft_to_tripwire_kwargs(draft)
    assert "unknown_field" not in kwargs
    assert "palace_hit_score" not in kwargs
    assert kwargs["id"] == "x"
    assert kwargs["verify_cmd"] == "cortex-check-x"
    assert kwargs["cost_usd"] == 42.0


def test_draft_to_tripwire_kwargs_passes_status():
    """Day 15: status field is now a recognized draft key."""
    draft = {
        "id": "x", "title": "X", "severity": "high", "domain": "d",
        "triggers": ["a"], "body": "b", "status": "shadow",
    }
    kwargs = draft_to_tripwire_kwargs(draft)
    assert kwargs["status"] == "shadow"
