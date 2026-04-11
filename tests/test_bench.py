"""Smoke tests for cortex/bench.py -- verify structure and shape, not
actual latency numbers (those depend on hardware)."""
from __future__ import annotations

from pathlib import Path

from cortex.bench import (
    TEST_PROMPTS,
    _brief_size_distribution,
    _measure,
    _session_log_stats,
    _storage_stats,
    _subsystem_latency,
    render_report,
    run_benchmarks,
)
from cortex.importers.memory_md import run_migration


def _seeded_db(tmp_path: Path) -> str:
    db = str(tmp_path / "seed.db")
    run_migration(db)
    return db


# ---- _measure ----


def test_measure_returns_percentiles():
    stats = _measure(lambda: 1 + 1, n=50)
    for key in ("p50", "p95", "p99", "max", "mean", "n"):
        assert key in stats
    assert stats["n"] == 50
    # p50 <= p95 <= p99 <= max (by construction)
    assert stats["p50"] <= stats["p95"]
    assert stats["p95"] <= stats["p99"]
    assert stats["p99"] <= stats["max"]


def test_measure_warmup_does_not_crash_on_small_n():
    stats = _measure(lambda: None, n=5)
    assert stats["n"] == 5


# ---- _storage_stats ----


def test_storage_stats_returns_fields(tmp_path):
    db = _seeded_db(tmp_path)
    st = _storage_stats(db)
    assert st["db_path"] == db
    assert st["db_size_bytes"] > 0
    assert st["db_size_kb"] > 0
    assert st["n_tripwires"] == 11
    assert st["n_cost_components"] == 3
    assert st["n_synthesis_rules"] == 1
    assert "by_severity" in st


# ---- _session_log_stats ----


def test_session_log_stats_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    st = _session_log_stats()
    assert "error" not in st
    assert st["n_files"] == 0
    assert st["total_size_bytes"] == 0


def test_session_log_stats_with_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_SESSIONS_DIR", str(tmp_path))
    (tmp_path / "a.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")
    (tmp_path / "b.jsonl").write_text('{"event":"y"}\n', encoding="utf-8")
    st = _session_log_stats()
    assert st["n_files"] == 2
    assert st["total_size_bytes"] > 0


# ---- _subsystem_latency ----


def test_subsystem_latency_has_all_components(tmp_path):
    db = _seeded_db(tmp_path)
    lat = _subsystem_latency(db, iterations=20)  # small N for fast test
    expected = {
        "tokenize", "classify_prompt", "fallback_search",
        "synthesize", "render_brief",
    }
    assert set(lat.keys()) == expected
    for stats in lat.values():
        assert stats["n"] == 20
        assert stats["p50"] >= 0


# ---- _brief_size_distribution ----


def test_brief_size_distribution_covers_all_prompts(tmp_path):
    db = _seeded_db(tmp_path)
    briefs = _brief_size_distribution(db)
    assert len(briefs) == len(TEST_PROMPTS)
    labels = {b["label"] for b in briefs}
    assert "trivial_irrelevant" in labels
    assert "medium_matching" in labels
    # Irrelevant prompts should produce empty briefs (0 chars)
    trivial = next(b for b in briefs if b["label"] == "trivial_irrelevant")
    assert trivial["chars"] == 0
    assert trivial["matched_tripwires"] == 0


def test_brief_size_distribution_matches_on_backtest_prompt(tmp_path):
    db = _seeded_db(tmp_path)
    briefs = _brief_size_distribution(db)
    medium = next(b for b in briefs if b["label"] == "medium_matching")
    assert medium["chars"] > 0
    assert medium["matched_tripwires"] > 0
    assert medium["tokens_est"] > 0


# ---- run_benchmarks ----


def test_run_benchmarks_structure(tmp_path):
    db = _seeded_db(tmp_path)
    report = run_benchmarks(
        db_path=db, iterations=20, skip_subprocess=True,
    )
    # Top-level sections
    assert "env" in report
    assert "storage" in report
    assert "session_logs" in report
    assert "latency_ms" in report
    assert "brief_sizes" in report
    assert "impact" in report
    # Sub-structure
    assert "python_version" in report["env"]
    assert report["storage"]["n_tripwires"] == 11
    assert len(report["brief_sizes"]) == len(TEST_PROMPTS)


def test_run_benchmarks_impact_analysis(tmp_path):
    db = _seeded_db(tmp_path)
    report = run_benchmarks(
        db_path=db, iterations=20, skip_subprocess=True,
    )
    impact = report["impact"]
    assert impact["avg_brief_tokens"] > 0
    assert impact["break_even_injections_per_prevented_mistake"] > 0


# ---- render_report ----


def test_render_report_produces_non_empty_text(tmp_path):
    db = _seeded_db(tmp_path)
    report = run_benchmarks(
        db_path=db, iterations=10, skip_subprocess=True,
    )
    text = render_report(report)
    assert "Cortex v" in text
    assert "benchmark report" in text
    assert "Storage footprint" in text
    assert "In-process subsystem latency" in text
    assert "Brief size per prompt" in text
    assert "Token impact analysis" in text
