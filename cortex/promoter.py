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

Stage 2 of the Day 16 rollout implements the classification layer only;
the decider and applier arrive in stage 3.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
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
