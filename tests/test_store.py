import tempfile
from pathlib import Path

import pytest

from cortex.store import CortexStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        s = CortexStore(db)
        try:
            yield s
        finally:
            s.close()


def test_init_creates_empty_store(store):
    stats = store.stats()
    assert stats["total_tripwires"] == 0
    assert stats["total_violations"] == 0
    assert stats["by_severity"] == {}


def test_add_and_get_tripwire(store):
    store.add_tripwire(
        id="test_tw",
        title="Test tripwire",
        severity="high",
        domain="test",
        triggers=["a", "b"],
        body="body",
        cost_usd=1.5,
    )
    tw = store.get_tripwire("test_tw")
    assert tw is not None
    assert tw["title"] == "Test tripwire"
    assert tw["triggers"] == ["a", "b"]
    assert tw["cost_usd"] == 1.5
    assert tw["violation_count"] == 0
    assert tw["born_at"]


def test_missing_tripwire_returns_none(store):
    assert store.get_tripwire("nonexistent") is None


def test_upsert_preserves_violation_stats(store):
    store.add_tripwire(
        id="t1", title="v1", severity="low", domain="d",
        triggers=["x"], body="b1",
    )
    store.record_violation(tripwire_id="t1", session_id="s1", evidence="e1")
    store.record_violation(tripwire_id="t1", session_id="s2", evidence="e2")
    store.add_tripwire(
        id="t1", title="v2", severity="low", domain="d",
        triggers=["x"], body="b2",
    )
    tw = store.get_tripwire("t1")
    assert tw["title"] == "v2"
    assert tw["body"] == "b2"
    assert tw["violation_count"] == 2
    assert tw["last_violated_at"] is not None


def test_find_by_triggers_case_insensitive(store):
    store.add_tripwire(
        id="a", title="A", severity="high", domain="d",
        triggers=["Poly", "Fee"], body="x",
    )
    store.add_tripwire(
        id="b", title="B", severity="high", domain="d",
        triggers=["gold", "atr"], body="x",
    )
    hits = store.find_by_triggers(["poly", "backtest"])
    assert len(hits) == 1
    assert hits[0]["id"] == "a"


def test_list_filters(store):
    store.add_tripwire(id="a", title="A", severity="critical", domain="poly",
                       triggers=["x"], body="x")
    store.add_tripwire(id="b", title="B", severity="low", domain="poly",
                       triggers=["x"], body="x")
    assert len(store.list_tripwires(severity="critical")) == 1
    assert len(store.list_tripwires(severity="low")) == 1
    assert len(store.list_tripwires(domain="poly")) == 2


def test_list_orders_by_severity(store):
    store.add_tripwire(id="z_low", title="Z", severity="low", domain="d",
                       triggers=["x"], body="x")
    store.add_tripwire(id="a_crit", title="A", severity="critical", domain="d",
                       triggers=["x"], body="x")
    rows = store.list_tripwires()
    assert [r["id"] for r in rows] == ["a_crit", "z_low"]


def test_record_violation_updates_counts(store):
    store.add_tripwire(id="a", title="A", severity="high", domain="d",
                       triggers=["x"], body="x", cost_usd=10.0)
    vid = store.record_violation(tripwire_id="a", evidence="e1")
    assert vid > 0
    tw = store.get_tripwire("a")
    assert tw["violation_count"] == 1
    violations = store.list_violations(tripwire_id="a")
    assert len(violations) == 1
    assert violations[0]["evidence"] == "e1"


def test_stats_summary(store):
    store.add_tripwire(id="a", title="A", severity="critical", domain="poly",
                       triggers=["x"], body="x", cost_usd=10.0)
    store.add_tripwire(id="b", title="B", severity="high", domain="generic",
                       triggers=["x"], body="x", cost_usd=5.0)
    store.record_violation(tripwire_id="a")
    s = store.stats()
    assert s["total_tripwires"] == 2
    assert s["total_violations"] == 1
    assert s["by_severity"]["critical"]["n"] == 1
    assert s["by_severity"]["critical"]["cost"] == 10.0
    assert s["by_domain"]["poly"]["n"] == 1


def test_cost_components(store):
    store.add_tripwire(id="a", title="A", severity="high", domain="poly",
                       triggers=["x"], body="x")
    store.add_cost_component(
        id="a_spread", tripwire_id="a", metric="edge_pp",
        value=2.4, unit="pp", sign="drag",
    )
    rows = store.list_cost_components("a")
    assert len(rows) == 1
    assert rows[0]["value"] == 2.4


def test_delete_cascades_to_cost_components(store):
    store.add_tripwire(id="a", title="A", severity="high", domain="poly",
                       triggers=["x"], body="x")
    store.add_cost_component(
        id="a_spread", tripwire_id="a", metric="edge_pp",
        value=2.4, unit="pp", sign="drag",
    )
    assert store.delete_tripwire("a") is True
    assert store.list_cost_components("a") == []


def test_delete_nonexistent_returns_false(store):
    assert store.delete_tripwire("nope") is False
