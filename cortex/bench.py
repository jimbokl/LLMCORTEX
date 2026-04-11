"""Cortex benchmarks — measure subsystem latency, storage footprint,
brief size distribution, and end-to-end hook cost.

Run via `cortex bench [--iterations N] [--json]`.

This module answers three questions:

1. **How fast is Cortex?** Per-subsystem p50/p95/p99/max over N iterations,
   plus end-to-end hook latency measured by spawning fresh subprocess
   invocations (the real-world cost model: Claude Code runs cortex-hook
   as a fresh process per prompt).

2. **How big is the brief?** Character count per prompt across a canned
   test set covering trivial / short / long / Russian / matching /
   non-matching prompts. Translated to rough token count (chars / 4).

3. **Does it pay off?** Break-even analysis: an injection costs
   ~N tokens of context; a prevented mistake saves ~3000 tokens
   (one wasted tool-call cycle). The math shows how often injections
   need to prevent mistakes for Cortex to be net positive.

No external deps. No tiktoken (char/4 estimate noted as approximate).
No psutil (RSS measurement skipped — noted in output).
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_TOKEN_CHARS = 4  # rough estimate: 1 token ≈ 4 chars for English text
_ASSUMED_PREVENTED_MISTAKE_TOKENS = 3000  # one wasted tool-call cycle


# Canned test prompts covering realistic usage scenarios
TEST_PROMPTS: list[tuple[str, str]] = [
    ("trivial_irrelevant", "hi"),
    ("short_irrelevant", "what time is it"),
    ("short_matching", "poly backtest"),
    ("medium_matching", "run a 5m poly directional backtest on btc"),
    (
        "long_matching",
        "I want to test a late-lock strategy on 5m polymarket "
        "slots using binance lead for timing and real entry prices",
    ),
    (
        "long_irrelevant",
        "write me a python function that sorts a list of strings by length",
    ),
    ("russian_with_kw", "покажи мне статистику по pnl для poly backtest"),
    ("russian_no_kw", "какая сегодня погода и что нового"),
    ("fallback_only_fee", "what are the fee mechanics for traders"),
    ("live_deploy", "should I deploy my new live bot for polymarket"),
]


def _measure(fn: Callable[[], Any], n: int = 1000) -> dict[str, float]:
    """Run `fn` n times, return latency percentiles in milliseconds."""
    # Warmup to stabilize caches / JIT paths
    warmup = min(20, n)
    for _ in range(warmup):
        fn()

    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()

    def pct(p: float) -> float:
        idx = min(int(len(samples) * p), len(samples) - 1)
        return samples[idx]

    return {
        "p50": round(pct(0.50), 4),
        "p95": round(pct(0.95), 4),
        "p99": round(pct(0.99), 4),
        "max": round(samples[-1], 4),
        "mean": round(statistics.fmean(samples), 4),
        "n": n,
    }


def _find_cortex_hook() -> str | None:
    """Locate the cortex-hook entry point. Returns None if not installed."""
    import shutil

    return shutil.which("cortex-hook")


def _storage_stats(db_path: str) -> dict[str, Any]:
    from cortex.store import CortexStore

    store = CortexStore(db_path)
    try:
        st = store.stats()
        size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
        return {
            "db_path": db_path,
            "db_size_bytes": size,
            "db_size_kb": round(size / 1024.0, 1),
            "n_tripwires": st["total_tripwires"],
            "n_violations": st["total_violations"],
            "n_cost_components": len(store.list_cost_components()),
            "n_synthesis_rules": len(store.list_synthesis_rules()),
            "by_severity": {k: v["n"] for k, v in st["by_severity"].items()},
        }
    finally:
        store.close()


def _session_log_stats() -> dict[str, Any]:
    try:
        from cortex.session import sessions_dir

        sdir = sessions_dir()
        total = 0
        n = 0
        for f in sdir.glob("*.jsonl"):
            total += f.stat().st_size
            n += 1
        return {
            "dir": str(sdir),
            "n_files": n,
            "total_size_bytes": total,
            "total_size_kb": round(total / 1024.0, 1),
        }
    except Exception:
        return {"error": "could not read sessions dir"}


def _subsystem_latency(db_path: str, iterations: int) -> dict[str, dict]:
    """Benchmark each Cortex subsystem in isolation, in-process."""
    from cortex.classify import _tokenize as classify_tokens
    from cortex.classify import classify_prompt, render_brief
    from cortex.store import CortexStore
    from cortex.synthesize import synthesize
    from cortex.tfidf_fallback import fallback_search

    latency: dict[str, dict] = {}
    prompt = "run a 5m poly directional backtest on btc with binance lead"

    # 1. Tokenize (pure regex + set ops)
    latency["tokenize"] = _measure(
        lambda: classify_tokens(prompt),
        n=iterations,
    )

    # 2. Full classify_prompt (opens store, rules, synthesize)
    latency["classify_prompt"] = _measure(
        lambda: classify_prompt(prompt, db_path=db_path),
        n=iterations,
    )

    # Keep a long-lived store for downstream benches
    store = CortexStore(db_path)
    try:
        # 3. Fallback search (in-process, store already open)
        latency["fallback_search"] = _measure(
            lambda: fallback_search(
                "покажи фичи по pnl для poly", store,
            ),
            n=iterations,
        )

        # 4. Synthesize over realistic matched set
        matched_ids = {
            "directional_5m_dead",
            "information_decay_5m",
            "adverse_selection_maker",
        }
        latency["synthesize"] = _measure(
            lambda: synthesize(matched_ids, store),
            n=iterations,
        )
    finally:
        store.close()

    # 5. Render brief on a realistic result
    result = classify_prompt(prompt, db_path=db_path)
    latency["render_brief"] = _measure(
        lambda: render_brief(result),
        n=iterations,
    )

    return latency


def _brief_size_distribution(db_path: str) -> list[dict[str, Any]]:
    """Measure brief size across canned test prompts."""
    from cortex.classify import classify_prompt, render_brief

    out: list[dict[str, Any]] = []
    for label, prompt in TEST_PROMPTS:
        result = classify_prompt(prompt, db_path=db_path)
        brief = render_brief(result)
        chars = len(brief)
        tokens_est = chars // _TOKEN_CHARS
        out.append({
            "label": label,
            "prompt": prompt[:60],
            "chars": chars,
            "tokens_est": tokens_est,
            "matched_rules": result.get("matched_rules") or [],
            "matched_tripwires": len(result.get("tripwires") or []),
            "synthesis_fired": len(result.get("synthesis") or []),
        })
    return out


def _hook_subprocess_latency(iterations: int = 10) -> dict[str, Any] | None:
    """Measure end-to-end cortex-hook subprocess cost.

    This is the real cost model: Claude Code spawns a fresh cortex-hook
    process for each user prompt. Python startup + import time dominates.
    Returns None if cortex-hook is not on PATH.
    """
    entry = _find_cortex_hook()
    if not entry:
        return None

    test_json = json.dumps({
        "session_id": "bench",
        "prompt": "run a 5m poly directional backtest on btc",
    })

    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            subprocess.run(
                [entry],
                input=test_json,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            continue
        samples.append((time.perf_counter() - t0) * 1000.0)

    if not samples:
        return None
    samples.sort()
    return {
        "p50": round(samples[len(samples) // 2], 1),
        "max": round(samples[-1], 1),
        "min": round(samples[0], 1),
        "mean": round(statistics.fmean(samples), 1),
        "n": len(samples),
    }


def run_benchmarks(
    db_path: str | None = None,
    iterations: int = 1000,
    skip_subprocess: bool = False,
) -> dict[str, Any]:
    """Run the full benchmark suite. Returns a structured result dict.

    Args:
        db_path: Path to SQLite store. Defaults to `find_db()` walk-up.
        iterations: Number of iterations for in-process latency samples.
        skip_subprocess: If True, skip the slow end-to-end hook measurement.
    """
    from cortex.classify import find_db

    db = db_path or find_db()

    try:
        cortex_version = __import__("cortex").__version__
    except Exception:
        cortex_version = "unknown"

    report: dict[str, Any] = {
        "env": {
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "cortex_version": cortex_version,
        },
        "storage": _storage_stats(db),
        "session_logs": _session_log_stats(),
        "latency_ms": _subsystem_latency(db, iterations),
        "brief_sizes": _brief_size_distribution(db),
    }

    if not skip_subprocess:
        hook = _hook_subprocess_latency(iterations=10)
        if hook is not None:
            report["hook_subprocess_ms"] = hook

    # Token impact analysis
    briefs = report["brief_sizes"]
    matched = [b for b in briefs if b["matched_tripwires"] > 0]
    if matched:
        avg_tokens = sum(b["tokens_est"] for b in matched) // len(matched)
        max_tokens = max(b["tokens_est"] for b in matched)
        break_even_rate = (
            _ASSUMED_PREVENTED_MISTAKE_TOKENS // avg_tokens if avg_tokens else 0
        )
        report["impact"] = {
            "avg_brief_tokens": avg_tokens,
            "max_brief_tokens": max_tokens,
            "assumed_mistake_cost_tokens": _ASSUMED_PREVENTED_MISTAKE_TOKENS,
            "break_even_injections_per_prevented_mistake": break_even_rate,
            "note": (
                "break_even = how many injections must occur for 1 to "
                "prevent a mistake before Cortex is net-positive on tokens. "
                "Lower is better. A value of 5 means Cortex pays for itself "
                "if at least 1 in 5 injections prevents a mistake."
            ),
        }
    else:
        report["impact"] = {"note": "no matched prompts in test set"}

    return report


def render_report(report: dict[str, Any]) -> str:
    """Render a benchmark report as human-readable text."""
    lines: list[str] = []
    env = report.get("env", {})
    lines.append(f"Cortex v{env.get('cortex_version', '?')} benchmark report")
    lines.append("=" * 72)
    lines.append(f"Python:     {env.get('python_version', '?')}")
    lines.append(f"Platform:   {env.get('platform', '?')}")
    lines.append("")

    # Storage
    st = report.get("storage", {})
    lines.append("## Storage footprint")
    lines.append(f"  SQLite store:       {st.get('db_size_kb', '?')} KB")
    lines.append(f"  Tripwires:          {st.get('n_tripwires', '?')}")
    by_sev = st.get("by_severity") or {}
    if by_sev:
        sev_parts = [f"{k}={v}" for k, v in by_sev.items()]
        lines.append(f"    by severity:      {', '.join(sev_parts)}")
    lines.append(f"  Cost components:    {st.get('n_cost_components', '?')}")
    lines.append(f"  Synthesis rules:    {st.get('n_synthesis_rules', '?')}")
    lines.append(f"  Logged violations:  {st.get('n_violations', '?')}")
    lines.append("")

    # Session logs
    sl = report.get("session_logs") or {}
    if "error" not in sl:
        lines.append("## Session audit logs")
        lines.append(f"  Files:              {sl.get('n_files', '?')}")
        lines.append(f"  Total size:         {sl.get('total_size_kb', '?')} KB")
        lines.append("")

    # In-process latency
    lat = report.get("latency_ms") or {}
    if lat:
        sample = next(iter(lat.values()))
        n = sample.get("n", "?")
        lines.append(f"## In-process subsystem latency (N={n}, ms)")
        lines.append(
            f"  {'Component':<20} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9} {'mean':>9}"
        )
        lines.append("  " + "-" * 68)
        for name, stats in lat.items():
            lines.append(
                f"  {name:<20} "
                f"{stats['p50']:>9.4f} "
                f"{stats['p95']:>9.4f} "
                f"{stats['p99']:>9.4f} "
                f"{stats['max']:>9.4f} "
                f"{stats['mean']:>9.4f}"
            )
        lines.append("")

    # Brief sizes
    briefs = report.get("brief_sizes") or []
    if briefs:
        lines.append("## Brief size per prompt")
        lines.append(
            f"  {'Label':<22} {'chars':>7} {'tokens*':>8} {'tw':>4} {'synth':>6}  prompt"
        )
        lines.append("  " + "-" * 88)
        for b in briefs:
            lines.append(
                f"  {b['label']:<22} "
                f"{b['chars']:>7} "
                f"{b['tokens_est']:>8} "
                f"{b['matched_tripwires']:>4} "
                f"{b['synthesis_fired']:>6}  "
                f"{b['prompt']}"
            )
        lines.append(f"  *tokens_est = chars // {_TOKEN_CHARS} (rough estimate)")
        lines.append("")

        matched = [b for b in briefs if b["matched_tripwires"] > 0]
        if matched:
            avg_chars = sum(b["chars"] for b in matched) // len(matched)
            max_chars = max(b["chars"] for b in matched)
            lines.append(f"  Matched prompts: {len(matched)}/{len(briefs)}")
            lines.append(
                f"  Avg brief on matches: {avg_chars} chars "
                f"≈ {avg_chars // _TOKEN_CHARS} tokens"
            )
            lines.append(
                f"  Max brief on matches: {max_chars} chars "
                f"≈ {max_chars // _TOKEN_CHARS} tokens"
            )
            lines.append("")

    # Hook subprocess
    hs = report.get("hook_subprocess_ms")
    if hs:
        lines.append("## End-to-end `cortex-hook` subprocess latency")
        lines.append(f"  Mean:      {hs['mean']} ms")
        lines.append(f"  p50:       {hs['p50']} ms")
        lines.append(f"  min:       {hs['min']} ms")
        lines.append(f"  max:       {hs['max']} ms")
        lines.append(f"  (N={hs['n']}, includes fresh Python startup + imports)")
        lines.append("")

    # Impact
    imp = report.get("impact") or {}
    if imp.get("avg_brief_tokens"):
        lines.append("## Token impact analysis")
        lines.append(
            f"  Avg brief:                         ~{imp['avg_brief_tokens']} tokens"
        )
        lines.append(
            f"  Max brief:                         ~{imp['max_brief_tokens']} tokens"
        )
        lines.append(
            f"  Assumed prevented-mistake cost:    ~{imp['assumed_mistake_cost_tokens']} tokens"
        )
        lines.append(
            f"  Break-even rate:                   1 prevented mistake per "
            f"{imp['break_even_injections_per_prevented_mistake']} injections"
        )
        lines.append("")
        lines.append("  Interpretation:")
        for line in imp.get("note", "").split(". "):
            if line.strip():
                lines.append(f"    {line.strip()}.")
        lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)
