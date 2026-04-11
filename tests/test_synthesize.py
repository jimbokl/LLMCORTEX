from pathlib import Path

from cortex.classify import classify_prompt, render_brief
from cortex.importers.memory_md import run_migration
from cortex.store import CortexStore
from cortex.synthesize import synthesize


def _seeded_store(tmp_path: Path) -> str:
    db = str(tmp_path / "seed.db")
    run_migration(db)
    return db


def test_synthesize_empty_match_returns_empty(tmp_path):
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        assert synthesize([], store) == []
    finally:
        store.close()


def test_synthesize_fires_when_all_directional_tripwires_matched(tmp_path):
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        result = synthesize(
            ["directional_5m_dead", "information_decay_5m", "adverse_selection_maker"],
            store,
        )
    finally:
        store.close()
    assert len(result) == 1
    rule = result[0]
    assert rule["id"] == "pm_5m_directional_block"
    # 2.4 + 7.25 + 10.0 = 19.65
    assert abs(rule["total"] - 19.65) < 0.01
    assert rule["n_components"] == 3
    assert "pp" in rule["message"]


def test_synthesize_partial_match_fires_if_above_threshold(tmp_path):
    """10pp alone (adverse_selection_maker) > 5pp threshold -- still fires."""
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        result = synthesize(["adverse_selection_maker"], store)
    finally:
        store.close()
    assert len(result) == 1
    assert result[0]["total"] == 10.0
    assert result[0]["n_components"] == 1


def test_synthesize_below_threshold_does_not_fire(tmp_path):
    """2.4pp alone (spread from directional_5m_dead) < 5pp threshold."""
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        result = synthesize(["directional_5m_dead"], store)
    finally:
        store.close()
    assert result == []


def test_synthesize_unrelated_match_returns_empty(tmp_path):
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        # These tripwires have no cost_components
        result = synthesize(["never_single_strategy", "backtest_must_match_prod"], store)
    finally:
        store.close()
    assert result == []


def test_classify_prompt_exposes_synthesis(tmp_path):
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "predict direction on 5m poly slot for btc signal",
        db_path=db,
    )
    assert result.get("synthesis"), "synthesis should fire for directional 5m prompt"
    synth = result["synthesis"][0]
    assert synth["id"] == "pm_5m_directional_block"
    assert synth["total"] >= 15.0


def test_render_brief_includes_synthesis_section(tmp_path):
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "predict direction on 5m poly slot for btc signal",
        db_path=db,
    )
    brief = render_brief(result)
    assert "SYNTHESIS" in brief
    assert "pm_5m_directional_block" in brief
    assert "pp" in brief


def test_render_brief_omits_synthesis_when_none(tmp_path):
    """For a prompt that matches tripwires without cost_components, brief
    should not contain a SYNTHESIS section at all."""
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "should I deploy my new live bot for polymarket",
        db_path=db,
    )
    brief = render_brief(result)
    assert brief, "expected matches"
    # never_single_strategy etc. have no cost_components, so no synthesis
    assert "SYNTHESIS" not in brief
