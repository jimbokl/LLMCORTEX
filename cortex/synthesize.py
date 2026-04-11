"""Cost-component synthesizer: sums matched drags/boosts and fires declarative
synthesis rules when thresholds are crossed.

The synthesizer exists because individual tripwires rarely kill a strategy
on their own -- it is the CUMULATIVE structural cost that determines whether
a hypothesis is even testable. A classifier that lists 4 lessons is
informative; a synthesizer that says `Sum drag = 19.7pp > 5pp threshold` is
actionable.

Semantics: for each synthesis rule, collect the `cost_components` listed in
its `sum_over` array whose `tripwire_id` is present in the matched set. Sum
their signed values (drag > 0, boost < 0). If the result crosses the rule's
threshold under its op, the rule fires. Partial component matches still fire
as long as the remaining sum crosses the threshold -- this mirrors reality,
where even 2 of 3 structural drags can kill a strategy.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cortex.store import CortexStore

_OPS = {
    "gte": lambda a, b: a >= b,
    "gt":  lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt":  lambda a, b: a < b,
}


def synthesize(
    matched_tripwire_ids: Iterable[str],
    store: CortexStore,
) -> list[dict[str, Any]]:
    """Evaluate synthesis rules against matched tripwires; return fired rules."""
    matched = set(matched_tripwire_ids)
    if not matched:
        return []

    all_components = store.list_cost_components()
    by_id = {c["id"]: c for c in all_components}

    results: list[dict[str, Any]] = []
    for rule in store.list_synthesis_rules():
        sum_over_ids = rule.get("sum_over") or []
        active: list[dict] = []
        for cid in sum_over_ids:
            comp = by_id.get(cid)
            if comp is None:
                continue
            if comp["tripwire_id"] not in matched:
                continue
            active.append(comp)
        if not active:
            continue

        total = 0.0
        for comp in active:
            signed = comp["value"] if comp["sign"] == "drag" else -comp["value"]
            total += signed

        op = rule.get("op", "gte")
        threshold = float(rule.get("threshold", 0.0))
        op_fn = _OPS.get(op)
        if op_fn is None or not op_fn(total, threshold):
            continue

        total_rounded = round(total, 2)
        try:
            msg = rule["message"].format(
                sum=total_rounded,
                total=total_rounded,
                threshold=threshold,
                n=len(active),
            )
        except (KeyError, IndexError):
            msg = rule.get("message", "")

        results.append({
            "id": rule["id"],
            "total": total_rounded,
            "threshold": threshold,
            "op": op,
            "n_components": len(active),
            "components": active,
            "message": msg,
        })

    return results
