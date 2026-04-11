"""Rule-based classifier: matches a task prompt against YAML rule files and
returns matched tripwires plus a rendered brief ready for hook injection.

Matching is intentionally simple: tokenize the prompt into lowercase words,
then check each rule's `match_any` and `and_any` word sets against the token
set. A rule fires when at least one word from each set intersects the tokens.

No LLM calls, no embeddings, no network. The goal is <20ms latency at hook
time so that cortex never becomes a reason to skip loading context.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from cortex.store import CortexStore

_RULES_DIR = Path(__file__).parent / "rules"
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_WORD_RE = re.compile(r"[a-z0-9_\-]+")

DEFAULT_MAX_TRIPWIRES = 5


def _tokenize(text: str) -> set[str]:
    """Lowercase-tokenize a prompt into a set of word-like tokens."""
    return set(_WORD_RE.findall(text.lower()))


def _load_rules(rules_dir: Path) -> list[dict]:
    rules: list[dict] = []
    if not rules_dir.exists():
        return rules
    for path in sorted(rules_dir.glob("*.yml")):
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        for rule in doc.get("rules", []) or []:
            rule["_source_file"] = path.name
            rules.append(rule)
    return rules


def _match_rule(rule: dict, tokens: set[str]) -> bool:
    match_any = {str(w).lower() for w in rule.get("match_any") or []}
    and_any = {str(w).lower() for w in rule.get("and_any") or []}
    if not match_any and not and_any:
        return False
    if match_any and not (match_any & tokens):
        return False
    if and_any and not (and_any & tokens):
        return False
    return True


def find_db(start: Path | None = None) -> str:
    """Locate `.cortex/store.db` by walking up from `start` (or CWD).

    Checks the `CORTEX_DB` environment variable first. Used by the hook and
    CLI so that `cortex stats` works from any subdirectory of a project that
    has a `.cortex/store.db` at its root.
    """
    env = os.environ.get("CORTEX_DB")
    if env:
        return env
    start = start or Path.cwd()
    for parent in [start, *start.parents]:
        candidate = parent / ".cortex" / "store.db"
        if candidate.exists():
            return str(candidate)
    return ".cortex/store.db"


def classify_prompt(
    prompt: str,
    *,
    db_path: str | None = None,
    rules_dir: Path | None = None,
    max_tripwires: int = DEFAULT_MAX_TRIPWIRES,
) -> dict[str, Any]:
    """Classify a prompt and return matched rules + tripwires.

    Returns a dict with keys:
      - matched_rules: list[str]   — ids of rules that fired
      - tripwires:     list[dict]  — sorted by severity then cost, capped at max
      - truncated:     bool        — True if tripwires was capped
      - total_matches: int         — total tripwire hits before cap
    """
    tokens = _tokenize(prompt)
    rules = _load_rules(rules_dir or _RULES_DIR)

    matched_rules: list[dict] = []
    tripwire_ids: set[str] = set()
    for rule in rules:
        if _match_rule(rule, tokens):
            matched_rules.append(rule)
            tripwire_ids.update(rule.get("inject", []) or [])

    tripwires: list[dict] = []
    synthesis: list[dict] = []
    if tripwire_ids:
        store = CortexStore(db_path or find_db())
        try:
            for tw_id in tripwire_ids:
                tw = store.get_tripwire(tw_id)
                if tw:
                    tripwires.append(tw)
            from cortex.synthesize import synthesize as _run_synthesize
            synthesis = _run_synthesize({t["id"] for t in tripwires}, store)
        finally:
            store.close()

    tripwires.sort(
        key=lambda t: (_SEV_ORDER.get(t["severity"], 9), -t["cost_usd"])
    )
    total = len(tripwires)
    truncated = total > max_tripwires
    tripwires = tripwires[:max_tripwires]

    return {
        "matched_rules": [r["id"] for r in matched_rules],
        "tripwires": tripwires,
        "synthesis": synthesis,
        "truncated": truncated,
        "total_matches": total,
    }


def render_brief(result: dict[str, Any]) -> str:
    """Render a classification result into an injection-ready text block."""
    tripwires = result.get("tripwires") or []
    if not tripwires:
        return ""

    n = len(tripwires)
    n_crit = sum(1 for t in tripwires if t["severity"] == "critical")
    rules = ", ".join(result.get("matched_rules") or []) or "(none)"

    lines: list[str] = []
    lines.append(f'<cortex_brief n="{n}" critical="{n_crit}">')
    lines.append(f"Cortex matched rule(s): {rules}")
    lines.append("")

    synth_list = result.get("synthesis") or []
    if synth_list:
        lines.append("SYNTHESIS (cumulative cost from matched tripwires):")
        for s in synth_list:
            lines.append(
                f"  {s['id']}: Sum = {s['total']}pp "
                f"(threshold {s['threshold']}pp, op {s['op']})"
            )
            for c in s["components"]:
                prefix = "+" if c["sign"] == "drag" else "-"
                lines.append(
                    f"    {prefix}{c['value']}{c['unit']:<4} "
                    f"{c['metric']:<24} [{c['tripwire_id']}]"
                )
            lines.append(f"    >> {s['message']}")
            lines.append("")

    # Day 7: pre-flight verifier results (opt-in via CORTEX_VERIFY_ENABLE=1)
    verifier_results = result.get("verifier_results") or []
    if verifier_results:
        try:
            from cortex.verify_runner import render_verifier_block
            lines.extend(render_verifier_block(verifier_results))
        except Exception:
            pass

    lines.append(
        "The following lessons apply to this task. Each cost real money or"
    )
    lines.append(
        "research time in the past. Read them before committing to an approach:"
    )
    lines.append("")

    for i, tw in enumerate(tripwires, 1):
        sev = tw["severity"].upper()
        cost_str = f" (past cost ${tw['cost_usd']:.2f})" if tw["cost_usd"] > 0 else ""
        lines.append(f"[{i}] {tw['id']}  --  {sev}{cost_str}")
        lines.append(f"    {tw['title']}")
        lines.append("")
        for body_line in tw["body"].split("\n"):
            lines.append(f"    {body_line}")
        lines.append("")

    if result.get("truncated"):
        extra = result["total_matches"] - n
        lines.append(
            f"... {extra} more tripwire(s) also matched. Run `cortex find ...` to inspect."
        )
        lines.append("")

    lines.append(
        "To acknowledge a tripwire for this task and silence it, include"
    )
    lines.append("`--cortex-ack=<id>` in your next message.")
    lines.append("</cortex_brief>")
    return "\n".join(lines)
