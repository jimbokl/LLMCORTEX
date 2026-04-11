"""Verifier: detect lookahead patterns in feature parquet pipelines.

Scans a directory of Python files for the classic mistake of labeling a bar
with floor-of-open-time, which makes the computed values for that bar reflect
the window AFTER `slot_ts` -- i.e., future data relative to decision time.

Exit code:
    0  if no lookahead patterns found (or directory does not exist)
    1  if any lookahead patterns found

Usage:
    python -m cortex.verifiers.check_feature_lookahead --features-dir DETECTOR/
    cortex-check-lookahead --features-dir . --json

Tied to tripwire `lookahead_parquet`. See feedback_lookahead_in_features_parquet.md
for the 2026-04-10 post-mortem that inspired this check.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_FLOOR_RE = re.compile(r"//\s*\d+")
_LOOKAHEAD_SIGNATURE = "slot_ts<-(expr//N) without forward shift"


def _detect_lookahead(line: str) -> bool:
    """Return True if the line contains a lookahead `slot_ts` floor assignment.

    Flags:      slot_ts = ts // 300 * 300
                df['slot_ts'] = (df['ts'] // 300) * 300

    Does NOT flag (treated as honest forward shift):
                slot_ts = (ts // 300) * 300 + 300
                df['slot_ts'] = (df['ts'] // 300) * 300 + TICK_SIZE

    The canonical lookahead bug is labeling a bar's COMPUTED values (which
    cover [t, t+N]) with the bar's OPEN time t. The fix is to shift the
    label forward by N, so the bar is labeled with its CLOSE time -- which
    means the feature is legitimately available at decision time. We treat
    any `+ <anything>` that follows the `// N` as the honest-shift signature.
    False positives are possible if the fix uses a weird form; false
    negatives are possible if a real bug happens to add a constant for
    unrelated reasons. In both cases the operator can review the flagged
    line manually.
    """
    if "slot_ts" not in line:
        return False
    idx = line.find("slot_ts")
    eq = line.find("=", idx)
    if eq < 0:
        return False
    # Guard against `==` comparisons (not assignments)
    if eq + 1 < len(line) and line[eq + 1] == "=":
        return False
    # Strip trailing comment before analysis
    rhs = line[eq + 1 :].split("#", 1)[0]
    m = _FLOOR_RE.search(rhs)
    if not m:
        return False
    # If there's a `+` operator after the floor division, treat as honest shift
    after = rhs[m.end():]
    if "+" in after:
        return False
    return True


def scan_file(path: Path) -> list[dict]:
    """Scan one file. Returns list of findings (dicts)."""
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _detect_lookahead(line):
            findings.append({
                "file": str(path),
                "line": i,
                "code": line.rstrip(),
                "pattern": _LOOKAHEAD_SIGNATURE,
            })
    return findings


def scan_directory(root: Path) -> list[dict]:
    """Recursively scan all .py files under root. Returns list of findings."""
    findings: list[dict] = []
    if not root.exists():
        return findings
    for py_file in sorted(root.rglob("*.py")):
        findings.extend(scan_file(py_file))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortex-check-lookahead",
        description="Detect lookahead patterns in feature pipeline code.",
    )
    parser.add_argument(
        "--features-dir",
        type=Path,
        default=Path("DETECTOR"),
        help="Directory to scan (default: DETECTOR)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON diagnostic instead of human-readable text",
    )
    args = parser.parse_args(argv)

    if not args.features_dir.exists():
        if args.json:
            print(json.dumps({
                "scanned_dir": str(args.features_dir),
                "error": "directory does not exist",
                "findings": [],
            }))
        else:
            print(
                f"{args.features_dir} does not exist -- skipping verifier",
                file=sys.stderr,
            )
        return 0

    findings = scan_directory(args.features_dir)

    if args.json:
        print(json.dumps({
            "scanned_dir": str(args.features_dir),
            "findings": findings,
        }))
    else:
        if not findings:
            print(f"OK: scanned {args.features_dir}, 0 lookahead patterns found")
        else:
            print(
                f"FAIL: {len(findings)} lookahead pattern(s) detected in {args.features_dir}"
            )
            for f in findings:
                print(f"  {f['file']}:{f['line']}  {f['code']}")

    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
