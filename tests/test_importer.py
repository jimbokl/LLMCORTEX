import tempfile
from pathlib import Path

from cortex.importers.memory_md import SEED_TRIPWIRES, run_migration
from cortex.store import CortexStore


def test_seed_has_no_duplicate_ids():
    ids = [tw["id"] for tw in SEED_TRIPWIRES]
    assert len(ids) == len(set(ids)), "duplicate tripwire ids in SEED_TRIPWIRES"


def test_seed_has_required_tripwires():
    ids = {tw["id"] for tw in SEED_TRIPWIRES}
    required = {
        "poly_fee_empirical",
        "lookahead_parquet",
        "directional_5m_dead",
        "real_entry_price",
        "never_single_strategy",
        "backtest_must_match_prod",
    }
    assert required.issubset(ids)


def test_seed_fields_are_valid():
    valid_severities = {"critical", "high", "medium", "low"}
    for tw in SEED_TRIPWIRES:
        assert tw["severity"] in valid_severities, f"{tw['id']}: bad severity"
        assert isinstance(tw["triggers"], list)
        assert len(tw["triggers"]) >= 2, f"{tw['id']}: needs >=2 triggers"
        assert tw["body"], f"{tw['id']}: empty body"
        assert tw["domain"], f"{tw['id']}: empty domain"
        assert tw["title"], f"{tw['id']}: empty title"


def test_poly_fee_has_verify_cmd():
    fee_tw = next(tw for tw in SEED_TRIPWIRES if tw["id"] == "poly_fee_empirical")
    assert fee_tw["verify_cmd"] is not None
    assert fee_tw["severity"] == "critical"


def test_run_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        n1 = run_migration(db)
        n2 = run_migration(db)
        assert n1 == n2 == len(SEED_TRIPWIRES)
        store = CortexStore(db)
        try:
            assert store.stats()["total_tripwires"] == len(SEED_TRIPWIRES)
        finally:
            store.close()


def test_migration_preserves_violations_on_rerun():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        store = CortexStore(db)
        try:
            store.record_violation(tripwire_id="poly_fee_empirical", evidence="test")
        finally:
            store.close()
        run_migration(db)  # re-migrate
        store = CortexStore(db)
        try:
            tw = store.get_tripwire("poly_fee_empirical")
            assert tw["violation_count"] == 1
        finally:
            store.close()


def test_find_by_triggers_on_seeded_store():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        store = CortexStore(db)
        try:
            hits = store.find_by_triggers(["poly", "fee"])
            ids = {h["id"] for h in hits}
            assert "poly_fee_empirical" in ids
        finally:
            store.close()
