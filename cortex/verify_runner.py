"""Pre-flight verifier runner: execute `verify_cmd` for critical tripwires
during `cortex-hook` invocation and append results to the injected brief.

Opt-in via `CORTEX_VERIFY_ENABLE=1`. When enabled:

1. Only tripwires with `severity == "critical"` are considered.
2. Only commands matching an allow-list prefix are run (default: anything
   starting with `cortex-` or `python -m cortex`). The user can override
   with `CORTEX_VERIFY_PREFIXES="prefix1,prefix2,..."` or drop the
   allow-list entirely with the DANGER flag `CORTEX_VERIFY_ALLOW_ANY=1`.
3. Hard timeout per command via `CORTEX_VERIFY_TIMEOUT` (default 3 s).
4. `shell=False` always. Commands are parsed with `shlex.split`. No
   pipelines, no shell expansion, no injection via tripwire body.
5. Captured `stdout` truncated to 500 chars, `stderr` to 200 chars.
6. Any failure (timeout, OSError, parse error) results in a `skipped`
   marker — never raises.

Why opt-in: arbitrary shell execution from a hook path is dangerous. A
safe-by-default Cortex cannot run verifiers automatically because users
may have `verify_cmd` values that do expensive or destructive things
(e.g. `place_test.exe --market` which is a real trade). Users who want
auto-verification explicitly enable the feature AND the allow-list
protects them from accidentally running the more dangerous `verify_cmd`
values they wrote without auto-run in mind.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any

_DEFAULT_TIMEOUT_S = 3.0
_DEFAULT_PREFIXES = ("cortex-", "python -m cortex")

_MAX_STDOUT = 500
_MAX_STDERR = 200


def _enabled() -> bool:
    return os.environ.get("CORTEX_VERIFY_ENABLE") == "1"


def _allow_any() -> bool:
    return os.environ.get("CORTEX_VERIFY_ALLOW_ANY") == "1"


def _timeout() -> float:
    try:
        return float(os.environ.get("CORTEX_VERIFY_TIMEOUT") or _DEFAULT_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _prefixes() -> tuple[str, ...]:
    env = os.environ.get("CORTEX_VERIFY_PREFIXES")
    if env:
        return tuple(p.strip() for p in env.split(",") if p.strip())
    return _DEFAULT_PREFIXES


def is_allowed(cmd: str) -> bool:
    """True if `cmd` passes the allow-list guard. Used by hook and tests."""
    if _allow_any():
        return True
    cmd = (cmd or "").strip()
    if not cmd:
        return False
    return any(cmd.startswith(p) for p in _prefixes())


def run_verifier(tripwire: dict[str, Any]) -> dict[str, Any] | None:
    """Run one tripwire's `verify_cmd`.

    Returns:
      - None if the tripwire has no `verify_cmd` at all
      - A result dict otherwise, with one of:
          * {"skipped": "not allow-listed"}
          * {"skipped": "timeout"}
          * {"skipped": "error: <type>"}
          * {"passed": bool, "returncode": int, "stdout": str, "stderr": str}
    """
    cmd = (tripwire.get("verify_cmd") or "").strip()
    if not cmd:
        return None

    base = {"tripwire_id": tripwire.get("id", ""), "cmd": cmd}

    if not is_allowed(cmd):
        return {**base, "skipped": "not allow-listed"}

    try:
        args = shlex.split(cmd, posix=True)
    except ValueError as e:
        return {**base, "skipped": f"parse error: {e}"}
    if not args:
        return {**base, "skipped": "empty command"}

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_timeout(),
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {**base, "skipped": "timeout"}
    except FileNotFoundError:
        return {**base, "skipped": "command not found"}
    except Exception as e:
        return {**base, "skipped": f"error: {type(e).__name__}"}

    return {
        **base,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[:_MAX_STDOUT],
        "stderr": (proc.stderr or "")[:_MAX_STDERR],
        "passed": proc.returncode == 0,
    }


def run_verifiers_for(tripwires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run pre-flight verifiers for all critical tripwires in the list.

    No-op (returns empty list) unless `CORTEX_VERIFY_ENABLE=1` is set.
    Only tripwires with severity == "critical" are considered, to avoid
    running expensive or noisy verifiers on medium/low severity lessons.
    """
    if not _enabled():
        return []
    results: list[dict[str, Any]] = []
    for tw in tripwires:
        if tw.get("severity") != "critical":
            continue
        r = run_verifier(tw)
        if r is not None:
            results.append(r)
    return results


def render_verifier_block(results: list[dict[str, Any]]) -> list[str]:
    """Render verifier results into a list of lines suitable for appending
    to a `<cortex_brief>` block. Returns empty list when no results.
    """
    if not results:
        return []

    lines: list[str] = []
    lines.append("VERIFIER RESULTS (pre-flight code check):")
    any_fail = False
    for v in results:
        tw_id = v.get("tripwire_id", "")
        if "skipped" in v:
            lines.append(f"  [SKIP] {tw_id}  ({v['skipped']})")
            continue
        passed = v.get("passed", False)
        status = "OK  " if passed else "FAIL"
        if not passed:
            any_fail = True
        lines.append(f"  [{status}] {tw_id}")
        lines.append(f"    cmd: {v.get('cmd', '')}")
        stdout = (v.get("stdout") or "").strip()
        if stdout:
            head = stdout.splitlines()[:5]
            for line in head:
                lines.append(f"    {line}")
        if not passed:
            lines.append(
                "    >> VERIFIER FAILED: the bug this tripwire warns about is "
                "PRESENT in your current code. Fix before proceeding."
            )
    if any_fail:
        lines.append("")
        lines.append(
            "  One or more pre-flight checks failed. Do not proceed with the "
            "task until the flagged code is corrected."
        )
    lines.append("")
    return lines
