import tempfile
from pathlib import Path

import yaml

from cortex.classify import (
    _match_rule,
    _tokenize,
    classify_prompt,
    find_db,
    render_brief,
)
from cortex.importers.memory_md import run_migration


def _write_test_rules(rules_dir: Path) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "test.yml").write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "r1",
                        "match_any": ["backtest", "replay"],
                        "and_any": ["poly", "slot"],
                        "inject": ["poly_fee_empirical", "lookahead_parquet"],
                    },
                    {
                        "id": "r2",
                        "match_any": ["live"],
                        "and_any": ["bot", "deploy"],
                        "inject": ["never_single_strategy"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_tokenize_splits_on_non_word():
    toks = _tokenize("Hello, 5m poly BACKTEST!")
    assert "5m" in toks
    assert "poly" in toks
    assert "backtest" in toks
    assert "hello" in toks


def test_match_rule_requires_both_sets():
    rule = {"match_any": ["backtest"], "and_any": ["poly"]}
    assert _match_rule(rule, {"backtest", "poly"}) is True
    assert _match_rule(rule, {"backtest"}) is False  # missing and_any
    assert _match_rule(rule, {"poly"}) is False      # missing match_any
    assert _match_rule(rule, {"hello"}) is False


def test_empty_rule_never_matches():
    assert _match_rule({}, {"anything"}) is False


def test_classify_no_rules_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        empty_rules = Path(tmp) / "empty_rules"
        empty_rules.mkdir()
        result = classify_prompt("backtest poly slot", db_path=db, rules_dir=empty_rules)
        assert result["tripwires"] == []
        assert result["matched_rules"] == []


def test_classify_matches_poly_backtest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        result = classify_prompt(
            "I want to run a backtest on 5m poly slot data",
            db_path=db,
            rules_dir=rules_dir,
        )
        assert "r1" in result["matched_rules"]
        ids = {t["id"] for t in result["tripwires"]}
        assert "poly_fee_empirical" in ids
        assert "lookahead_parquet" in ids


def test_classify_no_match_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        result = classify_prompt("hello world", db_path=db, rules_dir=rules_dir)
        assert result["tripwires"] == []
        assert result["matched_rules"] == []


def test_classify_sorts_tripwires_by_severity():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        result = classify_prompt("backtest poly slot", db_path=db, rules_dir=rules_dir)
        severities = [t["severity"] for t in result["tripwires"]]
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        assert severities == sorted(severities, key=sev_order.__getitem__)


def test_classify_truncates_to_max():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        # Rule that injects many tripwires
        rules_dir.mkdir()
        (rules_dir / "big.yml").write_text(
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "id": "big",
                            "match_any": ["all"],
                            "and_any": ["everything"],
                            "inject": [
                                "poly_fee_empirical",
                                "lookahead_parquet",
                                "directional_5m_dead",
                                "adverse_selection_maker",
                                "information_decay_5m",
                                "real_entry_price",
                                "never_single_strategy",
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = classify_prompt(
            "all everything", db_path=db, rules_dir=rules_dir, max_tripwires=3
        )
        assert len(result["tripwires"]) == 3
        assert result["truncated"] is True
        assert result["total_matches"] == 7


def test_render_brief_produces_tagged_block():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        result = classify_prompt("backtest poly slot", db_path=db, rules_dir=rules_dir)
        brief = render_brief(result)
        assert brief.startswith("<cortex_brief")
        assert brief.endswith("</cortex_brief>")
        assert "poly_fee_empirical" in brief
        assert "CRITICAL" in brief


def test_render_brief_empty_on_no_tripwires():
    assert render_brief({"tripwires": [], "matched_rules": [], "truncated": False}) == ""


def _make_result(tripwires):
    return {
        "matched_rules": ["r"],
        "tripwires": list(tripwires),
        "shadow_tripwires": [],
        "synthesis": [],
        "truncated": False,
        "total_matches": len(tripwires),
    }


def test_render_brief_default_budget_keeps_every_tripwire(monkeypatch):
    """The 6000-char default is above every brief observed in 13 days of
    production audit, so the clamp must be a no-op for realistic inputs.
    """
    monkeypatch.delenv("CORTEX_BRIEF_MAX_CHARS", raising=False)
    trips = [
        {"id": f"t{i}", "severity": "low", "cost_usd": 0.0,
         "title": f"Low {i}", "body": "Body " * 20}
        for i in range(4)
    ]
    brief = render_brief(_make_result(trips))
    # All four present, no truncation marker.
    assert all(f"[{i + 1}] t{i}" in brief for i in range(4))
    assert "cortex_brief: truncated" not in brief


def test_render_brief_truncates_low_severity_on_budget(monkeypatch):
    """Tight budget must drop low-severity entries before any critical
    one is touched. The surviving brief must carry the truncation marker
    listing the dropped ids so the agent sees what was pruned.
    """
    monkeypatch.setenv("CORTEX_BRIEF_MAX_CHARS", "800")
    trips = [
        {"id": "crit1", "severity": "critical", "cost_usd": 100.0,
         "title": "Critical lesson", "body": "Critical body\nLine 2"},
        {"id": "hi1", "severity": "high", "cost_usd": 50.0,
         "title": "High lesson", "body": "High body " * 20},
        {"id": "med1", "severity": "medium", "cost_usd": 0.0,
         "title": "Medium", "body": "Medium body " * 30},
        {"id": "low1", "severity": "low", "cost_usd": 0.0,
         "title": "Low lesson", "body": "Low body " * 30},
    ]
    brief = render_brief(_make_result(trips))
    # Critical is always preserved.
    assert "crit1" in brief
    # Marker names the dropped ids.
    assert "cortex_brief: truncated" in brief
    assert "low1" in brief  # listed in the marker even if dropped from body
    # The body section for low1 should be gone. We assert via the
    # "[N] low1" preamble pattern which appears only when the tripwire
    # is actually rendered in the list.
    assert "] low1" not in brief


def test_render_brief_never_truncates_critical_even_under_tiny_budget(monkeypatch):
    """Pathological budget: even at 50 chars the brief must keep every
    critical tripwire. The clamp stops as soon as only critical entries
    remain, trading over-budget for information preservation.
    """
    monkeypatch.setenv("CORTEX_BRIEF_MAX_CHARS", "50")
    trips = [
        {"id": "crit1", "severity": "critical", "cost_usd": 0.0,
         "title": "Crit one", "body": "Body one"},
        {"id": "crit2", "severity": "critical", "cost_usd": 0.0,
         "title": "Crit two", "body": "Body two"},
        {"id": "low1", "severity": "low", "cost_usd": 0.0,
         "title": "Low lesson", "body": "Low body " * 30},
    ]
    brief = render_brief(_make_result(trips))
    assert "crit1" in brief
    assert "crit2" in brief
    # Low is dropped, marker notes it.
    assert "] low1" not in brief
    assert "cortex_brief: truncated" in brief


def test_affected_files_match_injects_tripwire_even_with_irrelevant_prompt():
    """Tier 1.4: a prompt that shares zero keywords with any rule still
    surfaces the tripwire attached to one of the touched paths via its
    `affected_files` glob list.
    """
    from cortex.store import CortexStore

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        store = CortexStore(db)
        store.add_tripwire(
            id="fx_path", title="path-matched rule", severity="high",
            domain="generic", triggers=["bar"], body="body",
            affected_files=["cortex/classify.py", "*classify*"],
        )
        store.close()
        rules_dir = tmp_p / "rules"
        rules_dir.mkdir()
        (rules_dir / "empty.yml").write_text(
            yaml.safe_dump({"rules": []}), encoding="utf-8"
        )
        # The prompt has no keyword match — only the file path fires.
        result = classify_prompt(
            "cleanup random unrelated text",
            db_path=db,
            rules_dir=rules_dir,
            touched_files=["cortex/classify.py"],
        )
        ids = {t["id"] for t in result["tripwires"]}
        assert "fx_path" in ids
        assert "fx_path" in result["touched_files_matched"]


def test_affected_files_deduplicate_against_keyword_match():
    """A tripwire that already matched via keywords must not be counted
    twice when the touched_files second pass would also match it.
    """
    from cortex.store import CortexStore

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        store = CortexStore(db)
        store.add_tripwire(
            id="fx_both", title="both", severity="high", domain="generic",
            triggers=["foo"], body="body",
            affected_files=["cortex/classify.py"],
        )
        store.close()
        rules_dir = tmp_p / "rules"
        rules_dir.mkdir()
        (rules_dir / "rules.yml").write_text(
            yaml.safe_dump(
                {
                    "rules": [{
                        "id": "r",
                        "match_any": ["foo"],
                        "and_any": ["bar"],
                        "inject": ["fx_both"],
                    }]
                }
            ),
            encoding="utf-8",
        )
        result = classify_prompt(
            "foo bar hello", db_path=db, rules_dir=rules_dir,
            touched_files=["cortex/classify.py"],
        )
        ids = [t["id"] for t in result["tripwires"]]
        assert ids.count("fx_both") == 1
        # Not reported in touched_files_matched because it was already
        # matched by keywords first.
        assert "fx_both" not in result["touched_files_matched"]


def test_affected_files_empty_list_is_noop():
    """Backwards-compatibility guard: a tripwire without
    `affected_files` or a classify call without `touched_files` must
    behave exactly as pre-Tier-1.4.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        # No touched_files passed at all.
        r1 = classify_prompt(
            "backtest poly slot", db_path=db, rules_dir=rules_dir
        )
        # Empty list is equivalent.
        r2 = classify_prompt(
            "backtest poly slot", db_path=db, rules_dir=rules_dir,
            touched_files=[],
        )
        assert {t["id"] for t in r1["tripwires"]} == {t["id"] for t in r2["tripwires"]}
        assert r1["touched_files_matched"] == []
        assert r2["touched_files_matched"] == []


def test_render_brief_budget_env_non_integer_falls_back_to_default(monkeypatch):
    """A misconfigured env var must never silence the brief. Garbage
    input resolves back to DEFAULT_BRIEF_MAX_CHARS, which at 6000 is
    generous enough that nothing is dropped for the canonical four-item
    result used elsewhere in this test file.
    """
    monkeypatch.setenv("CORTEX_BRIEF_MAX_CHARS", "not-a-number")
    trips = [
        {"id": "crit1", "severity": "critical", "cost_usd": 0.0,
         "title": "Crit", "body": "Body"},
        {"id": "low1", "severity": "low", "cost_usd": 0.0,
         "title": "Low", "body": "Body"},
    ]
    brief = render_brief(_make_result(trips))
    assert "crit1" in brief
    assert "] low1" in brief
    assert "cortex_brief: truncated" not in brief


def test_real_rules_fire_on_replay_basis_arb():
    """Smoke test using the shipped rules: a real-world prompt that caused
    the whole project should match `poly_backtest_task` and inject the
    critical fee/lookahead/entry-price tripwires."""
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "seed.db")
        run_migration(db)
        result = classify_prompt(
            "run replay_basis_arb.py to backtest Binance lead on 5m poly slots",
            db_path=db,
        )
        assert result["tripwires"], "real rules should fire for this prompt"
        ids = {t["id"] for t in result["tripwires"]}
        assert "poly_fee_empirical" in ids
        assert "poly_backtest_task" in result["matched_rules"]


def test_render_brief_appends_predict_block_when_critical():
    """Day 14: render_brief must append the <cortex_predict> instructions
    when at least one critical tripwire is present in the result."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        rules_dir = tmp_p / "rules"
        _write_test_rules(rules_dir)
        result = classify_prompt("backtest poly slot", db_path=db, rules_dir=rules_dir)
        brief = render_brief(result)
        assert "CRITICAL TASK DETECTED" in brief
        assert "<cortex_predict>" in brief
        assert "<outcome>" in brief
        assert "<failure_mode>" in brief
        assert "</cortex_predict>" in brief


def test_classify_splits_active_and_shadow():
    """Day 15: a rule that injects both an active and a shadow tripwire
    must produce them on separate lists. Synthesis runs over active only."""
    import sqlite3

    from cortex.store import CortexStore
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        store = CortexStore(db)
        store.add_tripwire(
            id="tw_active", title="active one", severity="critical",
            domain="test", triggers=["x"], body="active body",
        )
        store.add_tripwire(
            id="tw_shadow", title="shadow one", severity="high",
            domain="test", triggers=["x"], body="shadow body",
            status="shadow",
        )
        store.add_tripwire(
            id="tw_archived", title="archived one", severity="high",
            domain="test", triggers=["x"], body="archived body",
            status="archived",
        )
        store.close()

        rules_dir = tmp_p / "rules"
        rules_dir.mkdir()
        (rules_dir / "test.yml").write_text(
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "id": "r_all",
                            "match_any": ["trigger"],
                            "and_any": ["test"],
                            "inject": ["tw_active", "tw_shadow", "tw_archived"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = classify_prompt(
            "trigger test", db_path=db, rules_dir=rules_dir,
        )
        active_ids = [t["id"] for t in result["tripwires"]]
        shadow_ids = [t["id"] for t in result["shadow_tripwires"]]
        assert active_ids == ["tw_active"]
        assert shadow_ids == ["tw_shadow"]
        # Archived must be hidden from BOTH lists.
        assert "tw_archived" not in active_ids
        assert "tw_archived" not in shadow_ids


def test_render_brief_never_includes_shadow_tripwires():
    """Even if shadow rules matched, the rendered brief must only show
    active ones — shadow is audit-only by contract."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        from cortex.store import CortexStore
        store = CortexStore(db)
        store.add_tripwire(
            id="live", title="live title", severity="critical",
            domain="test", triggers=["x"], body="live body",
        )
        store.add_tripwire(
            id="probation", title="PROBATION_RULE", severity="critical",
            domain="test", triggers=["x"], body="shadow body PROBATION_BODY",
            status="shadow",
        )
        store.close()
        rules_dir = tmp_p / "rules"
        rules_dir.mkdir()
        (rules_dir / "r.yml").write_text(
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "id": "r",
                            "match_any": ["trigger"],
                            "and_any": ["test"],
                            "inject": ["live", "probation"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = classify_prompt(
            "trigger test", db_path=db, rules_dir=rules_dir,
        )
        brief = render_brief(result)
        assert "live title" in brief
        assert "PROBATION_RULE" not in brief
        assert "PROBATION_BODY" not in brief


def test_render_brief_omits_predict_block_when_no_critical():
    """Only critical tripwires trigger the Surprise Engine request."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        db = str(tmp_p / "seed.db")
        run_migration(db)
        # Fabricate a result with only a medium-severity tripwire (no critical).
        result = {
            "matched_rules": ["r_test"],
            "tripwires": [
                {
                    "id": "fake_medium",
                    "title": "fake title",
                    "severity": "medium",
                    "body": "fake body",
                    "cost_usd": 0.0,
                }
            ],
            "synthesis": [],
            "truncated": False,
            "total_matches": 1,
        }
        brief = render_brief(result)
        assert "<cortex_predict>" not in brief
        assert "CRITICAL TASK DETECTED" not in brief


def test_find_db_honors_env_var(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("CORTEX_DB", custom)
    assert find_db() == custom


def test_find_db_walks_up(tmp_path, monkeypatch):
    # Create a fake project tree: tmp_path/.cortex/store.db and tmp_path/sub/
    (tmp_path / ".cortex").mkdir()
    (tmp_path / ".cortex" / "store.db").write_bytes(b"")
    sub = tmp_path / "sub" / "deeper"
    sub.mkdir(parents=True)
    monkeypatch.delenv("CORTEX_DB", raising=False)
    found = find_db(start=sub)
    assert Path(found) == tmp_path / ".cortex" / "store.db"
