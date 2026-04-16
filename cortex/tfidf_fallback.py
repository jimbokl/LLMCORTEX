"""Keyword fallback: when the rule engine returns zero tripwires, score all
tripwires by weighted token overlap against title / triggers / body and
inject the top-K.

Pure in-process, zero deps, zero daemon, zero network. Works on any prompt
whose English tokens can be extracted by a Latin-alpha regex, which covers
mixed Russian+English naturally (the Cyrillic chars are silently dropped
and only the English tokens participate in matching).

This replaces the abandoned Day-4 Palace daemon experiment. Palace
semantic search had too narrow a coverage profile (English-only, short
generic queries failed) and too much infrastructure cost (daemon + HTTP +
ONNX warmup). TF-IDF-ish keyword scoring over the already-local tripwire
bodies gives better coverage at 1% of the complexity.

Design notes:
- Single pass over ~50 tripwires at hook time: well under 1ms.
- Scoring weights `trigger` and `title` matches higher than `body` matches
  because triggers are curated and body is verbose.
- Stopwords stripped so common English filler doesn't inflate scores.
- Minimum score threshold (default 3.0 = at least one trigger/title hit)
  prevents weak body-only matches from becoming noise.
"""
from __future__ import annotations

from typing import Any

from cortex.store import CortexStore
from cortex.tokenize import tokenize as _tokenize_shared

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "am", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "for", "with", "from", "by", "as", "into",
    "and", "or", "not", "but", "if", "then", "else", "so", "than",
    "this", "that", "these", "those", "it", "its", "we", "i", "me",
    "my", "you", "your", "our", "us", "he", "she", "him", "her", "them", "they",
    "what", "how", "why", "when", "where", "which", "who", "whom",
    "do", "does", "did", "doing", "done",
    "have", "has", "had", "having", "will", "would", "could", "should",
    "can", "may", "might", "must", "shall",
    "just", "only", "also", "too", "very", "much", "many", "some", "any",
    "all", "no", "yes", "now", "here", "there", "still", "ever", "never",
    "make", "made", "making", "want", "need", "like", "use", "used", "using",
})

TRIGGER_WEIGHT = 3.0
TITLE_WEIGHT = 3.0
BODY_WEIGHT = 1.0

DEFAULT_MIN_SCORE = 3.0
DEFAULT_TOP_K = 3


def _tokens(text: str) -> set[str]:
    """Lowercase-tokenize a string into signal-bearing word set.

    Delegates the raw tokenization to `cortex.tokenize.tokenize` (which
    honors `CORTEX_UNICODE_TOKENS`), then strips short tokens and
    English stopwords.
    """
    return {
        t for t in _tokenize_shared(text)
        if len(t) > 1 and t not in _STOPWORDS
    }


def score_tripwire(prompt_tokens: set[str], tripwire: dict) -> float:
    """Weighted token overlap. Trigger and title matches count for 3.0,
    body matches for 1.0. Each token contributes at most once, at its
    highest-weighted location."""
    triggers = {str(t).lower() for t in tripwire.get("triggers") or []}
    title_toks = _tokens(tripwire.get("title") or "")
    body_toks = _tokens(tripwire.get("body") or "")

    score = 0.0
    for tok in prompt_tokens:
        if tok in triggers:
            score += TRIGGER_WEIGHT
        elif tok in title_toks:
            score += TITLE_WEIGHT
        elif tok in body_toks:
            score += BODY_WEIGHT
    return score


def fallback_search(
    prompt: str,
    store: CortexStore,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Score every tripwire against the prompt; return top-K with score above
    threshold, sorted by descending score then severity.
    """
    prompt_tokens = _tokens(prompt)
    if not prompt_tokens:
        return []

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    scored: list[tuple[float, int, dict]] = []
    for tw in store.list_tripwires():
        score = score_tripwire(prompt_tokens, tw)
        if score >= min_score:
            scored.append((score, sev_order.get(tw["severity"], 9), tw))

    # Sort: highest score first, then most severe
    scored.sort(key=lambda t: (-t[0], t[1]))

    return [
        {**tw, "_fallback_score": round(score, 2)}
        for score, _sev, tw in scored[:top_k]
    ]


def render_fallback_brief(tripwires: list[dict]) -> str:
    """Render a short fallback brief. Intentionally more compact than the
    primary rule-engine brief -- fallback matches are advisory, not
    definitive, so they should claim less agent attention.
    """
    if not tripwires:
        return ""
    lines: list[str] = []
    lines.append(
        f'<cortex_brief source="keyword_fallback" n="{len(tripwires)}">'
    )
    lines.append(
        "Cortex rule engine had no direct match, but these tripwires scored"
    )
    lines.append(
        "high on keyword overlap. Treat as advisory context, not commands:"
    )
    lines.append("")
    for i, tw in enumerate(tripwires, 1):
        sev = tw["severity"].upper()
        score = tw.get("_fallback_score", 0.0)
        cost_note = (
            f" [past cost ${tw['cost_usd']:.2f}]" if tw.get("cost_usd", 0) > 0 else ""
        )
        lines.append(
            f"[{i}] {tw['id']}  --  {sev}  (match score {score}){cost_note}"
        )
        lines.append(f"    {tw['title']}")
        # Show only first 2 lines of body to stay compact
        body_lines = (tw.get("body") or "").split("\n")[:2]
        for bl in body_lines:
            if bl.strip():
                lines.append(f"    {bl}")
        lines.append(f"    ... `cortex show {tw['id']}` for the full lesson")
        lines.append("")
    lines.append("</cortex_brief>")
    return "\n".join(lines)
