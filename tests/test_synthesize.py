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


def test_synthesize_fires_when_all_deploy_tripwires_matched(tmp_path):
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        result = synthesize(
            [
                "feature_flag_default_off",
                "backtest_must_match_prod",
                "never_single_strategy",
            ],
            store,
        )
    finally:
        store.close()
    assert len(result) == 1
    rule = result[0]
    assert rule["id"] == "prod_deploy_unsafe"
    # 3.0 + 2.0 + 4.0 = 9.0
    assert abs(rule["total"] - 9.0) < 0.01
    assert rule["n_components"] == 3
    assert "pts" in rule["message"]


def test_synthesize_partial_match_fires_if_above_threshold(tmp_path):
    """One critical SPOF (4.0 pts) + one staging-drift (2.0) + flag (3.0) = 9 > 5.

    Sub-cases: a single 4.0 SPOF alone is below the 5.0 threshold, while
    two components summed (e.g. 4.0 + 3.0 = 7.0) cross it.
    """
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        # 4.0 + 3.0 = 7.0 > 5.0 — fires
        result = synthesize(
            ["never_single_strategy", "feature_flag_default_off"], store,
        )
    finally:
        store.close()
    assert len(result) == 1
    assert result[0]["total"] == 7.0
    assert result[0]["n_components"] == 2


def test_synthesize_below_threshold_does_not_fire(tmp_path):
    """A single 3.0 pts component (feature flag) is below the 5.0 threshold."""
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        result = synthesize(["feature_flag_default_off"], store)
    finally:
        store.close()
    assert result == []


def test_synthesize_unrelated_match_returns_empty(tmp_path):
    db = _seeded_store(tmp_path)
    store = CortexStore(db)
    try:
        # These tripwires have no cost_components
        result = synthesize(["secrets_in_logs", "migration_destructive"], store)
    finally:
        store.close()
    assert result == []


def test_classify_prompt_exposes_synthesis(tmp_path):
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "ship the new pricing release to production today",
        db_path=db,
    )
    assert result.get("synthesis"), "synthesis should fire on a prod-deploy prompt"
    synth = result["synthesis"][0]
    assert synth["id"] == "prod_deploy_unsafe"
    assert synth["total"] >= 5.0


def test_render_brief_includes_synthesis_section(tmp_path):
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "ship the new pricing release to production today",
        db_path=db,
    )
    brief = render_brief(result)
    assert "SYNTHESIS" in brief
    assert "prod_deploy_unsafe" in brief
    assert "pts" in brief


def test_render_brief_omits_synthesis_when_none(tmp_path):
    """For a prompt that matches tripwires without cost_components, the brief
    should not contain a SYNTHESIS section at all."""
    db = _seeded_store(tmp_path)
    result = classify_prompt(
        "remember to redact the auth token before logging",
        db_path=db,
    )
    brief = render_brief(result)
    assert brief, "expected matches"
    # secrets_in_logs / auth_token_in_url have no cost_components.
    assert "SYNTHESIS" not in brief
