"""Rule-based classifier: matches a task prompt against YAML rule files and
returns matched tripwires plus a rendered brief ready for hook injection.

Matching is intentionally simple: tokenize the prompt into lowercase words,
then check each rule's `match_any` and `and_any` word sets against the token
set. A rule fires when at least one word from each set intersects the tokens.

No LLM calls, no embeddings, no network. The goal is <20ms latency at hook
time so that cortex never becomes a reason to skip loading context.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

import yaml

from cortex.store import CortexStore
from cortex.tokenize import tokenize as _tokenize_shared

_RULES_DIR = Path(__file__).parent / "rules"
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

DEFAULT_MAX_TRIPWIRES = 5

# Hard upper bound on the rendered brief size, in characters. Anthropic
# tokens are ~4 chars each, so the default 6000 caps the brief at ~1500
# tokens — above the max of 1544 observed in 13 days of production audit,
# so the default is a no-op for current workloads and only trips on
# unusually wide multi-tripwire matches.
#
# Overridable via CORTEX_BRIEF_MAX_CHARS. Low severities drop first;
# critical tripwires are never truncated. See _clamp_brief_to_budget.
DEFAULT_BRIEF_MAX_CHARS = 6000


def _tokenize(text: str) -> set[str]:
    """Lowercase-tokenize a prompt into a set of word-like tokens.

    Thin wrapper around `cortex.tokenize.tokenize` so the classifier's
    public surface stays stable. Behavior is controlled by the
    `CORTEX_UNICODE_TOKENS` env var — off-by-default keeps every Day-2
    guard test passing unchanged.
    """
    return _tokenize_shared(text)


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


def _match_affected_files(patterns: list[str], touched: list[str]) -> bool:
    """Return True if any touched path matches any fnmatch glob in patterns.

    Matching is case-insensitive on the path segment level and operates
    on both the full relative path and just the basename, so patterns
    like `"*classify*"` and `"cortex/*.py"` both behave intuitively.
    """
    if not patterns or not touched:
        return False
    norm_touched = [t.replace("\\", "/").lower() for t in touched]
    for pat in patterns:
        p = pat.lower()
        for path in norm_touched:
            basename = path.rsplit("/", 1)[-1]
            if fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(basename, p):
                return True
    return False


def classify_prompt(
    prompt: str,
    *,
    db_path: str | None = None,
    rules_dir: Path | None = None,
    max_tripwires: int = DEFAULT_MAX_TRIPWIRES,
    touched_files: list[str] | None = None,
) -> dict[str, Any]:
    """Classify a prompt and return matched rules + tripwires.

    When `touched_files` is non-empty (typically the output of
    `git diff --name-only HEAD` fetched by the hook), a second pass
    matches every tripwire's `affected_files` glob list against those
    paths and injects the matches into the same result set. File-path
    matches are additive — they never suppress keyword matches.

    Returns a dict with keys:
      - matched_rules:        list[str]   — ids of rules that fired
      - tripwires:            list[dict]  — ACTIVE tripwires, sorted by
                                            severity then cost, capped at
                                            max. These are the ones that
                                            go into `<cortex_brief>`.
      - shadow_tripwires:     list[dict]  — matched rows with
                                            status='shadow'. NEVER
                                            rendered into the brief;
                                            audit only. Day 15.
      - synthesis:            list[dict]  — synthesis rules fired over
                                            the active set only
      - truncated:            bool        — True if active tripwires
                                            was capped
      - total_matches:        int         — total active matches
                                            before cap
      - touched_files_matched:list[str]   — ids added by the file-path
                                            pass (empty when no git diff
                                            was supplied or nothing
                                            matched)
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
    shadow_tripwires: list[dict] = []
    synthesis: list[dict] = []
    file_matched_ids: list[str] = []

    # The file-path pass is additive: open the store even if the keyword
    # engine produced zero matches, so a tripwire with only an
    # `affected_files` rule can still fire when its file is touched.
    needs_store = bool(tripwire_ids) or bool(touched_files)

    if needs_store:
        store = CortexStore(db_path or find_db())
        try:
            # First pass — keyword-matched ids.
            for tw_id in tripwire_ids:
                tw = store.get_tripwire(tw_id)
                if not tw:
                    continue
                status = tw.get("status", "active") or "active"
                if status == "archived":
                    continue
                if status == "shadow":
                    shadow_tripwires.append(tw)
                    continue
                tripwires.append(tw)

            # Second pass — affected_files glob match against the git
            # diff. De-duplicates against the active + shadow sets so a
            # tripwire that already matched on keywords is not injected
            # twice.
            if touched_files:
                already_ids = {t["id"] for t in tripwires} | {
                    t["id"] for t in shadow_tripwires
                }
                for tw in store.list_tripwires(status=None):
                    if tw["id"] in already_ids:
                        continue
                    patterns = tw.get("affected_files") or []
                    if not patterns:
                        continue
                    if not _match_affected_files(patterns, touched_files):
                        continue
                    status = tw.get("status", "active") or "active"
                    if status == "archived":
                        continue
                    if status == "shadow":
                        shadow_tripwires.append(tw)
                    else:
                        tripwires.append(tw)
                    file_matched_ids.append(tw["id"])

            # Synthesis runs over the ACTIVE set only. Shadow synthesis
            # is a Day 16+ concern once the promoter loop exists.
            from cortex.synthesize import synthesize as _run_synthesize
            synthesis = _run_synthesize({t["id"] for t in tripwires}, store)
        finally:
            store.close()

    tripwires.sort(
        key=lambda t: (_SEV_ORDER.get(t["severity"], 9), -t["cost_usd"])
    )
    shadow_tripwires.sort(
        key=lambda t: (_SEV_ORDER.get(t["severity"], 9), -t["cost_usd"])
    )
    total = len(tripwires)
    truncated = total > max_tripwires
    tripwires = tripwires[:max_tripwires]

    return {
        "matched_rules": [r["id"] for r in matched_rules],
        "tripwires": tripwires,
        "shadow_tripwires": shadow_tripwires,
        "synthesis": synthesis,
        "truncated": truncated,
        "total_matches": total,
        "touched_files_matched": file_matched_ids,
    }


def _brief_budget() -> int:
    """Resolve the current brief-size budget, honoring the env override.

    Non-integer or non-positive overrides fall back to the default so a
    misconfigured env var can never silence the entire brief.
    """
    raw = os.environ.get("CORTEX_BRIEF_MAX_CHARS")
    if not raw:
        return DEFAULT_BRIEF_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BRIEF_MAX_CHARS
    return value if value > 0 else DEFAULT_BRIEF_MAX_CHARS


def _clamp_tripwires_to_budget(
    tripwires: list[dict], budget: int
) -> tuple[list[dict], list[dict]]:
    """Shrink the tripwire list until its rendered size fits the budget.

    Drops lowest-severity items first and never touches critical entries.
    Returns `(kept, dropped)`. Assumes `tripwires` is already sorted by
    severity (critical first) per classify_prompt's contract.
    """
    if not tripwires:
        return [], []

    def _prospective_size(trips: list[dict]) -> int:
        # Rough per-item cost: title + body + framing. Sum of raw
        # content lengths is a cheap upper bound on the rendered size
        # and avoids re-entering render_brief recursively.
        total = 256  # framing (opener, preamble, closer, blank lines)
        for t in trips:
            total += len(t.get("title") or "") + len(t.get("body") or "") + 96
        return total

    kept = list(tripwires)
    dropped: list[dict] = []
    while kept and _prospective_size(kept) > budget:
        # Peek the tail; stop as soon as only critical tripwires remain.
        if (kept[-1].get("severity") or "").lower() == "critical":
            break
        dropped.insert(0, kept.pop())
    return kept, dropped


def render_brief(result: dict[str, Any]) -> str:
    """Render a classification result into an injection-ready text block.

    Applies the `CORTEX_BRIEF_MAX_CHARS` budget clamp: when the full brief
    would exceed the budget, drops lowest-severity tripwires first and
    never touches `critical` entries. A truncation marker is appended to
    the brief so the agent sees that some lessons were pruned.
    """
    tripwires = result.get("tripwires") or []
    if not tripwires:
        return ""

    budget = _brief_budget()
    tripwires, budget_dropped = _clamp_tripwires_to_budget(tripwires, budget)
    if not tripwires:
        # Degenerate case: even the all-critical floor was empty (nothing
        # was critical and every item was budget-dropped). Re-instate a
        # single highest-priority item so the brief is never misleadingly
        # silent on a real match.
        tripwires = (result.get("tripwires") or [])[:1]
        budget_dropped = (result.get("tripwires") or [])[1:]

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

    # Day 14: Surprise Engine. For critical tripwires, ask the agent to
    # emit a falsifiable prediction before the next tool call. cortex-watch
    # reads the transcript and logs a `prediction` event, which DMN later
    # pairs with real outcomes to find the agent's blind spots.
    if n_crit > 0:
        lines.extend(_render_predict_block())

    if budget_dropped:
        dropped_ids = ", ".join(t["id"] for t in budget_dropped)
        lines.append(
            f"<!-- cortex_brief: truncated {len(budget_dropped)} lower-severity "
            f"tripwire(s) to fit CORTEX_BRIEF_MAX_CHARS budget: {dropped_ids} -->"
        )

    lines.append(
        "To acknowledge a tripwire for this task and silence it, include"
    )
    lines.append("`--cortex-ack=<id>` in your next message.")
    lines.append("</cortex_brief>")
    return "\n".join(lines)


def _render_predict_block() -> list[str]:
    """The Day-14 'Surprise Engine' injection for critical tripwires.

    The block asks the agent to emit a falsifiable prediction in a
    machine-parseable XML format. The two-field shape (`outcome` +
    `failure_mode`) forces System-2 reasoning: a formal 'expect success'
    answer is trivial, but stating the most likely failure mode is not.
    When `failure_mode` diverges from the real outcome captured by
    PostToolUse, that is the maximum-information signal for DMN reflection.
    """
    return [
        "CRITICAL TASK DETECTED. Before executing any tools, output your",
        "expectation using this exact XML format in your reply text:",
        "",
        "<cortex_predict>",
        "  <outcome>falsifiable prediction (e.g. PnL > 0, tests pass, 0 lookahead warnings)</outcome>",
        "  <failure_mode>the most likely technical reason this might fail</failure_mode>",
        "</cortex_predict>",
        "",
        "This is a soft request: if you omit it nothing breaks, but Cortex",
        "cannot measure surprise and DMN loses a data point.",
        "",
    ]
