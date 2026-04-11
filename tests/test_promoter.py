"""Day 16 -- DMN promoter tests.

Covers the classification layer (parser, prompt builder, classify_pair
with injected call_fn), the pure decider (promotion/demotion rule
matrix), and the applier (daily caps + dry-run + session audit).
Integration tests against a real store live in
`test_promoter_integration.py`.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import promoter
from cortex.promoter import PromoterDecision
from cortex.promoter_prompt import build_classification_prompt
from cortex.store import CortexStore


# ---- Decider fixtures ----


_FIXED_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def _tripwire(
    id: str,
    status: str = "active",
    born_days_ago: int = 30,
) -> dict:
    born = _FIXED_NOW - timedelta(days=born_days_ago)
    return {
        "id": id,
        "status": status,
        "born_at": born.isoformat(timespec="seconds"),
        "title": id,
        "severity": "high",
        "domain": "test",
        "cost_usd": 0.0,
    }


def _fit(
    hits: int = 10,
    caught: int | None = None,
    ignored: int = 0,
    surprise_ok: float = 0.0,
    fitness: float | None = None,
) -> dict:
    if caught is None:
        caught = hits - ignored
    if fitness is None:
        fitness = float(caught) + 0.5 * surprise_ok - 2.0 * ignored
    return {
        "hits": hits,
        "caught": caught,
        "ignored": ignored,
        "surprise_ok": surprise_ok,
        "frustration": 0,
        "cost_weight": 0.0,
        "fitness": fitness,
    }


def _history_shadow_since(hours_ago: int) -> list[dict]:
    """Synthetic status_changes row: this tripwire was moved to shadow
    `hours_ago` hours ago."""
    at = (_FIXED_NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return [
        {
            "id": 1,
            "tripwire_id": "t",
            "from_status": "active",
            "to_status": "shadow",
            "reason": "seed",
            "metadata": {},
            "metadata_json": None,
            "at": at,
            "session_id": None,
        }
    ]


# ---- parse_classification ----


def test_parse_classification_valid_match():
    text = (
        '{"label": "match", "confidence": 0.92, '
        '"reasoning": "tool output aligned with predicted outcome"}'
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "match"
    assert p["confidence"] == 0.92
    assert "aligned" in p["reasoning"]


def test_parse_classification_valid_mismatch():
    text = (
        '{"label": "mismatch", "confidence": 0.77, '
        '"reasoning": "predicted failure_mode occurred"}'
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "mismatch"
    assert p["confidence"] == 0.77


def test_parse_classification_strips_code_fence():
    text = (
        "```json\n"
        '{"label": "partial", "confidence": 0.4, "reasoning": "ambiguous"}'
        "\n```"
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "partial"
    assert p["confidence"] == 0.4


def test_parse_classification_clamps_confidence_above_one():
    text = '{"label": "match", "confidence": 1.5, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["confidence"] == 1.0


def test_parse_classification_clamps_confidence_below_zero():
    text = '{"label": "match", "confidence": -0.3, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["confidence"] == 0.0


def test_parse_classification_error_label_on_bad_json():
    p = promoter.parse_classification("not actually json")
    assert p["label"] == "error"
    assert p["confidence"] == 0.0


def test_parse_classification_error_label_on_unknown_label():
    text = '{"label": "bogus", "confidence": 0.5}'
    p = promoter.parse_classification(text)
    assert p["label"] == "error"


def test_parse_classification_error_label_on_empty_input():
    assert promoter.parse_classification("").get("label") == "error"
    assert promoter.parse_classification(None).get("label") == "error"  # type: ignore[arg-type]


def test_parse_classification_missing_confidence_uses_default():
    text = '{"label": "match", "reasoning": "no conf given"}'
    p = promoter.parse_classification(text)
    assert p["label"] == "match"
    assert p["confidence"] == 0.5


def test_parse_classification_reasoning_truncated_to_300():
    long_reason = "x" * 500
    text = f'{{"label": "match", "confidence": 0.8, "reasoning": "{long_reason}"}}'
    p = promoter.parse_classification(text)
    assert len(p["reasoning"]) == 300


def test_parse_classification_tolerates_prose_around_json():
    text = (
        "Here is my classification:\n"
        '{"label": "mismatch", "confidence": 0.9, "reasoning": "tool crashed"}'
        "\nLet me know if you need more context."
    )
    p = promoter.parse_classification(text)
    assert p["label"] == "mismatch"


def test_parse_classification_case_insensitive_label():
    text = '{"label": "MATCH", "confidence": 0.8, "reasoning": ""}'
    p = promoter.parse_classification(text)
    assert p["label"] == "match"


# ---- build_classification_prompt ----


def test_build_prompt_contains_all_pair_fields():
    pair = {
        "session_id": "sess1",
        "at": "2026-04-11T12:00:00+00:00",
        "outcome": "test suite passes cleanly",
        "failure_mode": "slot_ts floor bug reappears",
        "tool_name": "Bash",
        "tool_snippet": "pytest -q",
        "tool_response": "332 passed in 5.76s",
        "tripwire_ids": ["lookahead_parquet"],
    }
    prompt = build_classification_prompt(pair)
    assert "test suite passes cleanly" in prompt
    assert "slot_ts floor bug reappears" in prompt
    assert "Bash" in prompt
    assert "pytest -q" in prompt
    assert "332 passed" in prompt
    # Classification enum + the conservative rule must be present.
    assert "mismatch" in prompt
    assert "when in doubt, choose \"partial\"" in prompt


def test_build_prompt_handles_missing_fields():
    pair = {"outcome": "", "failure_mode": "", "tool_name": None}
    prompt = build_classification_prompt(pair)
    assert "(none recorded)" in prompt
    assert "(no tool call)" in prompt
    assert "(no output)" in prompt


def test_build_prompt_truncates_long_fields():
    pair = {
        "outcome": "x" * 5000,
        "failure_mode": "y" * 5000,
        "tool_name": "Bash",
        "tool_snippet": "z" * 5000,
        "tool_response": "w" * 5000,
    }
    prompt = build_classification_prompt(pair)
    # 1200-char cap per field plus template overhead; nowhere near 5000.
    assert len(prompt) < 8000


# ---- classify_pair with injected call_fn ----


def test_classify_pair_uses_injected_call_fn():
    captured: dict[str, object] = {}

    def fake_call(prompt: str, model: str, max_tokens: int, client) -> str:
        captured["prompt"] = prompt
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return '{"label": "mismatch", "confidence": 0.88, "reasoning": "diverged"}'

    pair = {
        "outcome": "passes",
        "failure_mode": "crashes",
        "tool_name": "Bash",
        "tool_snippet": "ls",
        "tool_response": "error",
    }
    result = promoter.classify_pair(pair, call_fn=fake_call)
    assert result["label"] == "mismatch"
    assert result["confidence"] == 0.88
    assert result["model"] == promoter.DEFAULT_MODEL
    assert result["prompt_tokens"] > 0
    # Prompt was built and passed through.
    assert "passes" in captured["prompt"]
    assert "crashes" in captured["prompt"]


def test_classify_pair_override_model():
    def fake_call(prompt, model, max_tokens, client):
        return '{"label": "match", "confidence": 0.9, "reasoning": ""}'

    result = promoter.classify_pair(
        {"outcome": "ok", "failure_mode": "", "tool_name": "Read"},
        call_fn=fake_call,
        model="claude-sonnet-4-6",
    )
    assert result["model"] == "claude-sonnet-4-6"


def test_classify_pair_error_on_parser_failure():
    def fake_call(prompt, model, max_tokens, client):
        return "this is not json at all"

    result = promoter.classify_pair(
        {"outcome": "x", "failure_mode": "y"},
        call_fn=fake_call,
    )
    assert result["label"] == "error"
    assert result["model"] == promoter.DEFAULT_MODEL


def test_classify_pair_handles_call_fn_exception():
    def failing_call(prompt, model, max_tokens, client):
        raise RuntimeError("network down")

    result = promoter.classify_pair(
        {"outcome": "x", "failure_mode": "y"},
        call_fn=failing_call,
    )
    assert result["label"] == "error"
    assert "RuntimeError" in result["reasoning"]


# ---- decide() primary promotion gate ----


def test_decide_promotion_happy_path():
    tw = _tripwire("tw_hot", status="shadow", born_days_ago=30)
    tw_hist = {"tw_hot": _history_shadow_since(hours_ago=200)}
    fit = {"tw_hot": _fit(hits=12, caught=11, ignored=1, fitness=9.0)}
    distinct = {"tw_hot": 4}
    mismatches = {"tw_hot": 3}

    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions=distinct,
        mismatches=mismatches,
        status_history=tw_hist,
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.from_status == "shadow"
    assert d.to_status == "active"
    assert d.reason == "primary_gate"
    assert d.metadata["mismatches"] == 3


def test_decide_no_promotion_insufficient_tenure():
    tw = _tripwire("tw_fresh", status="shadow")
    # 6 days in shadow = below the 168h minimum tenure.
    hist = {"tw_fresh": _history_shadow_since(hours_ago=6 * 24)}
    fit = {"tw_fresh": _fit(hits=20, fitness=20.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_fresh": 10},
        mismatches={"tw_fresh": 5},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert decisions == []


def test_decide_no_promotion_insufficient_sessions():
    tw = _tripwire("tw_loop", status="shadow")
    hist = {"tw_loop": _history_shadow_since(hours_ago=200)}
    # 20 hits but only 1 session -- smells like a replay loop.
    fit = {"tw_loop": _fit(hits=20, fitness=20.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_loop": 1},
        mismatches={"tw_loop": 5},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert decisions == []


def test_decide_no_promotion_without_mismatches_and_below_fallback():
    tw = _tripwire("tw_quiet", status="shadow")
    hist = {"tw_quiet": _history_shadow_since(hours_ago=200)}
    # Meets primary gate on everything except mismatches; fitness is
    # below the fallback floor so neither path fires.
    fit = {"tw_quiet": _fit(hits=6, fitness=6.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_quiet": 4},
        mismatches={"tw_quiet": 0},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert decisions == []


def test_decide_fallback_clause_high_fitness():
    """Classification-free promotion when fitness is strong enough."""
    tw = _tripwire("tw_struct", status="shadow")
    hist = {"tw_struct": _history_shadow_since(hours_ago=200)}
    fit = {"tw_struct": _fit(hits=12, caught=12, ignored=0, fitness=12.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_struct": 6},
        mismatches={"tw_struct": 0},  # zero Haiku labels, yet fallback fires
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    assert decisions[0].reason == "fallback_gate"
    assert decisions[0].to_status == "active"


def test_decide_fallback_requires_higher_session_count():
    tw = _tripwire("tw_fb_fail", status="shadow")
    hist = {"tw_fb_fail": _history_shadow_since(hours_ago=200)}
    fit = {"tw_fb_fail": _fit(hits=12, caught=12, fitness=12.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_fb_fail": 4},  # below FALLBACK_DISTINCT_SESSIONS=5
        mismatches={"tw_fb_fail": 0},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert decisions == []


# ---- decide() demotion rules ----


def test_decide_demotion_ignored_rate_active_to_shadow():
    tw = _tripwire("tw_ignored", status="active", born_days_ago=20)
    fit = {"tw_ignored": _fit(hits=10, caught=4, ignored=6, fitness=-8.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_ignored": 3},
        mismatches={"tw_ignored": 0},
        status_history={},
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.from_status == "active"
    assert d.to_status == "shadow"
    assert d.reason == "active_ignored_rate"


def test_decide_demotion_negative_fitness():
    tw = _tripwire("tw_neg", status="active", born_days_ago=30)
    # fitness=-1.5, tenure long enough, no ignored-rate trigger.
    fit = {"tw_neg": _fit(hits=2, caught=1, ignored=1, fitness=-1.5)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_neg": 1},
        mismatches={"tw_neg": 0},
        status_history={},
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    assert decisions[0].reason == "negative_fitness"
    assert decisions[0].to_status == "shadow"


def test_decide_demotion_dormant():
    tw = _tripwire("tw_quiet", status="active", born_days_ago=45)
    fit = {"tw_quiet": _fit(hits=0, caught=0, ignored=0, fitness=0.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_quiet": 0},
        mismatches={"tw_quiet": 0},
        status_history={},
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    assert decisions[0].reason == "dormant"
    assert decisions[0].to_status == "shadow"


def test_decide_archive_after_shadow_tenure():
    tw = _tripwire("tw_stale", status="shadow", born_days_ago=60)
    # 15 days in shadow = above SHADOW_TENURE_HOURS_FOR_ARCHIVE=336h.
    hist = {"tw_stale": _history_shadow_since(hours_ago=15 * 24)}
    fit = {"tw_stale": _fit(hits=2, caught=2, fitness=2.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_stale": 1},
        mismatches={"tw_stale": 0},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    assert decisions[0].from_status == "shadow"
    assert decisions[0].to_status == "archived"
    assert decisions[0].reason == "shadow_tenure_expired"


def test_decide_shadow_ignored_rate_archives():
    tw = _tripwire("tw_rejected", status="shadow")
    # Shadow rule being ignored 90% of the time → straight to archive.
    fit = {"tw_rejected": _fit(hits=10, caught=1, ignored=9, fitness=-17.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_rejected": 4},
        mismatches={"tw_rejected": 0},
        status_history={"tw_rejected": _history_shadow_since(hours_ago=200)},
        now=_FIXED_NOW,
    )
    assert len(decisions) == 1
    assert decisions[0].to_status == "archived"
    assert decisions[0].reason == "shadow_ignored_rate"


# ---- decide() cooldown ----


def test_decide_cooldown_blocks_flapping():
    tw = _tripwire("tw_flap", status="shadow")
    # Two recent changes → cooldown engaged.
    recent1 = (_FIXED_NOW - timedelta(hours=24)).isoformat(timespec="seconds")
    recent2 = (_FIXED_NOW - timedelta(hours=48)).isoformat(timespec="seconds")
    hist = {
        "tw_flap": [
            {"id": 2, "from_status": "active", "to_status": "shadow",
             "reason": "demote", "at": recent1, "metadata": {}},
            {"id": 1, "from_status": "shadow", "to_status": "active",
             "reason": "promote", "at": recent2, "metadata": {}},
        ]
    }
    fit = {"tw_flap": _fit(hits=12, fitness=12.0)}
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_flap": 6},
        mismatches={"tw_flap": 5},
        status_history=hist,
        now=_FIXED_NOW,
    )
    assert decisions == []


# ---- decide() clock injection ----


def test_decide_uses_module_clock_when_now_omitted(monkeypatch):
    tw = _tripwire("tw_clock", status="shadow")
    hist = {"tw_clock": _history_shadow_since(hours_ago=200)}
    fit = {"tw_clock": _fit(hits=8, fitness=8.0)}

    monkeypatch.setattr(promoter, "_now", lambda: _FIXED_NOW)
    decisions = promoter.decide(
        tripwires=[tw],
        fitness=fit,
        distinct_sessions={"tw_clock": 3},
        mismatches={"tw_clock": 2},
        status_history=hist,
        # now omitted -- module clock wins
    )
    assert len(decisions) == 1
    assert decisions[0].reason == "primary_gate"


# ---- apply_decisions() ----


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "promoter.db"
        s = CortexStore(db)
        try:
            yield s
        finally:
            s.close()


def _seed_tripwire(store, tw_id, status="active"):
    store.add_tripwire(
        id=tw_id, title=tw_id, severity="high", domain="test",
        triggers=["x"], body="body", cost_usd=0.0, status=status,
    )


def test_apply_decisions_dry_run_does_not_mutate(tmp_store):
    _seed_tripwire(tmp_store, "tw_dry", status="shadow")
    d = PromoterDecision(
        tripwire_id="tw_dry", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={"fitness": 9.0}, priority=10,
    )
    results = promoter.apply_decisions(
        tmp_store, [d], session_id="test", now=_FIXED_NOW, dry_run=True,
    )
    assert len(results) == 1
    assert results[0].applied is False
    assert results[0].skip_reason == "dry_run"
    # Store is untouched.
    assert tmp_store.get_tripwire("tw_dry")["status"] == "shadow"
    assert tmp_store.list_status_changes() == []


def test_apply_decisions_apply_mutates_and_audits(tmp_store, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_store.db_path.parent))
    _seed_tripwire(tmp_store, "tw_hot", status="shadow")
    d = PromoterDecision(
        tripwire_id="tw_hot", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={"fitness": 9.0, "hits": 12},
        priority=10,
    )
    results = promoter.apply_decisions(
        tmp_store, [d], session_id="promoter_run_1",
        now=_FIXED_NOW, dry_run=False,
    )
    assert len(results) == 1
    assert results[0].applied is True
    # Live status mutated.
    assert tmp_store.get_tripwire("tw_hot")["status"] == "active"
    # Audit row written.
    rows = tmp_store.list_status_changes(tripwire_id="tw_hot")
    assert len(rows) == 1
    assert rows[0]["to_status"] == "active"
    assert rows[0]["metadata"]["fitness"] == 9.0
    # Session log event emitted.
    from cortex.session import read_session

    events = read_session("promoter_run_1")
    assert any(
        e.get("event") == "status_change"
        and e.get("tripwire_id") == "tw_hot"
        and e.get("to") == "active"
        for e in events
    )


def test_apply_decisions_daily_cap_promotions(tmp_store, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_store.db_path.parent))
    _seed_tripwire(tmp_store, "tw_a", status="shadow")
    _seed_tripwire(tmp_store, "tw_b", status="shadow")
    d_a = PromoterDecision(
        tripwire_id="tw_a", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={"fitness": 20.0}, priority=10,
        fitness_delta_hint=20.0,
    )
    d_b = PromoterDecision(
        tripwire_id="tw_b", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={"fitness": 8.0}, priority=10,
        fitness_delta_hint=8.0,
    )
    results = promoter.apply_decisions(
        tmp_store, [d_a, d_b], session_id="run1",
        now=_FIXED_NOW, dry_run=False,
    )
    # Cap is 1 promotion per day. Higher-impact wins.
    applied = [r for r in results if r.applied]
    skipped = [r for r in results if not r.applied]
    assert len(applied) == 1
    assert applied[0].tripwire_id == "tw_a"
    assert len(skipped) == 1
    assert skipped[0].tripwire_id == "tw_b"
    assert skipped[0].skip_reason == "daily_cap_promotions"


def test_apply_decisions_noop_skipped(tmp_store, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_store.db_path.parent))
    # Tripwire already active, decision says "promote to active" -- noop.
    _seed_tripwire(tmp_store, "tw_already", status="active")
    d = PromoterDecision(
        tripwire_id="tw_already", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={}, priority=10,
    )
    results = promoter.apply_decisions(
        tmp_store, [d], session_id="run_nop",
        now=_FIXED_NOW, dry_run=False,
    )
    assert results[0].applied is False
    assert results[0].skip_reason == "noop_or_missing"


def test_apply_decisions_respects_today_existing_rows(tmp_store, monkeypatch):
    """If a promotion already happened today (direct DB row), the
    applier must short-circuit further promotions."""
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_store.db_path.parent))
    _seed_tripwire(tmp_store, "tw_new", status="shadow")
    # Pretend an earlier promotion happened 2 hours ago.
    tmp_store.record_status_change(
        tripwire_id="tw_new",
        from_status="shadow",
        to_status="active",
        reason="earlier_run",
        at=(_FIXED_NOW - timedelta(hours=2)).isoformat(timespec="seconds"),
    )
    d = PromoterDecision(
        tripwire_id="tw_new", from_status="shadow", to_status="active",
        reason="primary_gate", metadata={}, priority=10,
    )
    results = promoter.apply_decisions(
        tmp_store, [d], session_id="run_capped",
        now=_FIXED_NOW, dry_run=False,
    )
    assert results[0].applied is False
    assert results[0].skip_reason == "daily_cap_promotions"
