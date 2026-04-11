"""Cortex CLI — thin argparse wrapper over the store."""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

from cortex.store import CortexStore

DEFAULT_DB = ".cortex/store.db"
SEVERITIES = ("critical", "high", "medium", "low")


def _open(args: argparse.Namespace) -> CortexStore:
    return CortexStore(args.db)


def cmd_init(args: argparse.Namespace) -> int:
    store = _open(args)
    store.close()
    print(f"Initialized cortex store at {Path(args.db).resolve()}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from cortex.importers.memory_md import run_migration

    n = run_migration(args.db)
    print(f"Migrated {n} seed tripwires into {args.db}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with _open(args) as store:
        rows = store.list_tripwires(domain=args.domain, severity=args.severity)
    if not rows:
        print("(no tripwires)")
        return 0
    print(f"{'ID':<28} {'SEV':<9} {'DOMAIN':<12} {'COST':>8}  {'VIOL':>4}  TITLE")
    print("-" * 100)
    for r in rows:
        title = r["title"][:38]
        print(
            f"{r['id']:<28} {r['severity']:<9} {r['domain']:<12} "
            f"${r['cost_usd']:>7.2f}  {r['violation_count']:>4}  {title}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with _open(args) as store:
        tw = store.get_tripwire(args.id)
    if not tw:
        print(f"No tripwire with id={args.id!r}", file=sys.stderr)
        return 1
    print(f"ID:          {tw['id']}")
    print(f"Title:       {tw['title']}")
    print(f"Severity:    {tw['severity']}")
    print(f"Domain:      {tw['domain']}")
    print(f"Triggers:    {', '.join(tw['triggers'])}")
    print(f"Cost (USD):  ${tw['cost_usd']:.2f}")
    print(f"Born at:     {tw['born_at']}")
    print(f"Violations:  {tw['violation_count']}")
    if tw["last_violated_at"]:
        print(f"Last hit:    {tw['last_violated_at']}")
    if tw["verify_cmd"]:
        print(f"Verify:      {tw['verify_cmd']}")
    if tw["source_file"]:
        print(f"Source:      {tw['source_file']}")
    patterns = tw.get("violation_patterns") or []
    if patterns:
        print(f"Patterns:    {len(patterns)} violation regex(es)")
        for p in patterns:
            print(f"  - {p}")
    print()
    print("Body:")
    print(tw["body"])
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    if getattr(args, "sessions", False):
        return _cmd_stats_sessions(args)
    with _open(args) as store:
        s = store.stats()
    print(f"Total tripwires:  {s['total_tripwires']}")
    print(f"Total violations: {s['total_violations']}")
    print()
    print("By severity:")
    for sev in SEVERITIES:
        data = s["by_severity"].get(sev)
        if not data:
            continue
        print(
            f"  {sev:<10} n={data['n']:<3} cost=${data['cost']:.2f}"
            f"  violations={data['violations']}"
        )
    print()
    print("By domain:")
    for dom, data in sorted(s["by_domain"].items()):
        print(f"  {dom:<15} n={data['n']}")
    return 0


def _cmd_stats_sessions(args: argparse.Namespace) -> int:
    from cortex.stats import (
        collect_sessions,
        compute_stats,
        find_cold_tripwires,
        render_stats,
    )

    sessions = collect_sessions(days=args.days)
    stats = compute_stats(sessions)
    with _open(args) as store:
        all_ids = [tw["id"] for tw in store.list_tripwires()]
    cold = find_cold_tripwires(stats, all_ids)
    print(render_stats(stats, cold, days=args.days))
    return 0


def cmd_import_palace(args: argparse.Namespace) -> int:
    """Query Palace and emit tripwire draft templates the user can
    copy into cortex/importers/memory_md.py after review.

    This is a smart-search helper: Palace stays authoritative for
    broad semantic recall, Cortex stays authoritative for active
    injection. The human in the loop is intentional -- automatic
    drawer-to-tripwire promotion would dilute the curated signal.
    """
    if not args.palace_path:
        print(
            "Palace path not configured. Pass --palace-path or set "
            "CORTEX_PALACE_PATH environment variable.",
            file=sys.stderr,
        )
        return 2

    try:
        from mempalace.searcher import search_memories
    except ImportError:
        print(
            "mempalace is not installed. Install it or run palace_search.py directly.",
            file=sys.stderr,
        )
        return 1

    try:
        result = search_memories(
            args.query,
            palace_path=args.palace_path,
            wing=args.wing,
            n_results=args.n,
        )
    except Exception as e:
        print(f"Palace search failed: {e}", file=sys.stderr)
        return 1

    if not isinstance(result, dict) or "error" in result:
        err = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
        print(f"Palace returned error: {err}", file=sys.stderr)
        return 1

    hits = result.get("results") or []
    eligible = [h for h in hits if h.get("similarity", 0.0) >= args.min_sim]

    if not eligible:
        print(
            f"No hits above min_sim={args.min_sim} for: {args.query}",
            file=sys.stderr,
        )
        return 0

    print(f"{len(eligible)} Palace hit(s) for: {args.query}")
    print(f"(wing={args.wing}, min_sim={args.min_sim})")
    print()

    for i, hit in enumerate(eligible, 1):
        sim = hit.get("similarity", 0.0)
        room = hit.get("room", "")
        src = hit.get("source_file", "")
        text = (hit.get("text") or "")[:500]
        print(f"[{i}] {room}/{src}  (sim={sim:.3f})")
        print("-" * 70)
        for line in text.splitlines():
            print(f"    {line}")
        print()
        print("    Draft tripwire to review and paste into")
        print("    cortex/importers/memory_md.py SEED_TRIPWIRES:")
        print()
        print("    {")
        print('        "id": "TODO_snake_case_id",')
        print('        "title": "TODO one-line summary (<=80 chars)",')
        print('        "severity": "medium",  # critical | high | medium | low')
        print(f'        "domain": "{args.wing}",')
        print('        "triggers": ["TODO", "extract", "from", "body"],')
        print('        "body": (')
        print('            "TODO one-sentence rule statement.\\n"')
        print('            "\\n"')
        print(f'            "Why: distilled from {src}.\\n"')
        print('            "\\n"')
        print('            "How to apply: (1) TODO. (2) TODO. (3) edge case."')
        print("        ),")
        print('        "verify_cmd": None,')
        print('        "cost_usd": 0.0,')
        print(f'        "source_file": "{src}",')
        print("    },")
        print()

    return 0


def cmd_find(args: argparse.Namespace) -> int:
    words = [w.strip() for w in args.words.split(",") if w.strip()]
    with _open(args) as store:
        hits = store.find_by_triggers(words)
    if not hits:
        print("(no matches)")
        return 0
    print(f"{len(hits)} match(es) for triggers: {words}")
    for h in hits:
        print(f"  [{h['severity']:<9}] {h['id']:<28}  {h['title'][:50]}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    triggers = [t.strip() for t in args.triggers.split(",") if t.strip()]
    with _open(args) as store:
        store.add_tripwire(
            id=args.id,
            title=args.title,
            severity=args.severity,
            domain=args.domain,
            triggers=triggers,
            body=args.body,
            verify_cmd=args.verify_cmd,
            cost_usd=args.cost_usd,
            source_file=args.source_file,
        )
    print(f"Added tripwire: {args.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cortex",
        description="Active memory and executive control for AI coding agents.",
    )
    p.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite store path (default: {DEFAULT_DB})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize an empty store").set_defaults(func=cmd_init)

    mig = sub.add_parser("migrate", help="Import seed tripwires from MEMORY.md")
    mig.set_defaults(func=cmd_migrate)

    ls = sub.add_parser("list", help="List tripwires")
    ls.add_argument("--domain", help="Filter by domain")
    ls.add_argument("--severity", choices=SEVERITIES, help="Filter by severity")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="Show one tripwire")
    sh.add_argument("id")
    sh.set_defaults(func=cmd_show)

    stats_p = sub.add_parser("stats", help="Store statistics (or session audit with --sessions)")
    stats_p.add_argument(
        "--sessions",
        action="store_true",
        help="Analyze .cortex/sessions/ audit log instead of store",
    )
    stats_p.add_argument(
        "--days",
        type=int,
        default=None,
        help="With --sessions, limit to last N days",
    )
    stats_p.set_defaults(func=cmd_stats)

    ip = sub.add_parser(
        "import-palace",
        help="Search Palace memory and emit tripwire draft templates",
    )
    ip.add_argument("query", help="Palace semantic search query")
    ip.add_argument("--n", type=int, default=3, help="Max hits to request from Palace")
    ip.add_argument(
        "--min-sim",
        type=float,
        default=0.4,
        dest="min_sim",
        help="Minimum similarity threshold (default 0.4)",
    )
    ip.add_argument(
        "--palace-path",
        default=os.environ.get("CORTEX_PALACE_PATH"),
        dest="palace_path",
        help=(
            "Path to the Palace chromadb directory. "
            "Defaults to $CORTEX_PALACE_PATH env var."
        ),
    )
    ip.add_argument(
        "--wing",
        default=os.environ.get("CORTEX_PALACE_WING", "polymarket"),
        help="Palace wing to search (env: CORTEX_PALACE_WING, default: polymarket)",
    )
    ip.set_defaults(func=cmd_import_palace)

    fd = sub.add_parser("find", help="Find tripwires by trigger keywords")
    fd.add_argument("words", help="Comma-separated list of words")
    fd.set_defaults(func=cmd_find)

    ad = sub.add_parser("add", help="Add a tripwire manually")
    ad.add_argument("--id", required=True)
    ad.add_argument("--title", required=True)
    ad.add_argument("--severity", required=True, choices=SEVERITIES)
    ad.add_argument("--domain", required=True)
    ad.add_argument("--triggers", required=True, help="Comma-separated trigger words")
    ad.add_argument("--body", required=True)
    ad.add_argument("--verify-cmd", default=None, dest="verify_cmd")
    ad.add_argument("--cost-usd", type=float, default=0.0, dest="cost_usd")
    ad.add_argument("--source-file", default=None, dest="source_file")
    ad.set_defaults(func=cmd_add)

    return p


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout so non-ASCII content in tripwire bodies and
    # Palace hits doesn't crash on Windows cp1251 consoles.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
