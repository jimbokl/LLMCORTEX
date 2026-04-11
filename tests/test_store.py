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


# ---- Day 15: status column ----


def test_new_tripwire_defaults_to_active_status(store):
    store.add_tripwire(
        id="t1", title="x", severity="low", domain="d",
        triggers=["a"], body="b",
    )
    tw = store.get_tripwire("t1")
    assert tw["status"] == "active"


def test_add_with_shadow_status(store):
    store.add_tripwire(
        id="t_sh", title="x", severity="low", domain="d",
        triggers=["a"], body="b", status="shadow",
    )
    assert store.get_tripwire("t_sh")["status"] == "shadow"


def test_add_with_invalid_status_raises(store):
    with pytest.raises(ValueError):
        store.add_tripwire(
            id="t_bad", title="x", severity="low", domain="d",
            triggers=["a"], body="b", status="bogus",
        )


def test_list_tripwires_default_filters_to_active(store):
    store.add_tripwire(
        id="a", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    store.add_tripwire(
        id="s", title="S", severity="high", domain="d",
        triggers=["x"], body="b", status="shadow",
    )
    store.add_tripwire(
        id="z", title="Z", severity="high", domain="d",
        triggers=["x"], body="b", status="archived",
    )
    ids = [t["id"] for t in store.list_tripwires()]
    assert ids == ["a"]


def test_list_tripwires_all_statuses(store):
    store.add_tripwire(
        id="a", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    store.add_tripwire(
        id="s", title="S", severity="high", domain="d",
        triggers=["x"], body="b", status="shadow",
    )
    ids = {t["id"] for t in store.list_tripwires(status=None)}
    assert ids == {"a", "s"}
    shadow_ids = [t["id"] for t in store.list_tripwires(status="shadow")]
    assert shadow_ids == ["s"]


def test_set_status_transition(store):
    store.add_tripwire(
        id="t1", title="x", severity="low", domain="d",
        triggers=["a"], body="b", status="shadow",
    )
    assert store.set_status("t1", "active") is True
    assert store.get_tripwire("t1")["status"] == "active"


def test_set_status_invalid_raises(store):
    store.add_tripwire(
        id="t1", title="x", severity="low", domain="d",
        triggers=["a"], body="b",
    )
    with pytest.raises(ValueError):
        store.set_status("t1", "bogus")


def test_set_status_unknown_id_returns_false(store):
    assert store.set_status("nope", "shadow") is False


def test_upsert_preserves_status(store):
    """Re-running `cortex migrate` must not clobber a manual shadow
    decision on a tripwire that was promoted after the initial import."""
    store.add_tripwire(
        id="t1", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    store.set_status("t1", "shadow")
    # Second import of the same id with default status='active'.
    store.add_tripwire(
        id="t1", title="A changed", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    # Status stays shadow because ON CONFLICT DO UPDATE SET omits
    # the status column intentionally.
    assert store.get_tripwire("t1")["status"] == "shadow"
    assert store.get_tripwire("t1")["title"] == "A changed"


def test_migration_adds_status_column_to_legacy_store(tmp_path):
    """Simulate a pre-Day-15 store: drop the status column and reopen.

    The migration runner on `_init_schema` must re-add the column with
    default='active' so legacy rows stay visible to default list queries.
    """
    import sqlite3

    db = tmp_path / "legacy.db"
    store = CortexStore(db)
    store.add_tripwire(
        id="legacy_tw", title="old", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    store.close()

    # Manually drop the status column by rebuilding the table without it.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        ALTER TABLE tripwires RENAME TO tripwires_old;
        CREATE TABLE tripwires (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            severity TEXT NOT NULL, domain TEXT NOT NULL,
            triggers TEXT NOT NULL, body TEXT NOT NULL,
            verify_cmd TEXT, cost_usd REAL NOT NULL DEFAULT 0,
            born_at TEXT NOT NULL, last_violated_at TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            source_file TEXT, violation_patterns TEXT
        );
        INSERT INTO tripwires
          (id, title, severity, domain, triggers, body, verify_cmd,
           cost_usd, born_at, last_violated_at, violation_count,
           source_file, violation_patterns)
          SELECT id, title, severity, domain, triggers, body, verify_cmd,
                 cost_usd, born_at, last_violated_at, violation_count,
                 source_file, violation_patterns
          FROM tripwires_old;
        DROP TABLE tripwires_old;
        """
    )
    conn.commit()
    conn.close()

    # Reopen: migration must add the column back.
    store2 = CortexStore(db)
    try:
        tw = store2.get_tripwire("legacy_tw")
        assert tw is not None
        assert tw["status"] == "active"
        # Idempotency: opening a second time must not crash.
        store2.close()
        store3 = CortexStore(db)
        try:
            assert store3.get_tripwire("legacy_tw") is not None
        finally:
            store3.close()
    except Exception:
        store2.close()
        raise


# ---- Day 16: pair classifications ----


def test_upsert_pair_classification_inserts_new_row(store):
    ok = store.upsert_pair_classification(
        session_id="sess_a",
        at="2026-04-11T12:00:00+00:00",
        tripwire_ids=["tw1", "tw2"],
        label="mismatch",
        confidence=0.87,
        reasoning="the world surprised the agent",
        model="claude-haiku-4-5",
        classified_at="2026-04-11T12:01:00+00:00",
        cost_usd=0.0012,
    )
    assert ok is True
    got = store.get_pair_classification("sess_a", "2026-04-11T12:00:00+00:00")
    assert got is not None
    assert got["label"] == "mismatch"
    assert got["tripwire_ids"] == ["tw1", "tw2"]
    assert got["confidence"] == 0.87
    assert got["reasoning"] == "the world surprised the agent"
    assert got["model"] == "claude-haiku-4-5"
    assert got["cost_usd"] == 0.0012


def test_upsert_pair_classification_is_idempotent(store):
    kwargs = dict(
        session_id="sess_b",
        at="2026-04-11T12:00:00+00:00",
        tripwire_ids=["tw1"],
        label="match",
        confidence=0.9,
        reasoning="aligned",
        model="claude-haiku-4-5",
        classified_at="2026-04-11T12:01:00+00:00",
    )
    assert store.upsert_pair_classification(**kwargs) is True
    # Second call with the SAME key: INSERT OR IGNORE → rowcount 0.
    # Even if we change the label, the existing row must be preserved
    # so reruns never re-bill Haiku for a classified pair.
    kwargs["label"] = "mismatch"
    assert store.upsert_pair_classification(**kwargs) is False
    got = store.get_pair_classification("sess_b", "2026-04-11T12:00:00+00:00")
    assert got["label"] == "match"  # original label preserved


def test_upsert_pair_classification_rejects_bad_label(store):
    with pytest.raises(ValueError):
        store.upsert_pair_classification(
            session_id="sess_c",
            at="2026-04-11T12:00:00+00:00",
            tripwire_ids=["tw1"],
            label="bogus",  # not in enum
            confidence=0.5,
            reasoning="",
            model="claude-haiku-4-5",
            classified_at="2026-04-11T12:01:00+00:00",
        )


def test_upsert_pair_classification_clamps_confidence(store):
    store.upsert_pair_classification(
        session_id="sess_d",
        at="2026-04-11T12:00:00+00:00",
        tripwire_ids=[],
        label="partial",
        confidence=2.5,  # above 1.0
        reasoning="",
        model="m",
        classified_at="2026-04-11T12:01:00+00:00",
    )
    got = store.get_pair_classification("sess_d", "2026-04-11T12:00:00+00:00")
    assert got["confidence"] == 1.0


def test_upsert_pair_classification_truncates_reasoning(store):
    long_reason = "x" * 500
    store.upsert_pair_classification(
        session_id="sess_e",
        at="2026-04-11T12:00:00+00:00",
        tripwire_ids=[],
        label="partial",
        confidence=0.5,
        reasoning=long_reason,
        model="m",
        classified_at="2026-04-11T12:01:00+00:00",
    )
    got = store.get_pair_classification("sess_e", "2026-04-11T12:00:00+00:00")
    assert len(got["reasoning"]) == 300


def test_list_pair_classifications_since_filter(store):
    base = dict(
        tripwire_ids=["tw1"],
        label="mismatch",
        confidence=0.8,
        reasoning="",
        model="m",
    )
    store.upsert_pair_classification(
        session_id="s1", at="2026-04-10T00:00:00+00:00",
        classified_at="2026-04-10T00:01:00+00:00", **base,
    )
    store.upsert_pair_classification(
        session_id="s2", at="2026-04-11T00:00:00+00:00",
        classified_at="2026-04-11T00:01:00+00:00", **base,
    )
    all_rows = store.list_pair_classifications()
    assert len(all_rows) == 2
    # Filter by classified_at cutoff.
    recent = store.list_pair_classifications(since_iso="2026-04-11T00:00:00+00:00")
    assert len(recent) == 1
    assert recent[0]["session_id"] == "s2"


# ---- Day 16: status_changes audit ----


def test_record_status_change_without_mutation(store):
    store.add_tripwire(
        id="tw_audit", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    row_id = store.record_status_change(
        tripwire_id="tw_audit",
        from_status="active",
        to_status="shadow",
        reason="manual_test",
        metadata={"fitness": 5.5, "hits": 12},
        session_id="sess_x",
    )
    assert row_id > 0
    # record_status_change is audit-only; the tripwire status is
    # NOT mutated by this path.
    assert store.get_tripwire("tw_audit")["status"] == "active"
    rows = store.list_status_changes(tripwire_id="tw_audit")
    assert len(rows) == 1
    assert rows[0]["from_status"] == "active"
    assert rows[0]["to_status"] == "shadow"
    assert rows[0]["reason"] == "manual_test"
    assert rows[0]["metadata"] == {"fitness": 5.5, "hits": 12}
    assert rows[0]["session_id"] == "sess_x"


def test_apply_status_transition_writes_audit_and_mutates(store):
    store.add_tripwire(
        id="tw_app", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    result = store.apply_status_transition(
        tripwire_id="tw_app",
        to_status="shadow",
        reason="dmn_auto_demote",
        metadata={"fitness": -1.2},
        session_id="promoter_run_1",
    )
    assert result is not None
    assert result["from_status"] == "active"
    assert result["to_status"] == "shadow"
    assert result["reason"] == "dmn_auto_demote"
    # Live status field IS mutated.
    assert store.get_tripwire("tw_app")["status"] == "shadow"
    # Audit row landed in the same txn.
    audit = store.list_status_changes(tripwire_id="tw_app")
    assert len(audit) == 1
    assert audit[0]["to_status"] == "shadow"
    assert audit[0]["metadata"]["fitness"] == -1.2


def test_apply_status_transition_noop_when_same_status(store):
    store.add_tripwire(
        id="tw_nop", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    # Already active → no-op, no audit row, returns None.
    result = store.apply_status_transition(
        tripwire_id="tw_nop",
        to_status="active",
        reason="dmn_auto_promote",
    )
    assert result is None
    assert store.list_status_changes(tripwire_id="tw_nop") == []


def test_apply_status_transition_missing_tripwire_returns_none(store):
    result = store.apply_status_transition(
        tripwire_id="does_not_exist",
        to_status="shadow",
        reason="ghost",
    )
    assert result is None


def test_apply_status_transition_rejects_bad_status(store):
    store.add_tripwire(
        id="tw_bad", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    with pytest.raises(ValueError):
        store.apply_status_transition(
            tripwire_id="tw_bad",
            to_status="bogus",
            reason="x",
        )


def test_list_status_changes_ordering_and_since_filter(store):
    store.add_tripwire(
        id="tw_ord", title="A", severity="high", domain="d",
        triggers=["x"], body="b",
    )
    store.record_status_change(
        tripwire_id="tw_ord",
        from_status="active", to_status="shadow", reason="r1",
        at="2026-04-09T00:00:00+00:00",
    )
    store.record_status_change(
        tripwire_id="tw_ord",
        from_status="shadow", to_status="active", reason="r2",
        at="2026-04-11T00:00:00+00:00",
    )
    store.record_status_change(
        tripwire_id="tw_ord",
        from_status="active", to_status="shadow", reason="r3",
        at="2026-04-10T00:00:00+00:00",
    )
    rows = store.list_status_changes(tripwire_id="tw_ord")
    # Newest first
    assert [r["reason"] for r in rows] == ["r2", "r3", "r1"]
    # Since-filter
    recent = store.list_status_changes(
        tripwire_id="tw_ord",
        since_iso="2026-04-10T00:00:00+00:00",
    )
    assert [r["reason"] for r in recent] == ["r2", "r3"]


def test_schema_version_bumps_on_upgrade(tmp_path):
    """A store created with SCHEMA_VERSION=1 (simulated) must have its
    schema_version row updated to 2 when reopened."""
    import sqlite3

    db = tmp_path / "upgrade.db"
    # Fresh store first (writes version=2)
    store = CortexStore(db)
    store.close()

    # Manually stomp the version back to 1 to simulate a pre-Day-16 store.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE schema_version SET version = 1")
    conn.commit()
    conn.close()

    # Reopen → upgrade path must bump to 2.
    store2 = CortexStore(db)
    try:
        row = store2.conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        assert int(row["version"]) == 2
    finally:
        store2.close()
