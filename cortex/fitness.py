"""Composite fitness scoring for tripwires (Phase 0, Autonomous Epistemic Loop).

Derives a numeric fitness per tripwire from three *implicit* signals that
already live in the audit log, without requiring a human to hand-label
brief relevance:

1. caught   -- tripwire warned, agent did NOT violate the lesson before the
               next inject (implicit positive).
2. ignored  -- tripwire warned, agent violated anyway; `potential_violation`
               event referencing the same tripwire landed in the window
               between this inject and the next (implicit hard negative).
3. surprise -- the Day-14 `<cortex_predict>` failure_mode text shares >= N
               content tokens with the tripwire body, meaning the agent
               itself named the same failure the tripwire predicted
               (implicit positive: the brief was consumed during reasoning).
4. frustration -- the next user prompt after an inject scored high on a
               regex of corrective language ("нет", "откати", "undo", "why
               did you", ...). Soft negative; the brief may have been
               irrelevant or caused a wrong action.

Composite:

    fitness = W_CAUGHT * caught
            + W_IGNORED * ignored        (negative weight)
            + W_SURPRISE * surprise
            + W_FRUSTRATION * frustration (negative weight)
            + COST_SCALE * cost_avoided

Individual signals are weak but composite over many sessions they become
actionable. Negative fitness = candidate for rewording or depreciation;
strongly positive fitness = keep and consider expanding the trigger set.

This module is PURE over `(sessions, tripwires)` -- no IO, no LLM calls,
no writes. The CLI wires it to `collect_sessions` + the store.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# --------------------------------------------------------------------
# Tunable weights. Kept as module-level constants so research / DMN can
# sweep them in replay without touching the scoring logic.
# --------------------------------------------------------------------

W_CAUGHT: float = 1.0
W_IGNORED: float = -2.0       # failure to obey is costlier than success
W_SURPRISE: float = 0.5
W_FRUSTRATION: float = -0.3

# Cost weight is a SOFT modifier: log1p(cost_usd) per caught, scaled small.
# A $500 tripwire contributes ~+0.06 per caught hit -- enough to break ties
# in favor of historically expensive lessons, but not enough to drown out
# the caught/ignored/surprise/frustration signal. Cost_usd is the historical
# cost of ONE past incident, NOT a per-inject saving, so the linear sum
# would massively overstate impact.
COST_WEIGHT_SCALE: float = 0.01

# Minimum content-token overlap required to call a surprise-body match.
# Three tokens is tight enough to avoid "fee" alone matching any fee
# tripwire, loose enough that "forgot to subtract entry price" matches
# a real_entry_price tripwire body.
SURPRISE_MIN_OVERLAP: int = 3

# Saturation point for the frustration scorer: 3+ regex hits in the first
# 120 chars of a prompt clamp to 1.0.
FRUSTRATION_SATURATE: int = 3

# A next-prompt frustration score above this threshold counts as one
# negative signal against the tripwires in the preceding inject.
FRUSTRATION_THRESHOLD: float = 0.5


# --------------------------------------------------------------------
# Frustration scorer: classify the FIRST 120 chars of a prompt.
# --------------------------------------------------------------------

_FRUSTRATION_PATTERNS_RU = [
    r"\bнет\b",
    r"\bне так\b",
    r"\bне то\b",
    r"\bзачем\b",
    r"\bоткат",
    r"\bсломал",
    r"\bне работает",
    r"\bя же говорил",
    r"\bа почему",
    r"\bвернись",
    r"\bверни\b",
    r"\bубери\b",
    r"\bстоп\b",
]
_FRUSTRATION_PATTERNS_EN = [
    r"\bno,",
    r"\bno\b\.",
    r"\bthat'?s wrong\b",
    r"\brevert\b",
    r"\bundo\b",
    r"\bwhy did you\b",
    r"\byou broke\b",
    r"\bnot what\b",
    r"\brollback\b",
    r"\bstop\b",
    r"\bi said\b",
    r"\bthat's not\b",
]

_FRUSTRATION_RE = re.compile(
    "|".join(_FRUSTRATION_PATTERNS_RU + _FRUSTRATION_PATTERNS_EN),
    re.IGNORECASE,
)


def score_prompt_frustration(prompt: str, head_len: int = 120) -> float:
    """Return a [0.0, 1.0] score of how frustrated the prompt sounds.

    Frustration is almost always front-loaded ("нет, ты сломал" / "no,
    revert") so we only scan the first `head_len` characters. A prompt
    that merely contains the word "no" somewhere in the middle is not
    flagged.

    Saturates at `FRUSTRATION_SATURATE` hits.
    """
    if not prompt:
        return 0.0
    head = prompt[:head_len]
    hits = len(_FRUSTRATION_RE.findall(head))
    if hits == 0:
        return 0.0
    return min(hits / float(FRUSTRATION_SATURATE), 1.0)


# --------------------------------------------------------------------
# Surprise matcher: failure_mode text <-> tripwire body.
# --------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-zА-Яа-я0-9_]+")

# Short, practical stopword set. Not a serious NLP list -- just trims the
# most common filler so a 3-token overlap floor actually means something.
_STOPWORDS: set[str] = {
    # English
    "a", "an", "and", "the", "is", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "be", "or", "if", "it", "this", "that", "from",
    "are", "was", "were", "will", "not", "no", "can", "may", "should",
    "would", "could", "do", "does", "did", "have", "has", "had", "but",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their",
    "its", "me", "us", "them", "so", "than", "then", "there", "here",
    "all", "any", "some", "most", "more", "less", "too", "very", "just",
    "also", "only", "own", "such", "same", "other", "new", "old",
    # Russian
    "и", "в", "на", "с", "по", "не", "что", "это", "как", "из", "за",
    "к", "о", "но", "то", "же", "бы", "а", "у", "ли", "или", "чтобы",
    "для", "при", "со", "об", "обо", "над", "под", "до", "от", "без",
    "я", "ты", "мы", "вы", "он", "она", "оно", "они", "мне", "тебе",
    "ему", "ей", "им", "его", "её", "их", "был", "была", "было",
    "будет", "есть", "нет", "да", "вот", "уже", "еще", "только",
    "очень", "надо", "можно", "нужно", "теперь",
}


def _content_tokens(text: str) -> set[str]:
    """Lowercase tokens minus stopwords minus single-char tokens."""
    if not text:
        return set()
    raw = {m.lower() for m in _WORD_RE.findall(text)}
    return {t for t in raw if len(t) > 1 and t not in _STOPWORDS}


def match_surprise_to_tripwires(
    failure_mode: str,
    tripwire_bodies: dict[str, str],
    min_overlap: int = SURPRISE_MIN_OVERLAP,
) -> list[str]:
    """Return tripwire ids whose body shares >= `min_overlap` content
    tokens with the prediction's failure_mode.

    This is how we detect that a `<cortex_predict>` failure_mode lines up
    with a tripwire in the preceding inject -- evidence the agent
    consumed the brief during reasoning.
    """
    fm_tokens = _content_tokens(failure_mode)
    if len(fm_tokens) < min_overlap:
        return []
    matched: list[str] = []
    for tw_id, body in tripwire_bodies.items():
        body_tokens = _content_tokens(body)
        overlap = fm_tokens & body_tokens
        if len(overlap) >= min_overlap:
            matched.append(tw_id)
    return matched


# --------------------------------------------------------------------
# The main aggregator.
# --------------------------------------------------------------------


def _empty_row() -> dict[str, Any]:
    return {
        "hits": 0,
        "caught": 0,
        "ignored": 0,
        # surprise_ok is a float in the row because Day 16 Haiku
        # classification contributes fractional weights (partial=0.5,
        # mismatch=1.0). The Day-14 token-overlap heuristic still emits
        # integer 1 per pair, so the representation is a strict
        # superset of the previous behavior.
        "surprise_ok": 0.0,
        "frustration": 0,
        "cost_weight": 0.0,
        "fitness": 0.0,
        # Day 16: track distinct session ids per tripwire for the DMN
        # promoter's anti-replay-loop gate. Stripped from the row
        # before JSON serialization / CLI rendering below.
        "session_ids": set(),
        # Day 16: count of Haiku-classified `label='mismatch'` pairs
        # attributed to this tripwire in the fitness window. Feeds the
        # promoter's MIN_MISMATCHES threshold.
        "mismatches": 0,
    }


def _cost_factor(cost_usd: float) -> float:
    """Soft per-caught contribution from a tripwire's historical cost.

    Uses log1p so that $500 ~ 0.062 and $5000 ~ 0.085 -- the difference
    between 'expensive' and 'very expensive' lessons is compressed but
    still ordered. Returns 0 for non-positive costs.
    """
    import math

    if cost_usd <= 0:
        return 0.0
    return math.log1p(cost_usd) * COST_WEIGHT_SCALE


_LABEL_WEIGHTS: dict[str, float] = {
    "match": 0.0,
    "partial": 0.5,
    "mismatch": 1.0,
    "error": 0.0,
}


def compute_fitness(
    sessions: list[tuple[str, list[dict[str, Any]]]],
    tripwire_bodies: dict[str, str] | None = None,
    tripwire_costs: dict[str, float] | None = None,
    classification_index: dict[tuple[str, str], str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate per-tripwire fitness over all session event streams.

    `sessions` is the output of `cortex.stats.collect_sessions`.
    `tripwire_bodies` / `tripwire_costs` map tripwire id -> body text /
    cost_usd. When a body is missing the surprise match silently skips
    that tripwire; when a cost is missing it's treated as zero.

    `classification_index` (Day 16, optional) maps
    `(session_id, prediction.at_iso) -> label` where label is one of
    `match`, `mismatch`, `partial`, `error`. When a classification
    exists for a prediction, it REPLACES the token-overlap heuristic
    for that pair (partial=0.5, mismatch=1.0, match/error=0.0 signal).
    When the index is absent or the specific pair is not in it, the
    function falls back to the Day-14 token overlap unchanged.

    Returns `{tripwire_id: row}` where `row` has the eight fields shown
    in `_empty_row()` plus the composite `fitness` number and a
    `distinct_sessions` integer. `session_ids` is stripped before
    return; consumers use `distinct_sessions` instead.
    """
    bodies = tripwire_bodies or {}
    costs = tripwire_costs or {}
    cls_index = classification_index or {}

    stats: dict[str, dict[str, Any]] = defaultdict(_empty_row)

    for _sid, events in sessions:
        # Index every event position that counts as an "injection point"
        # -- both primary inject and keyword_fallback -- so we can build
        # the (inject[i], inject[i+1]) windows to attribute violations
        # and next-prompt frustration to.
        inject_positions: list[int] = [
            i
            for i, e in enumerate(events)
            if e.get("event") in ("inject", "keyword_fallback")
        ]

        for idx, pos in enumerate(inject_positions):
            inject_ev = events[pos]
            tw_ids: list[str] = list(inject_ev.get("tripwire_ids") or [])
            if not tw_ids:
                continue

            # Window: from this inject (exclusive) to the next inject
            # (exclusive) OR end-of-session.
            window_end = (
                inject_positions[idx + 1]
                if idx + 1 < len(inject_positions)
                else len(events)
            )
            window = events[pos + 1 : window_end]

            # Which tripwires in this inject got violated within the window?
            violated_ids: set[str] = set()
            for ev in window:
                if ev.get("event") == "potential_violation":
                    vid = ev.get("tripwire_id") or ""
                    if vid:
                        violated_ids.add(vid)

            # Surprise: per-pair classification override wins; fall
            # back to the Day-14 token overlap heuristic otherwise.
            # Tracked as a per-tripwire float so partial labels can
            # contribute 0.5 without losing precision.
            surprise_score: dict[str, float] = {}
            mismatch_count: dict[str, int] = {}
            for ev in window:
                if ev.get("event") != "prediction":
                    continue
                fm = ev.get("failure_mode") or ""
                cls_key = (_sid, ev.get("at", ""))
                cls_label = cls_index.get(cls_key)
                if cls_label is not None:
                    weight = _LABEL_WEIGHTS.get(cls_label, 0.0)
                    if weight > 0:
                        for tid in tw_ids:
                            surprise_score[tid] = (
                                surprise_score.get(tid, 0.0) + weight
                            )
                    if cls_label == "mismatch":
                        for tid in tw_ids:
                            mismatch_count[tid] = mismatch_count.get(tid, 0) + 1
                    continue

                # Heuristic fallback (Day 14 path preserved for
                # pre-classification / never-classified pairs).
                if not fm:
                    continue
                relevant_bodies = {
                    tid: bodies.get(tid, "") for tid in tw_ids if tid in bodies
                }
                if not relevant_bodies:
                    continue
                matched = match_surprise_to_tripwires(fm, relevant_bodies)
                for tid in matched:
                    # Integer +1 per matched pair, same as before.
                    surprise_score[tid] = surprise_score.get(tid, 0.0) + 1.0

            # Frustration: read the NEXT inject's `prompt_frustration`
            # field. If the next inject didn't record a score (pre-Phase-0
            # sessions, or the hook failed to write it), treat as 0.
            next_frustration = 0.0
            if idx + 1 < len(inject_positions):
                next_inject = events[inject_positions[idx + 1]]
                try:
                    next_frustration = float(
                        next_inject.get("prompt_frustration", 0) or 0
                    )
                except (TypeError, ValueError):
                    next_frustration = 0.0

            frustrated = next_frustration >= FRUSTRATION_THRESHOLD

            for tw_id in tw_ids:
                row = stats[tw_id]
                row["hits"] += 1
                row["session_ids"].add(_sid)
                if tw_id in violated_ids:
                    row["ignored"] += 1
                else:
                    row["caught"] += 1
                    row["cost_weight"] += _cost_factor(
                        float(costs.get(tw_id, 0.0))
                    )
                if tw_id in surprise_score:
                    row["surprise_ok"] += surprise_score[tw_id]
                if tw_id in mismatch_count:
                    row["mismatches"] += mismatch_count[tw_id]
                if frustrated:
                    row["frustration"] += 1

    # Finalize composite score.
    for row in stats.values():
        row["fitness"] = round(
            W_CAUGHT * row["caught"]
            + W_IGNORED * row["ignored"]
            + W_SURPRISE * row["surprise_ok"]
            + W_FRUSTRATION * row["frustration"]
            + row["cost_weight"],
            3,
        )
        row["cost_weight"] = round(row["cost_weight"], 3)
        row["surprise_ok"] = round(row["surprise_ok"], 3)
        # Expose distinct session count, strip the internal set so
        # the row is JSON-serializable.
        row["distinct_sessions"] = len(row["session_ids"])
        del row["session_ids"]

    return dict(stats)


# --------------------------------------------------------------------
# Rendering: appended to `cortex stats --sessions` output.
# --------------------------------------------------------------------


def render_fitness_block(
    fitness: dict[str, dict[str, Any]],
    top_n: int = 15,
) -> list[str]:
    """Render a fitness section as lines of text.

    Returned as a list so `cortex.stats.render_stats` can splice it in
    without caring about trailing newlines.
    """
    if not fitness:
        return []

    lines: list[str] = []
    lines.append("Tripwire composite fitness (Phase 0):")
    lines.append(
        "  h=hits c=caught i=ignored surp=surprise_ok frust=frustration "
        "cw=cost_weight (log1p of cost_usd, soft tiebreaker)"
    )
    ranked = sorted(
        fitness.items(),
        key=lambda kv: (-kv[1]["fitness"], -kv[1]["hits"]),
    )[:top_n]
    for tw_id, row in ranked:
        lines.append(
            f"  {tw_id:<28} "
            f"h={row['hits']:<3} c={row['caught']:<3} i={row['ignored']:<3} "
            f"surp={float(row['surprise_ok']):<4.1f} "
            f"frust={row['frustration']:<2} "
            f"cw={row['cost_weight']:<5.2f} fit={row['fitness']:+.2f}"
        )
    lines.append("")
    lines.append(
        f"  fitness = {W_CAUGHT:+g}*caught {W_IGNORED:+g}*ignored "
        f"{W_SURPRISE:+g}*surprise {W_FRUSTRATION:+g}*frustration "
        f"+ cost_weight"
    )
    lines.append(
        "  Strongly negative = candidate for rewording or depreciation."
    )
    lines.append("")
    return lines
