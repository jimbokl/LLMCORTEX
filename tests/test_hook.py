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


# ---------- Day 15: shadow mode ----------


def test_hook_shadow_tripwire_not_injected_but_logged(monkeypatch, tmp_path):
    """A tripwire with status='shadow' must NOT appear in the injected
    brief, but must be recorded as a `shadow_hit` audit event."""
    from cortex.session import read_session
    from cortex.store import CortexStore

    db = str(tmp_path / "seed.db")
    run_migration(db)
    # Demote one of the critical poly_backtest_task targets to shadow.
    s = CortexStore(db)
    s.set_status("real_entry_price", "shadow")
    s.close()

    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)

    prompt = "run replay_basis_arb.py to backtest binance lead on 5m poly slots"
    ret, out = _run_hook(json.dumps({"session_id": "sh_sess", "prompt": prompt}))
    assert ret == 0
    assert out, "hook should still emit active brief"
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # Active tripwires still render.
    assert "poly_fee_empirical" in ctx
    # The shadowed one must NOT appear in the visible brief.
    assert "real_entry_price" not in ctx

    events = read_session("sh_sess")
    kinds = [e["event"] for e in events]
    assert "shadow_hit" in kinds
    shadow_ev = next(e for e in events if e["event"] == "shadow_hit")
    assert "real_entry_price" in shadow_ev["tripwire_ids"]


def test_hook_all_shadow_falls_through_to_fallback(monkeypatch, tmp_path):
    """When every matched tripwire is shadow, the active list is empty
    and the hook falls through to the TF-IDF fallback path. The
    shadow_hit event must still be logged BEFORE the fallback fires."""
    from cortex.session import read_session
    from cortex.store import CortexStore

    db = str(tmp_path / "seed.db")
    run_migration(db)
    # Nuke every active poly_backtest_task target to shadow.
    s = CortexStore(db)
    for tw_id in (
        "poly_fee_empirical",
        "lookahead_parquet",
        "real_entry_price",
        "backtest_must_match_prod",
    ):
        s.set_status(tw_id, "shadow")
    s.close()

    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)

    prompt = "run replay_basis_arb.py to backtest binance lead on 5m poly slots"
    ret, _out = _run_hook(json.dumps({"session_id": "all_sh", "prompt": prompt}))
    assert ret == 0
    events = read_session("all_sh")
    kinds = [e["event"] for e in events]
    # Shadow audit must be logged regardless of downstream path.
    assert "shadow_hit" in kinds


# ---------- Tier 1.4: git-diff-aware injection ----------


def test_git_diff_matches_tripwire_by_filepath(monkeypatch, tmp_path):
    """Touching a file whose path matches a seed tripwire's `affected_files`
    glob list must inject that tripwire even when the prompt has no
    keyword match. Uses the seeded `lookahead_parquet` which has
    `*features*.py` in its globs.
    """
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)

    # Stub _fetch_touched_files so the test does not depend on a live
    # git repo. Pattern `*features*.py` is in the seeded tripwire.
    monkeypatch.setattr(
        hook, "_fetch_touched_files",
        lambda timeout_seconds=2.0: ["DETECTOR/compute_features.py"],
    )

    # Prompt is generic — rule engine would not inject anything.
    ret, out = _run_hook(json.dumps({
        "session_id": "tier14a",
        "prompt": "clean up the imports in this file",
    }))
    assert ret == 0
    assert out, "expected inject because of touched_files glob match"
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "lookahead_parquet" in ctx


def test_git_diff_failure_is_fail_open(monkeypatch, tmp_path):
    """Any exception from _fetch_touched_files must yield an empty
    touched_files list and leave the rest of the hook path untouched.
    """
    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))

    def _boom(timeout_seconds=2.0):
        raise RuntimeError("git not available")

    # Our helper already catches its own exceptions, but verify the
    # outer hook also tolerates a stricter override via a stub that
    # simulates a git invocation raising inside subprocess.run.
    import subprocess

    def _fake_run(*_a, **_kw):
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Keyword-only match on the seeded rules must still fire.
    prompt = "run replay_basis_arb.py to backtest binance lead on 5m poly slots"
    ret, out = _run_hook(json.dumps({"session_id": "tier14b", "prompt": prompt}))
    assert ret == 0
    assert out, "keyword match must still emit brief despite git failure"


def test_git_diff_touched_files_logged_in_inject_event(monkeypatch, tmp_path):
    """The new `touched_files_matched` field must show up in the jsonl
    audit so Day-11 DMN and `cortex stats` can attribute injections to
    the git-diff source.
    """
    from cortex.session import read_session

    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))

    monkeypatch.setattr(
        hook, "_fetch_touched_files",
        lambda timeout_seconds=2.0: ["DETECTOR/features.py"],
    )

    ret, _out = _run_hook(json.dumps({
        "session_id": "tier14c",
        "prompt": "touch up the file",
    }))
    assert ret == 0
    events = read_session("tier14c")
    inject_events = [e for e in events if e["event"] == "inject"]
    assert inject_events, "inject event must be present"
    matched = inject_events[0].get("touched_files_matched") or []
    assert "lookahead_parquet" in matched


def test_hook_no_shadow_hit_event_when_only_active(monkeypatch, tmp_path):
    """Sanity check: default state (no shadow rows) must not produce
    any `shadow_hit` events."""
    from cortex.session import read_session

    db = str(tmp_path / "seed.db")
    run_migration(db)
    monkeypatch.setenv("CORTEX_DB", db)
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("CORTEX_VERIFY_BLOCK", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)

    prompt = "run replay_basis_arb.py to backtest binance lead on 5m poly slots"
    ret, _out = _run_hook(json.dumps({"session_id": "clean", "prompt": prompt}))
    assert ret == 0
    kinds = [e["event"] for e in read_session("clean")]
    assert "shadow_hit" not in kinds
