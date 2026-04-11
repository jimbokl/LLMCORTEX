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
