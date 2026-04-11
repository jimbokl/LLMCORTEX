"""Day 16 -- DMN promoter CLI end-to-end tests.

These tests exercise the `cortex promote classify/run/log` subparsers
against a real CortexStore instance and a fixture sessions directory,
with Haiku calls either skipped (dry-run) or replaced by a monkeypatch
of `promoter.classify_pair` so no network request ever happens.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from cortex import promoter
from cortex.cli import main as cli_main
from cortex.session import log_event, read_session
from cortex.store import CortexStore


# ---- fixtures ----


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Isolated `.cortex/` tree with a real store + sessions dir."""
    cortex_dir = tmp_path / ".cortex"
    cortex_dir.mkdir()
    sessions_dir = cortex_dir / "sessions"
    sessions_dir.mkdir()
    db_path = cortex_dir / "store.db"

    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(sessions_dir))

    store = CortexStore(db_path)
    try:
        yield {
            "store": store,
            "db_path": db_path,
            "sessions_dir": sessions_dir,
            "tmp_path": tmp_path,
        }
    finally:
        store.close()


def _seed_prediction_pair(
    sessions_dir: Path,
    session_id: str,
    at: str,
    failure_mode: str,
    tool_response: str,
):
    """Write a minimal session jsonl containing an inject, a prediction,
    and a tool_call so `collect_pairs` yields exactly one pair."""
    path = sessions_dir / f"{session_id}.jsonl"
    rows = [
        {
            "at": at,
            "event": "inject",
            "matched_rules": ["poly_backtest_task"],
            "tripwire_ids": ["tw1"],
        },
        {
            "at": at,
            "event": "prediction",
            "outcome": "backtest passes",
            "failure_mode": failure_mode,
        },
        {
            "at": at,
            "event": "tool_call",
            "tool_name": "Bash",
            "input_snippet": "pytest -q",
            "response_snippet": tool_response,
        },
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---- classify CLI ----


def test_classify_dry_run_no_mutations(live_env, capsys, monkeypatch):
    # Seed a pair so collect_pairs has something to find.
    _seed_prediction_pair(
        live_env["sessions_dir"],
        "sess_dry",
        at="2026-04-11T12:00:00+00:00",
        failure_mode="slot_ts lookahead bug",
        tool_response="332 passed",
    )
    # Make sure no network is ever reached even if a bug routes through it.
    monkeypatch.setattr(
        promoter,
        "classify_pair",
        lambda *a, **k: pytest.fail("classify_pair must NOT be called in dry-run"),
    )
    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "classify", "--dry-run", "--days", "30",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Will classify 1 pair" in out
    assert "[dry run]" in out
    # No rows should have been written.
    assert live_env["store"].list_pair_classifications() == []


def test_classify_apply_calls_classifier_and_persists(
    live_env, capsys, monkeypatch
):
    _seed_prediction_pair(
        live_env["sessions_dir"],
        "sess_apply",
        at="2026-04-11T12:00:00+00:00",
        failure_mode="slot_ts lookahead bug",
        tool_response="332 passed",
    )

    def fake_classify(pair, **kwargs):
        assert pair["session_id"] == "sess_apply"
        return {
            "label": "mismatch",
            "confidence": 0.82,
            "reasoning": "tool response differs from prediction",
            "model": "claude-haiku-4-5",
        }

    monkeypatch.setattr(promoter, "classify_pair", fake_classify)

    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "classify", "--days", "30", "--yes",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Classified 1" in out

    rows = live_env["store"].list_pair_classifications()
    assert len(rows) == 1
    assert rows[0]["label"] == "mismatch"
    assert rows[0]["confidence"] == 0.82


def test_classify_is_idempotent(live_env, capsys, monkeypatch):
    """Two consecutive classify runs must write only one row per pair."""
    _seed_prediction_pair(
        live_env["sessions_dir"],
        "sess_idempotent",
        at="2026-04-11T12:00:00+00:00",
        failure_mode="anything",
        tool_response="ok",
    )

    call_count = {"n": 0}

    def fake_classify(pair, **kwargs):
        call_count["n"] += 1
        return {
            "label": "match",
            "confidence": 0.9,
            "reasoning": "ok",
            "model": "claude-haiku-4-5",
        }

    monkeypatch.setattr(promoter, "classify_pair", fake_classify)

    # First run: classify 1
    cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "classify", "--days", "30", "--yes",
    ])
    # Second run: nothing to do
    cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "classify", "--days", "30", "--yes",
    ])
    out = capsys.readouterr().out
    assert "already classified" in out
    # classifier should have been invoked exactly once.
    assert call_count["n"] == 1
    assert len(live_env["store"].list_pair_classifications()) == 1


def test_classify_no_pairs_short_circuits(live_env, capsys, monkeypatch):
    monkeypatch.setattr(
        promoter,
        "classify_pair",
        lambda *a, **k: pytest.fail("no pairs: classifier must not run"),
    )
    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "classify", "--days", "1",
    ])
    assert rc == 0
    assert "No surprise pairs" in capsys.readouterr().out


# ---- run CLI ----


def test_run_dry_run_does_not_mutate(live_env, capsys, monkeypatch):
    """A shadow tripwire that doesn't meet gates produces no decisions
    and no mutation."""
    live_env["store"].add_tripwire(
        id="shadow_cold",
        title="cold shadow",
        severity="medium",
        domain="test",
        triggers=["cold"],
        body="cold shadow body",
        cost_usd=0.0,
        status="shadow",
    )
    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stable" in out or "DRY RUN" in out
    # No status change
    assert live_env["store"].get_tripwire("shadow_cold")["status"] == "shadow"
    assert live_env["store"].list_status_changes() == []


def test_run_apply_mutates_store_when_gate_met(
    live_env, capsys, monkeypatch
):
    """Seed a shadow tripwire + enough sessions + a stale status_changes
    row to satisfy the fallback gate, then verify --apply mutates."""
    store = live_env["store"]
    sessions_dir = live_env["sessions_dir"]

    store.add_tripwire(
        id="tw_good",
        title="good shadow",
        severity="high",
        domain="test",
        triggers=["g"],
        body="good shadow body",
        cost_usd=0.0,
        status="shadow",
    )
    # Seed a stale "moved to shadow" audit row so tenure is > 7 days.
    store.record_status_change(
        tripwire_id="tw_good",
        from_status="active",
        to_status="shadow",
        reason="seed",
        at="2026-04-01T00:00:00+00:00",
    )

    # Seed 6 distinct sessions with inject+caught events for tw_good so
    # fitness clears the fallback gate (hits=12, distinct_sessions=6,
    # fitness=12.0, caught_rate=1.0).
    for i in range(6):
        sid = f"s_good_{i}"
        path = sessions_dir / f"{sid}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for _ in range(2):
                f.write(json.dumps({
                    "at": "2026-04-11T12:00:00+00:00",
                    "event": "inject",
                    "matched_rules": ["r"],
                    "tripwire_ids": ["tw_good"],
                }) + "\n")

    # Freeze promoter._now so tenure calculations are deterministic
    from datetime import datetime, timezone
    fake_now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(promoter, "_now", lambda: fake_now)

    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "run", "--apply", "--days", "30",
        "--session-id", "promoter_test",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "APPLIED" in out
    # Status should now be active.
    assert store.get_tripwire("tw_good")["status"] == "active"
    # Audit row should exist (beyond the seed).
    changes = store.list_status_changes(tripwire_id="tw_good")
    assert len(changes) == 2  # seed + new
    assert changes[0]["to_status"] == "active"
    assert changes[0]["session_id"] == "promoter_test"
    # Session audit event.
    events = read_session("promoter_test")
    assert any(
        e.get("event") == "status_change"
        and e.get("tripwire_id") == "tw_good"
        and e.get("to") == "active"
        for e in events
    )


# ---- log CLI ----


def test_log_empty_state(live_env, capsys):
    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "log",
    ])
    assert rc == 0
    assert "no status changes" in capsys.readouterr().out


def test_log_renders_audit_rows(live_env, capsys):
    live_env["store"].add_tripwire(
        id="tw_log", title="x", severity="high", domain="d",
        triggers=["x"], body="b", cost_usd=0.0,
    )
    live_env["store"].record_status_change(
        tripwire_id="tw_log",
        from_status="active",
        to_status="shadow",
        reason="test_reason",
        metadata={"fitness": -3.5},
    )
    rc = cli_main([
        "--db", str(live_env["db_path"]),
        "promote", "log", "--days", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tw_log" in out
    assert "test_reason" in out
    assert "fit=-3.50" in out
