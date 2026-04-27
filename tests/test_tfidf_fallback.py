from pathlib import Path

from cortex.importers.memory_md import run_migration
from cortex.store import CortexStore
from cortex.tfidf_fallback import (
    _tokens,
    fallback_search,
    render_fallback_brief,
    score_tripwire,
)


def _seeded_store(tmp_path: Path) -> CortexStore:
    db = str(tmp_path / "seed.db")
    run_migration(db)
    return CortexStore(db)


def test_tokens_strips_stopwords_and_short_words():
    assert _tokens("the quick backtest of the data") == {"quick", "backtest", "data"}
    assert _tokens("I am doing a backtest") == {"backtest"}


def test_tokens_drops_cyrillic_keeps_latin():
    # Regex is [a-z0-9_\-]+ so Cyrillic is silently dropped
    assert _tokens("покажи топ фичи по pnl") == {"pnl"}


def test_score_tripwire_prefers_triggers():
    tw = {
        "triggers": ["secret", "log"],
        "title": "redact secret tokens before logging",
        "body": "some text mentioning aggregators and headers",
    }
    # 'secret' in triggers -> 3.0
    assert score_tripwire({"secret"}, tw) == 3.0
    # 'redact' in title -> 3.0
    assert score_tripwire({"redact"}, tw) == 3.0
    # 'aggregators' in body only -> 1.0
    assert score_tripwire({"aggregators"}, tw) == 1.0
    # unrelated
    assert score_tripwire({"unrelated"}, tw) == 0.0


def test_score_tripwire_takes_highest_location(tmp_path):
    """If the same token appears in triggers AND body, count it once at
    the highest weight."""
    tw = {
        "triggers": ["secret"],
        "title": "secret",
        "body": "secret mentioned in body too",
    }
    # 'secret' in triggers (3.0), title (3.0), body (1.0) -- count once at 3.0
    assert score_tripwire({"secret"}, tw) == 3.0


def test_fallback_search_finds_secret_tripwires(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        # secrets_in_logs has 'secret' / 'token' in triggers
        hits = fallback_search(
            "user wants to log a secret token for debugging", store,
        )
    finally:
        store.close()
    ids = {h["id"] for h in hits}
    assert "secrets_in_logs" in ids


def test_fallback_search_mixed_russian_english_prompt(tmp_path):
    """A Russian-context prompt with a single English keyword still has
    to fire if that keyword matches a seeded trigger."""
    store = _seeded_store(tmp_path)
    try:
        hits = fallback_search("покажи где логируется token и password", store)
    finally:
        store.close()
    assert len(hits) > 0, (
        "fallback should fire on 'token + password' even in Cyrillic context"
    )
    ids = {h["id"] for h in hits}
    # secrets_in_logs has both token and password in its triggers.
    assert "secrets_in_logs" in ids


def test_fallback_search_trivial_prompt_returns_empty(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        assert fallback_search("hi", store) == []
        assert fallback_search("thanks", store) == []
    finally:
        store.close()


def test_fallback_search_respects_top_k(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        hits = fallback_search(
            "production deploy backtest config secret token",
            store,
            top_k=2,
        )
    finally:
        store.close()
    assert len(hits) <= 2


def test_fallback_search_sorts_by_score_then_severity(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        hits = fallback_search(
            "production deploy backtest config secret token",
            store,
            top_k=5,
        )
    finally:
        store.close()
    scores = [h["_fallback_score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_fallback_search_empty_prompt_returns_empty(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        assert fallback_search("", store) == []
    finally:
        store.close()


def test_render_brief_empty_returns_empty():
    assert render_fallback_brief([]) == ""


def test_render_brief_contains_source_marker():
    tw = {
        "id": "secrets_in_logs",
        "title": "Never log secrets",
        "severity": "critical",
        "cost_usd": 500.0,
        "body": "line1\nline2\nline3",
        "_fallback_score": 6.0,
    }
    out = render_fallback_brief([tw])
    assert 'source="keyword_fallback"' in out
    assert "secrets_in_logs" in out
    assert "CRITICAL" in out
    assert "match score 6.0" in out
    assert "[past cost $500.00]" in out


def test_fallback_on_cortex_meta_prompt_stays_silent(tmp_path):
    """A meta question about cortex itself should NOT fire any fallback."""
    store = _seeded_store(tmp_path)
    try:
        hits = fallback_search("did cortex fire", store)
    finally:
        store.close()
    # 'cortex' and 'fire' aren't in any tripwire triggers/title/body
    # (or are below min_score)
    assert hits == []
