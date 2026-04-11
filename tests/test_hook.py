"""Tests for the cortex-hook entry point.

These tests invoke `cortex.hook.main()` with mocked stdin/stdout so they
exercise the full hook path without spawning a subprocess.
"""
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from cortex import hook
from cortex.importers.memory_md import run_migration


def _run_hook(stdin_text: str) -> tuple[int, str]:
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
        ret = hook.main()
    return ret, stdout.getvalue()


def test_hook_empty_stdin_is_noop():
    ret, out = _run_hook("")
    assert ret == 0
    assert out == ""


def test_hook_invalid_json_fails_open():
    ret, out = _run_hook("not json at all")
    assert ret == 0
    assert out == ""


def test_hook_missing_prompt_is_noop():
    ret, out = _run_hook(json.dumps({"session_id": "abc"}))
    assert ret == 0
    assert out == ""


def test_hook_no_match_emits_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        monkeypatch.setenv("CORTEX_DB", db)
        ret, out = _run_hook(json.dumps({"prompt": "hello world nothing special"}))
        assert ret == 0
        assert out == ""


def test_hook_match_emits_additional_context(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        monkeypatch.setenv("CORTEX_DB", db)
        prompt = "run replay_basis_arb.py to backtest binance lead on 5m poly slots"
        ret, out = _run_hook(json.dumps({"prompt": prompt}))
        assert ret == 0
        assert out, "hook should emit JSON for a matching prompt"
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "<cortex_brief" in ctx
        assert "poly_fee_empirical" in ctx


# ---------- Day 10: verifier blocking mode ----------


def test_hook_does_not_block_when_verify_block_unset(monkeypatch, tmp_path):
    """CORTEX_VERIFY_BLOCK unset -> normal flow even with verifier failures."""
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)

    prompt = "run a poly backtest on 5m slot data"
    ret, out = _run_hook(json.dumps({"session_id": "t1", "prompt": prompt}))
    assert ret == 0  # never blocks when env vars are unset


def test_hook_does_not_block_when_verify_enable_set_but_block_unset(monkeypatch, tmp_path):
    """CORTEX_VERIFY_ENABLE=1 runs verifiers but never blocks without BLOCK."""
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_VERIFY_ENABLE", "1")
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)

    prompt = "run a poly backtest on 5m slot data"
    ret, _out = _run_hook(json.dumps({"session_id": "t2", "prompt": prompt}))
    assert ret == 0  # verifier ran but we never block without BLOCK=1


def test_hook_blocks_on_verifier_fail_when_block_enabled(monkeypatch, tmp_path):
    """Simulate a verifier failure by patching run_verifiers_for."""
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_VERIFY_ENABLE", "1")
    monkeypatch.setenv("CORTEX_VERIFY_BLOCK", "1")

    # Patch run_verifiers_for so it reports a failure for any matched critical
    import cortex.verify_runner as _vr

    def _fake_run(tripwires):
        # Return one failed result for the first critical tripwire
        for tw in tripwires:
            if tw.get("severity") == "critical":
                return [{
                    "tripwire_id": tw["id"],
                    "cmd": "cortex-check-fake",
                    "passed": False,
                    "returncode": 1,
                    "stdout": "simulated failure",
                    "stderr": "",
                }]
        return []

    monkeypatch.setattr(_vr, "run_verifiers_for", _fake_run)

    prompt = "run a poly backtest on 5m slot data"
    ret, out = _run_hook(json.dumps({"session_id": "t3", "prompt": prompt}))
    assert ret == 2  # blocked
    # Brief still emitted so the user sees why
    assert out
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "<cortex_brief" in ctx


def test_hook_does_not_block_when_verifier_passes(monkeypatch, tmp_path):
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CORTEX_VERIFY_ENABLE", "1")
    monkeypatch.setenv("CORTEX_VERIFY_BLOCK", "1")

    import cortex.verify_runner as _vr

    def _fake_pass(tripwires):
        return [{
            "tripwire_id": tripwires[0]["id"] if tripwires else "x",
            "cmd": "cortex-check-fake",
            "passed": True,
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        }] if tripwires else []

    monkeypatch.setattr(_vr, "run_verifiers_for", _fake_pass)

    prompt = "run a poly backtest on 5m slot data"
    ret, _out = _run_hook(json.dumps({"session_id": "t4", "prompt": prompt}))
    assert ret == 0  # passed -> no block
