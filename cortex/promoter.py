"""Day 16 -- DMN Promoter.

Autonomous subsystem that reads Surprise Engine pairs, classifies each
one via Haiku, aggregates the labels into the Phase-0 composite fitness
score, and promotes / demotes / archives tripwires between the
`active`, `shadow`, and `archived` lifecycle states without human
intervention.

Layered into three concerns:

  1. Classification  (`classify_pair`, `parse_classification`) --
     takes one raw surprise pair, asks Haiku for a single JSON verdict,
     parses defensively, returns a row ready for
     `store.upsert_pair_classification()`. Fail-safe: any parse error
     becomes a persisted `label='error'` row so we never retry
     endlessly on a malformed response.

  2. Decision       (`decide`) -- pure function over `(tripwires,
     fitness, distinct_sessions, mismatches, status_history, now)`.
     No store, no LLM, no wall clock. Returns a list of
     `PromoterDecision` objects; daily caps are applied by the
     applier, not by this function.

  3. Application    (`apply_decisions`) -- the ONLY mutation path.
     Enforces per-day caps by counting today's `status_changes`
     rows, ranks the remaining decisions, and writes each one as a
     `status_changes` row + a `status_change` session event inside
     a single `store.apply_status_transition()` transaction.

All time-sensitive logic reads the clock from the module-level `_now()`
function so tests can monkeypatch `cortex.promoter._now` to return a
fixed datetime. No `freezegun` dependency.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from cortex.promoter_prompt import build_classification_prompt

# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------

DEFAULT_MODEL: str = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS: int = 256

_VALID_LABELS = ("match", "mismatch", "partial")

# Confidence fallback when Haiku omits the field entirely but did give a
# label -- mid-range so the row still contributes if we later filter by
# a confidence cutoff.
_DEFAULT_CONFIDENCE: float = 0.5

# --------------------------------------------------------------------
# Phase-0 promoter thresholds. Kept module-level so tests can monkey-
# patch them without reaching into the decider body. The numbers are
# grounded in the empirical distribution of hits/caught/cost_weight
# observed on the live store as of 2026-04-11 (13 tripwires, median
# hit count 8, max fitness +45.67, zero shadow tripwires yet).
# --------------------------------------------------------------------

# Shadow -> active primary gate (all must hold).
MIN_HITS: int = 5
MIN_DISTINCT_SESSIONS: int = 3
MIN_TENURE_HOURS: int = 168            # 7 days in shadow before eligible
MIN_FITNESS: float = 5.0
MIN_MISMATCHES: int = 2                # Haiku-classified, within window
MIN_CAUGHT_RATE: float = 0.8

# Shadow -> active fallback clause (classification-free): lets an
# obviously-good rule promote even if `cortex promote classify` has
# never been run, so the system is not permanently blocked on Haiku
# availability.
FALLBACK_FITNESS: float = 10.0
FALLBACK_DISTINCT_SESSIONS: int = 5
FALLBACK_CAUGHT_RATE: float = 0.9

# Demotion thresholds.
MAX_IGNORED_RATE_ACTIVE: float = 0.5
MAX_IGNORED_RATE_SHADOW: float = 0.8
MIN_HITS_FOR_IGNORED_DEMOTE: int = 3   # anti-noise floor on rate-based demote
SHADOW_TENURE_HOURS_FOR_ARCHIVE: int = 336   # 14 days in shadow without promote
DORMANT_HOURS: int = 720               # 30 days with zero hits
NEGATIVE_FITNESS_TENURE_HOURS: int = 168  # fitness < 0 and been around 7+ days

# Cooldown: if the tripwire has >= 2 status changes in the last 7 days,
# no automated change applies for another 7 days from the most recent.
COOLDOWN_RECENT_CHANGES: int = 2
COOLDOWN_HOURS: int = 168

# Per-day action caps. Checked in `apply_decisions` against today's
# `status_changes` rows so a loop-driven caller cannot burst.
MAX_PROMOTIONS_PER_DAY: int = 1
MAX_DEMOTIONS_PER_DAY: int = 3


# --------------------------------------------------------------------
# Injectable clock. Tests monkeypatch this attribute.
# --------------------------------------------------------------------


def _now() -> datetime:
    """Return the current UTC time. Tests override this via monkeypatch
    so time-dependent decider logic stays hermetic."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------
# Response parser
# --------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_classification(text: str) -> dict[str, Any]:
    """Extract `{label, confidence, reasoning}` from a Haiku reply.

    Defensive: strips markdown code fences, walks from the first `{`
    to the last `}`, runs `json.loads`, validates the label against
    the enum, clamps confidence to [0.0, 1.0], and truncates the
    reasoning string to 300 characters. On any failure (empty input,
    unparseable JSON, unknown label, etc.) returns a row with
    `label="error"` and confidence 0 -- this is persisted as-is so
    the pair does not get retried on the next `cortex promote classify`
    run.

    Mirrors the approach used in `cortex/dmn.py::parse_proposals` but
    adapted for a single-object response.
    """
    if not text:
        return _error_row("empty response")

    # Strip obvious markdown code fences Haiku sometimes emits despite
    # the "no prose, no code fences" instruction.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (```json or ```), keep the body, drop
        # any trailing fence.
        cleaned = re.sub(r"^```[a-zA-Z0-9]*\s*", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # Pull the first {...} block. json.loads will complain if there's
    # trailing prose, and the search avoids that.
    match = _JSON_OBJECT_RE.search(cleaned)
    if not match:
        return _error_row("no json object found")

    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _error_row(f"json decode failed: {exc.msg[:80]}")

    if not isinstance(obj, dict):
        return _error_row("response was not a json object")

    label = obj.get("label")
    if not isinstance(label, str) or label.lower() not in _VALID_LABELS:
        return _error_row(f"unknown label: {label!r}")
    label = label.lower()

    raw_conf = obj.get("confidence", _DEFAULT_CONFIDENCE)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = _DEFAULT_CONFIDENCE
    # Clamp to [0.0, 1.0]
    confidence = max(0.0, min(1.0, confidence))

    raw_reason = obj.get("reasoning", "")
    if not isinstance(raw_reason, str):
        raw_reason = str(raw_reason)
    reasoning = raw_reason.strip()[:300]

    return {
        "label": label,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def _error_row(message: str) -> dict[str, Any]:
    return {
        "label": "error",
        "confidence": 0.0,
        "reasoning": message[:300],
    }


# --------------------------------------------------------------------
# Haiku call wrapper
# --------------------------------------------------------------------


def classify_pair(
    pair: dict[str, Any],
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    call_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Classify one surprise pair end-to-end: build prompt, call Haiku,
    parse the response.

    Returns a dict with keys `label`, `confidence`, `reasoning`,
    `model`, `prompt_tokens` (estimate), ready to be written to
    `store.upsert_pair_classification()`. Errors (parse failure,
    network failure, missing SDK) yield a row with `label="error"` so
    the caller persists the outcome and the pair is not retried.

    `call_fn` is an optional override used by tests: a callable that
    receives `(prompt, model, max_tokens, client)` and returns the
    Haiku response text. Default implementation delegates to
    `cortex.dmn.call_haiku` so the Haiku client wiring stays in one
    place.
    """
    prompt = build_classification_prompt(pair)
    prompt_tokens = len(prompt) // 4  # rough estimate, same heuristic as dmn.py

    if call_fn is None:
        # Lazy-import so tests that stub `call_fn` don't need the
        # anthropic SDK installed, and promoter.py stays importable
        # in environments without the [dmn] extra.
        from cortex.dmn import call_haiku as _call_haiku

        def _default_call(p: str, m: str, mt: int, c: Any) -> str:
            return _call_haiku(p, model=m, max_tokens=mt, client=c)

        call_fn = _default_call

    try:
        response_text = call_fn(prompt, model, max_tokens, client)
    except Exception as exc:  # pragma: no cover - network path
        parsed = _error_row(f"haiku call failed: {type(exc).__name__}")
        parsed["model"] = model
        parsed["prompt_tokens"] = prompt_tokens
        return parsed

    parsed = parse_classification(response_text)
    parsed["model"] = model
    parsed["prompt_tokens"] = prompt_tokens
    return parsed


# --------------------------------------------------------------------
# Decider: pure function over store snapshots.
# --------------------------------------------------------------------


@dataclass(frozen=True)
class PromoterDecision:
    """One proposed action the DMN promoter would like to apply.

    `priority` ranks decisions when daily caps are binding in the
    applier. Lower priority number = higher importance (so a
    `ignored_rate` demotion fires before a `dormant` demotion if
    both land on the same day and only two demotion slots are free).
    `fitness_delta_hint` is the rough score impact, used as a secondary
    sort key within the same priority.
    """

    tripwire_id: str
    from_status: str
    to_status: str
    reason: str
    metadata: dict
    priority: int = 100
    fitness_delta_hint: float = 0.0


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _hours_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def _is_in_cooldown(
    status_history: list[dict[str, Any]],
    now: datetime,
) -> bool:
    """True if this tripwire has flapped recently and should be left
    alone. Counts entries in `status_history` (newest-first from
    `store.list_status_changes`) within COOLDOWN_HOURS back from now.
    """
    window_start = now - timedelta(hours=COOLDOWN_HOURS)
    recent = 0
    for entry in status_history:
        at = _parse_iso(entry.get("at"))
        if at is None:
            continue
        if at >= window_start:
            recent += 1
    return recent >= COOLDOWN_RECENT_CHANGES


def _most_recent_status_entry(
    status_history: list[dict[str, Any]],
    to_status: str,
) -> dict[str, Any] | None:
    """Return the most recent audit row where `to_status` matches.
    Assumes history is newest-first (the list_status_changes contract).
    """
    for entry in status_history:
        if entry.get("to_status") == to_status:
            return entry
    return None


def _tenure_hours_in_current_status(
    tripwire: dict[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
) -> float:
    """How long (in hours) the tripwire has held its current status.

    Looks for the most recent `status_changes` row whose `to_status`
    matches the live status. Falls back to `born_at` if no audit row
    exists (pre-Day-16 tripwires, or rules imported through
    `cortex migrate` that never transitioned).
    """
    current_status = tripwire.get("status", "active")
    entry = _most_recent_status_entry(history, current_status)
    marker: datetime | None = None
    if entry:
        marker = _parse_iso(entry.get("at"))
    if marker is None:
        marker = _parse_iso(tripwire.get("born_at"))
    if marker is None:
        return 0.0
    return max(0.0, _hours_between(marker, now))


def _caught_rate(fitness_row: dict[str, Any]) -> float:
    hits = int(fitness_row.get("hits", 0) or 0)
    if hits <= 0:
        return 0.0
    caught = int(fitness_row.get("caught", 0) or 0)
    return caught / hits


def _ignored_rate(fitness_row: dict[str, Any]) -> float:
    hits = int(fitness_row.get("hits", 0) or 0)
    if hits <= 0:
        return 0.0
    ignored = int(fitness_row.get("ignored", 0) or 0)
    return ignored / hits


def _meets_primary_promotion_gate(
    fitness_row: dict[str, Any],
    distinct_sessions: int,
    mismatches: int,
    tenure_hours: float,
) -> bool:
    if tenure_hours < MIN_TENURE_HOURS:
        return False
    if int(fitness_row.get("hits", 0) or 0) < MIN_HITS:
        return False
    if distinct_sessions < MIN_DISTINCT_SESSIONS:
        return False
    if float(fitness_row.get("fitness", 0.0) or 0.0) < MIN_FITNESS:
        return False
    if mismatches < MIN_MISMATCHES:
        return False
    if _caught_rate(fitness_row) < MIN_CAUGHT_RATE:
        return False
    return True


def _meets_fallback_promotion_gate(
    fitness_row: dict[str, Any],
    distinct_sessions: int,
    tenure_hours: float,
) -> bool:
    """Classification-free promotion path: requires strictly higher
    fitness, more distinct sessions, and a tighter caught rate so that
    a rule can promote on structural signal alone when Haiku has never
    been run against this window.
    """
    if tenure_hours < MIN_TENURE_HOURS:
        return False
    if int(fitness_row.get("hits", 0) or 0) < MIN_HITS:
        return False
    if distinct_sessions < FALLBACK_DISTINCT_SESSIONS:
        return False
    if float(fitness_row.get("fitness", 0.0) or 0.0) < FALLBACK_FITNESS:
        return False
    if _caught_rate(fitness_row) < FALLBACK_CAUGHT_RATE:
        return False
    return True


def decide(
    tripwires: list[dict[str, Any]],
    fitness: dict[str, dict[str, Any]],
    distinct_sessions: dict[str, int],
    mismatches: dict[str, int],
    status_history: dict[str, list[dict[str, Any]]],
    now: datetime | None = None,
) -> list[PromoterDecision]:
    """Pure decider. Returns a list of proposed `PromoterDecision`s
    without mutating anything.

    Arguments:
      * `tripwires`           -- list of tripwire dicts (as returned by
                                `store.list_tripwires(status=None)`).
                                Must include `id`, `status`, `born_at`.
      * `fitness`             -- output of `compute_fitness`:
                                `{tripwire_id: row}` with hits, caught,
                                ignored, surprise_ok, fitness, ...
      * `distinct_sessions`   -- `{tripwire_id: int}` count of unique
                                sessions this tripwire matched within
                                the fitness window.
      * `mismatches`          -- `{tripwire_id: int}` count of
                                Haiku-classified `label='mismatch'`
                                pairs tied to this tripwire in the
                                window.
      * `status_history`      -- `{tripwire_id: [audit_rows]}` newest
                                first, the output of
                                `store.list_status_changes`.
      * `now`                 -- injected clock; defaults to `_now()`.

    The function is *pure*: same inputs, same outputs, no IO. Daily
    caps are NOT applied here -- the applier handles that so that
    `decide` output is a stable "what the system would do absent
    throughput limits" snapshot useful for audit.
    """
    if now is None:
        now = _now()
    decisions: list[PromoterDecision] = []

    for tw in tripwires:
        tw_id = tw.get("id")
        if not tw_id:
            continue
        status = tw.get("status", "active")
        history = status_history.get(tw_id, [])

        if _is_in_cooldown(history, now):
            continue

        row = fitness.get(tw_id, {})
        sessions = int(distinct_sessions.get(tw_id, 0) or 0)
        mm = int(mismatches.get(tw_id, 0) or 0)
        tenure_hours = _tenure_hours_in_current_status(tw, history, now)
        hits = int(row.get("hits", 0) or 0)
        fit = float(row.get("fitness", 0.0) or 0.0)

        if status == "shadow":
            decision = _decide_shadow(
                tw_id, row, sessions, mm, tenure_hours, hits, fit
            )
            if decision is not None:
                decisions.append(decision)
            continue

        if status == "active":
            decision = _decide_active(
                tw_id, tw, row, sessions, tenure_hours, hits, fit
            )
            if decision is not None:
                decisions.append(decision)
            continue

        # `archived` is a terminal state. Day 16 never auto-unarchives.
    return decisions


def _decide_shadow(
    tw_id: str,
    row: dict[str, Any],
    sessions: int,
    mismatches: int,
    tenure_hours: float,
    hits: int,
    fit: float,
) -> PromoterDecision | None:
    # Shadow -> archived on heavy ignored rate.
    if (
        _ignored_rate(row) >= MAX_IGNORED_RATE_SHADOW
        and hits >= MIN_HITS
    ):
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="shadow",
            to_status="archived",
            reason="shadow_ignored_rate",
            metadata={
                "fitness": fit,
                "hits": hits,
                "ignored_rate": _ignored_rate(row),
            },
            priority=30,
            fitness_delta_hint=-fit,
        )

    # Primary promotion gate.
    if _meets_primary_promotion_gate(row, sessions, mismatches, tenure_hours):
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="shadow",
            to_status="active",
            reason="primary_gate",
            metadata={
                "fitness": fit,
                "hits": hits,
                "caught_rate": _caught_rate(row),
                "distinct_sessions": sessions,
                "mismatches": mismatches,
                "tenure_hours": tenure_hours,
            },
            priority=10,
            fitness_delta_hint=fit,
        )

    # Classification-free fallback promotion.
    if _meets_fallback_promotion_gate(row, sessions, tenure_hours):
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="shadow",
            to_status="active",
            reason="fallback_gate",
            metadata={
                "fitness": fit,
                "hits": hits,
                "caught_rate": _caught_rate(row),
                "distinct_sessions": sessions,
                "tenure_hours": tenure_hours,
            },
            priority=20,
            fitness_delta_hint=fit,
        )

    # Shadow -> archived after long tenure without ever hitting the gate.
    if tenure_hours >= SHADOW_TENURE_HOURS_FOR_ARCHIVE:
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="shadow",
            to_status="archived",
            reason="shadow_tenure_expired",
            metadata={"tenure_hours": tenure_hours, "fitness": fit},
            priority=50,
            fitness_delta_hint=-fit,
        )

    return None


def _decide_active(
    tw_id: str,
    tw: dict[str, Any],
    row: dict[str, Any],
    sessions: int,
    tenure_hours: float,
    hits: int,
    fit: float,
) -> PromoterDecision | None:
    # Active -> shadow on high ignored rate (lesson is being ignored).
    if (
        _ignored_rate(row) >= MAX_IGNORED_RATE_ACTIVE
        and hits >= MIN_HITS_FOR_IGNORED_DEMOTE
    ):
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="active",
            to_status="shadow",
            reason="active_ignored_rate",
            metadata={
                "fitness": fit,
                "hits": hits,
                "ignored_rate": _ignored_rate(row),
            },
            priority=15,
            fitness_delta_hint=-fit,
        )

    # Active -> shadow when fitness has gone negative and the rule is
    # mature. Approximates "fitness < 0 for 7+ days" without a history
    # table -- documented limitation, upgrades to rolling window in
    # Day 17+.
    if fit < 0 and tenure_hours >= NEGATIVE_FITNESS_TENURE_HOURS:
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="active",
            to_status="shadow",
            reason="negative_fitness",
            metadata={"fitness": fit, "tenure_hours": tenure_hours},
            priority=25,
            fitness_delta_hint=-fit,
        )

    # Active -> shadow when the rule has been dormant for a month.
    if hits == 0 and tenure_hours >= DORMANT_HOURS:
        return PromoterDecision(
            tripwire_id=tw_id,
            from_status="active",
            to_status="shadow",
            reason="dormant",
            metadata={"tenure_hours": tenure_hours, "hits": hits},
            priority=60,
            fitness_delta_hint=0.0,
        )

    return None


# --------------------------------------------------------------------
# Applier: the only mutation path.
# --------------------------------------------------------------------


@dataclass
class AppliedChange:
    tripwire_id: str
    from_status: str
    to_status: str
    reason: str
    metadata: dict
    applied: bool
    skip_reason: str = ""


def _count_today(
    store: Any, kind: str, now: datetime, predicate: Callable[[str, str], bool]
) -> int:
    """Count `status_changes` rows from the last 24h whose (from, to)
    transition matches `predicate`. `kind` is just a telemetry label.
    """
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = store.list_status_changes(since_iso=since)
    return sum(1 for r in rows if predicate(r["from_status"], r["to_status"]))


def _is_promotion(from_status: str, to_status: str) -> bool:
    # Day 16 treats shadow -> active as the only promotion.
    return from_status == "shadow" and to_status == "active"


def _is_demotion(from_status: str, to_status: str) -> bool:
    # Every other state reduction counts as a demotion for cap purposes.
    if from_status == to_status:
        return False
    if _is_promotion(from_status, to_status):
        return False
    return True


def apply_decisions(
    store: Any,
    decisions: list[PromoterDecision],
    session_id: str | None,
    now: datetime | None = None,
    *,
    dry_run: bool = True,
) -> list[AppliedChange]:
    """Apply decisions against the store, honoring per-day caps.

    Ranking (ascending): `(priority, -abs(fitness_delta_hint))`. Lower
    `priority` fires first; within a priority band, higher-impact
    decisions win. Daily caps short-circuit remaining decisions with a
    `skip_reason='daily_cap'` entry so the caller can log what was
    suppressed.

    `dry_run=True` (default) means NOTHING is written: every decision
    comes back as `applied=False skip_reason='dry_run'`. Only
    `dry_run=False` actually calls `store.apply_status_transition`.
    """
    if now is None:
        now = _now()

    used_promotions = _count_today(store, "promotion", now, _is_promotion)
    used_demotions = _count_today(store, "demotion", now, _is_demotion)

    ordered = sorted(
        decisions,
        key=lambda d: (d.priority, -abs(d.fitness_delta_hint)),
    )

    results: list[AppliedChange] = []
    for d in ordered:
        is_promo = _is_promotion(d.from_status, d.to_status)
        is_demo = _is_demotion(d.from_status, d.to_status)

        if is_promo and used_promotions >= MAX_PROMOTIONS_PER_DAY:
            results.append(
                AppliedChange(
                    tripwire_id=d.tripwire_id,
                    from_status=d.from_status,
                    to_status=d.to_status,
                    reason=d.reason,
                    metadata=d.metadata,
                    applied=False,
                    skip_reason="daily_cap_promotions",
                )
            )
            continue

        if is_demo and used_demotions >= MAX_DEMOTIONS_PER_DAY:
            results.append(
                AppliedChange(
                    tripwire_id=d.tripwire_id,
                    from_status=d.from_status,
                    to_status=d.to_status,
                    reason=d.reason,
                    metadata=d.metadata,
                    applied=False,
                    skip_reason="daily_cap_demotions",
                )
            )
            continue

        if dry_run:
            results.append(
                AppliedChange(
                    tripwire_id=d.tripwire_id,
                    from_status=d.from_status,
                    to_status=d.to_status,
                    reason=d.reason,
                    metadata=d.metadata,
                    applied=False,
                    skip_reason="dry_run",
                )
            )
            continue

        outcome = store.apply_status_transition(
            tripwire_id=d.tripwire_id,
            to_status=d.to_status,
            reason=d.reason,
            metadata=d.metadata,
            session_id=session_id,
        )
        if outcome is None:
            results.append(
                AppliedChange(
                    tripwire_id=d.tripwire_id,
                    from_status=d.from_status,
                    to_status=d.to_status,
                    reason=d.reason,
                    metadata=d.metadata,
                    applied=False,
                    skip_reason="noop_or_missing",
                )
            )
            continue

        # Mirror the transition into the session audit log so a human
        # can see it on `cortex timeline <sid>` alongside tool_call
        # events without cracking open SQLite.
        if session_id:
            try:
                from cortex.session import log_event

                log_event(
                    session_id,
                    "status_change",
                    {
                        "tripwire_id": d.tripwire_id,
                        "from": outcome["from_status"],
                        "to": outcome["to_status"],
                        "reason": d.reason,
                    },
                )
            except Exception:
                pass  # fail-open: session-log write is best-effort

        results.append(
            AppliedChange(
                tripwire_id=d.tripwire_id,
                from_status=outcome["from_status"],
                to_status=outcome["to_status"],
                reason=d.reason,
                metadata=d.metadata,
                applied=True,
            )
        )
        if is_promo:
            used_promotions += 1
        if is_demo:
            used_demotions += 1

    return results
