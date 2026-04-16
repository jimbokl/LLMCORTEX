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
    # Day 15: --all shows every row regardless of status; --status X
    # filters to a specific bucket; default is 'active' so pre-Day-15
    # workflows keep seeing only live rules.
    status: str | None = None if getattr(args, "all", False) else args.status
    with _open(args) as store:
        rows = store.list_tripwires(
            domain=args.domain, severity=args.severity, status=status,
        )
    if not rows:
        print("(no tripwires)")
        return 0
    print(
        f"{'ID':<28} {'SEV':<9} {'STATUS':<9} {'DOMAIN':<12} "
        f"{'COST':>8}  {'VIOL':>4}  TITLE"
    )
    print("-" * 110)
    for r in rows:
        title = r["title"][:32]
        st = r.get("status", "active") or "active"
        print(
            f"{r['id']:<28} {r['severity']:<9} {st:<9} {r['domain']:<12} "
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
    print(f"Status:      {tw.get('status', 'active')}")
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
    from cortex.fitness import compute_fitness
    from cortex.stats import (
        collect_sessions,
        compute_primary_vs_fallback_ratio,
        compute_stats,
        find_cold_tripwires,
        render_stats,
    )

    sessions = collect_sessions(days=args.days)
    stats = compute_stats(sessions)
    with _open(args) as store:
        all_tripwires = store.list_tripwires()
        # Day 16: hydrate the Haiku classification index from the
        # `pair_classifications` table so `compute_fitness` can
        # override its Day-14 token-overlap heuristic per-pair. Empty
        # table -> empty dict -> bit-identical behavior to Day 14.
        classifications = store.list_pair_classifications()
    all_ids = [tw["id"] for tw in all_tripwires]
    bodies = {tw["id"]: tw.get("body") or "" for tw in all_tripwires}
    costs = {tw["id"]: float(tw.get("cost_usd") or 0.0) for tw in all_tripwires}
    classification_index: dict[tuple[str, str], str] = {
        (row["session_id"], row["at"]): row["label"] for row in classifications
    }
    cold = find_cold_tripwires(stats, all_ids)
    ratio = compute_primary_vs_fallback_ratio(sessions)
    fitness = compute_fitness(
        sessions,
        tripwire_bodies=bodies,
        tripwire_costs=costs,
        classification_index=classification_index,
    )
    print(render_stats(
        stats, cold, days=args.days,
        anonymize=getattr(args, "anonymize", False),
        ratio=ratio,
        fitness=fitness,
    ))
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    from cortex.session import read_session
    from cortex.stats import render_timeline

    events = read_session(args.session_id)
    if not events:
        print(f"No events found for session {args.session_id}", file=sys.stderr)
        return 1
    print(render_timeline(
        args.session_id, events,
        anonymize=getattr(args, "anonymize", False),
        max_events=args.max_events,
    ))
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

    if args.to_inbox:
        from cortex.inbox import write_draft

        staged: list[str] = []
        for hit in eligible:
            src = hit.get("source_file", "")
            text = (hit.get("text") or "")[:1500]
            draft = {
                "id": "TODO_snake_case_id",
                "title": "TODO one-line summary",
                "severity": "medium",
                "domain": args.wing,
                "triggers": ["TODO", "extract", "from", "body"],
                "body": (
                    "TODO one-sentence rule statement.\n"
                    "\n"
                    f"Why: distilled from Palace {hit.get('room', '')}/{src}.\n"
                    "\n"
                    "How to apply: (1) TODO. (2) TODO. (3) edge case.\n"
                    "\n"
                    f"--- Palace excerpt (similarity {hit.get('similarity', 0.0):.3f}) ---\n"
                    f"{text}"
                ),
                "verify_cmd": None,
                "cost_usd": 0.0,
                "source_file": src,
            }
            draft_id = write_draft(draft, source=f"palace_{args.wing}")
            if draft_id:
                staged.append(draft_id)
                print(f"  staged: {draft_id}  ({hit.get('room', '')}/{src})")
        print()
        print(
            f"Staged {len(staged)} draft(s) into the inbox. "
            "Edit fields, then approve:"
        )
        print("  cortex inbox list")
        print("  cortex inbox show <draft_id>")
        print("  cortex inbox approve <draft_id>")
        return 0

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

    print("Tip: re-run with --to-inbox to stage these drafts as editable")
    print("     JSON files under .cortex/inbox/ instead of printing templates.")

    return 0


# ---- inbox commands (Day 8) ----


def cmd_inbox_list(args: argparse.Namespace) -> int:
    from cortex.inbox import list_drafts

    drafts = list_drafts()
    if not drafts:
        print("(inbox is empty)")
        return 0
    print(f"{'DRAFT_ID':<40} {'SOURCE':<20} {'ID_FIELD':<28} STATUS")
    print("-" * 100)
    from cortex.inbox import validate_draft

    for d in drafts:
        draft = d.get("draft") or {}
        missing, todos = validate_draft(draft)
        if missing:
            status = f"MISSING: {','.join(missing[:3])}"
        elif todos:
            status = f"TODO: {','.join(todos[:3])}"
        else:
            status = "READY"
        print(
            f"{d.get('draft_id', ''):<40} "
            f"{d.get('source', ''):<20} "
            f"{str(draft.get('id', '')):<28} "
            f"{status}"
        )
    return 0


def cmd_inbox_show(args: argparse.Namespace) -> int:
    import json as _json

    from cortex.inbox import read_draft, validate_draft

    d = read_draft(args.draft_id)
    if not d:
        print(f"Draft not found: {args.draft_id}", file=sys.stderr)
        return 1
    print(f"Draft ID:   {d.get('draft_id', '')}")
    print(f"Source:     {d.get('source', '')}")
    print(f"Created at: {d.get('created_at', '')}")
    draft = d.get("draft") or {}
    missing, todos = validate_draft(draft)
    if missing:
        print(f"Missing:    {', '.join(missing)}")
    if todos:
        print(f"TODO in:    {', '.join(todos)}")
    if not missing and not todos:
        print("Status:     READY to approve")
    print()
    print("Draft contents:")
    print(_json.dumps(draft, indent=2, ensure_ascii=False))
    return 0


def cmd_inbox_approve(args: argparse.Namespace) -> int:
    from cortex.inbox import (
        delete_draft,
        draft_to_tripwire_kwargs,
        read_draft,
        validate_draft,
    )

    d = read_draft(args.draft_id)
    if not d:
        print(f"Draft not found: {args.draft_id}", file=sys.stderr)
        return 1
    draft = d.get("draft") or {}

    missing, todos = validate_draft(draft)
    if missing:
        print(
            f"Cannot approve {args.draft_id}: missing required fields: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            f"Edit {args.draft_id}.json in the inbox directory and retry.",
            file=sys.stderr,
        )
        return 2
    if todos and not args.force:
        print(
            f"Cannot approve {args.draft_id}: TODO placeholders in: {', '.join(todos)}",
            file=sys.stderr,
        )
        print(
            "Edit the draft to fill them, or re-run with --force to approve as-is.",
            file=sys.stderr,
        )
        return 2

    kwargs = draft_to_tripwire_kwargs(draft)
    # Day 15: `--shadow` promotes the draft as a shadow tripwire instead
    # of active. The rule is matched by the classifier and logged as a
    # `shadow_hit` audit event, but NEVER rendered into <cortex_brief>.
    # This is the safe probation path for DMN-proposed rules.
    if getattr(args, "shadow", False):
        kwargs["status"] = "shadow"
    try:
        with _open(args) as store:
            store.add_tripwire(**kwargs)
    except Exception as e:
        print(f"Failed to add tripwire to store: {e}", file=sys.stderr)
        return 3

    delete_draft(args.draft_id)
    status_label = kwargs.get("status", "active")
    print(
        f"Approved: {kwargs['id']} (status={status_label}, "
        f"draft {args.draft_id} removed)"
    )
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    try:
        from cortex.dmn import render_reflection_report, run_reflection
    except ImportError as e:
        print(f"cortex reflect requires anthropic: {e}", file=sys.stderr)
        print(
            "Install via: pip install cortex-agent[dmn]",
            file=sys.stderr,
        )
        return 1

    db = args.db if args.db != DEFAULT_DB else None
    result = run_reflection(
        days=args.days,
        model=args.model,
        max_proposals=args.max_proposals,
        dry_run=args.dry_run,
        db_path=db,
    )
    print(render_reflection_report(result))
    return 0 if result.get("error") is None else 2


def cmd_suggest_patterns(args: argparse.Namespace) -> int:
    from cortex.suggest_patterns import (
        analyze_snippets,
        collect_post_injection_snippets,
        generate_regex_candidates,
        render_suggestions,
    )

    findings = collect_post_injection_snippets(
        args.tripwire_id, window=args.window,
    )
    analysis = analyze_snippets(findings)
    candidates = generate_regex_candidates(analysis, fix_example=args.fix_example)
    print(
        render_suggestions(
            args.tripwire_id,
            findings,
            analysis,
            candidates=candidates,
            fix_example=args.fix_example,
        )
    )
    return 0


def cmd_surprise(args: argparse.Namespace) -> int:
    from cortex.surprise import collect_pairs, render_surprise_table

    pairs = collect_pairs(days=args.days)
    print(render_surprise_table(pairs, days=args.days, max_rows=args.max_rows))
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from cortex.bench import render_report, run_benchmarks

    report = run_benchmarks(
        db_path=args.db if args.db != DEFAULT_DB else None,
        iterations=args.iterations,
        skip_subprocess=args.no_subprocess,
    )
    if args.json:
        import json as _json

        print(_json.dumps(report, indent=2, default=str))
        return 0
    print(render_report(report))
    return 0


def cmd_inbox_reject(args: argparse.Namespace) -> int:
    from cortex.inbox import delete_draft, read_draft

    d = read_draft(args.draft_id)
    if not d:
        print(f"Draft not found: {args.draft_id}", file=sys.stderr)
        return 1
    if delete_draft(args.draft_id):
        print(f"Rejected: {args.draft_id} (removed from inbox)")
        return 0
    print(f"Failed to delete {args.draft_id}", file=sys.stderr)
    return 3


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
            status=args.status,
        )
    print(f"Added tripwire: {args.id} (status={args.status})")
    return 0


def cmd_install_skills(args: argparse.Namespace) -> int:
    """Copy bundled SKILL.md files into ~/.claude/skills/ (or project)."""
    from cortex.skills_install import (
        default_project_skills_dir,
        default_user_skills_dir,
        install_skills,
        list_bundled_skills,
        render_install_report,
    )

    if args.list:
        names = list_bundled_skills()
        if not names:
            print("(no bundled skills found)")
            return 1
        print(f"{len(names)} bundled skill(s):")
        for n in names:
            print(f"  - {n}")
        return 0

    target = (
        default_project_skills_dir() if args.project else default_user_skills_dir()
    )
    only: list[str] | None = None
    if args.only:
        only = [s.strip() for s in args.only.split(",") if s.strip()]
    report = install_skills(target, only=only, force=args.force)
    print(render_install_report(report))
    if report["errors"]:
        return 2
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Day 15: explicit status transition for a tripwire."""
    with _open(args) as store:
        if store.get_tripwire(args.id) is None:
            print(f"No tripwire with id={args.id!r}", file=sys.stderr)
            return 1
        try:
            ok = store.set_status(args.id, args.new_status)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if not ok:
        print(f"Failed to update status for {args.id}", file=sys.stderr)
        return 3
    print(f"{args.id}: status -> {args.new_status}")
    return 0


# --------------------------------------------------------------------
# Day 16: DMN promoter CLI
# --------------------------------------------------------------------


def cmd_sessions_prune(args: argparse.Namespace) -> int:
    import time

    from cortex.session import prune_sessions, sessions_dir

    if args.days < 0:
        print("error: --days must be non-negative", file=sys.stderr)
        return 2

    if args.dry_run:
        target = sessions_dir()
        cutoff = time.time() - args.days * 86400
        matches = [
            p.name for p in target.glob("*.jsonl")
            if p.stat().st_mtime < cutoff
        ]
        print(f"[dry-run] would delete {len(matches)} session log(s) older than {args.days}d")
        for name in matches:
            print(f"  would-delete {name}")
        return 0

    n, deleted = prune_sessions(args.days)
    print(f"Deleted {n} session log(s) older than {args.days}d")
    if args.verbose and deleted:
        for name in deleted:
            print(f"  deleted {name}")
    return 0


def cmd_promote_classify(args: argparse.Namespace) -> int:
    """Classify unclassified surprise pairs via Haiku."""
    from datetime import datetime, timezone

    from cortex import promoter
    from cortex.surprise import collect_pairs

    pairs = collect_pairs(days=args.days)
    if not pairs:
        print(f"No surprise pairs found in the last {args.days} days.")
        return 0

    with _open(args) as store:
        existing = store.list_pair_classifications()
        existing_keys = {(r["session_id"], r["at"]) for r in existing}
        to_classify = [
            p for p in pairs if (p["session_id"], p["at"]) not in existing_keys
        ]
        if not to_classify:
            print(
                f"All {len(pairs)} pair(s) already classified -- nothing to do."
            )
            return 0

        # Hard cap at 200 per invocation to keep bill shock impossible.
        batch_size = min(args.batch_size, 200)
        to_classify = to_classify[:batch_size]

        est_cost = len(to_classify) * 0.0002  # Haiku 4.5 ~rough estimate
        print(
            f"Will classify {len(to_classify)} pair(s) "
            f"(of {len(pairs)} total, {len(existing)} already on record). "
            f"Estimated cost: ~${est_cost:.4f}."
        )
        if args.dry_run:
            print("[dry run] no Haiku calls, no DB writes.")
            for p in to_classify[:5]:
                print(
                    f"  - {p['session_id'][:12]} at {p['at'][:19]} "
                    f"tool={p['tool_name']}"
                )
            if len(to_classify) > 5:
                print(f"  ... and {len(to_classify) - 5} more")
            return 0

        if len(to_classify) > 10 and not args.yes:
            print(
                "Pass --yes to confirm; refusing to spend >$0.002 "
                "implicitly. Use --batch-size to narrow the window."
            )
            return 1

        classified = 0
        errors = 0
        for pair in to_classify:
            result = promoter.classify_pair(pair, model=args.model)
            if result["label"] == "error":
                errors += 1
            classified_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            store.upsert_pair_classification(
                session_id=pair["session_id"],
                at=pair["at"],
                tripwire_ids=pair.get("tripwire_ids") or [],
                label=result["label"],
                confidence=result.get("confidence", 0.0),
                reasoning=result.get("reasoning", ""),
                model=result.get("model", args.model),
                classified_at=classified_at,
            )
            classified += 1

    print(
        f"Classified {classified} pair(s), errors={errors}. "
        f"See `cortex stats --sessions` for updated fitness."
    )
    return 0


def cmd_promote_run(args: argparse.Namespace) -> int:
    """Run the promoter decider and optionally apply decisions."""
    from cortex import promoter
    from cortex.fitness import compute_fitness
    from cortex.stats import collect_sessions

    sessions = collect_sessions(days=args.days)

    with _open(args) as store:
        all_tripwires = store.list_tripwires(status=None)
        bodies = {tw["id"]: tw.get("body") or "" for tw in all_tripwires}
        costs = {
            tw["id"]: float(tw.get("cost_usd") or 0.0) for tw in all_tripwires
        }
        classifications = store.list_pair_classifications()
        cls_index: dict[tuple[str, str], str] = {
            (r["session_id"], r["at"]): r["label"] for r in classifications
        }
        fitness = compute_fitness(
            sessions,
            tripwire_bodies=bodies,
            tripwire_costs=costs,
            classification_index=cls_index,
        )

        distinct_sessions = {
            tw_id: int(row.get("distinct_sessions", 0) or 0)
            for tw_id, row in fitness.items()
        }
        mismatches = {
            tw_id: int(row.get("mismatches", 0) or 0)
            for tw_id, row in fitness.items()
        }

        status_history: dict[str, list[dict]] = {}
        for tw in all_tripwires:
            status_history[tw["id"]] = store.list_status_changes(
                tripwire_id=tw["id"]
            )

        decisions = promoter.decide(
            tripwires=all_tripwires,
            fitness=fitness,
            distinct_sessions=distinct_sessions,
            mismatches=mismatches,
            status_history=status_history,
        )

        if not decisions:
            print("No promotion/demotion decisions -- everything stable.")
            return 0

        session_id = args.session_id or "promoter_run"
        results = promoter.apply_decisions(
            store,
            decisions,
            session_id=session_id,
            dry_run=not args.apply,
        )

    header = "APPLIED" if args.apply else "DRY RUN (use --apply to mutate)"
    print(f"Promoter {header}:")
    for r in results:
        tag = "OK" if r.applied else f"SKIP[{r.skip_reason}]"
        print(
            f"  [{tag:<22}] {r.tripwire_id:<30} "
            f"{r.from_status:>8} -> {r.to_status:<8} ({r.reason})"
        )
    return 0


def cmd_promote_log(args: argparse.Namespace) -> int:
    """Show recent status_changes audit rows."""
    from datetime import datetime, timedelta, timezone

    since: str | None = None
    if args.days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        since = cutoff.isoformat(timespec="seconds")

    with _open(args) as store:
        rows = store.list_status_changes(since_iso=since)

    if not rows:
        print("(no status changes recorded)")
        return 0

    header = (
        f"Status changes (last {args.days} days):"
        if args.days is not None
        else "Status changes (all time):"
    )
    print(header)
    print("-" * 80)
    for r in rows:
        meta = r.get("metadata") or {}
        fit = meta.get("fitness")
        fit_str = f" fit={fit:+.2f}" if isinstance(fit, (int, float)) else ""
        print(
            f"  {r['at']:<25} {r['tripwire_id']:<28} "
            f"{r['from_status']:>8} -> {r['to_status']:<8} "
            f"({r['reason']}){fit_str}"
        )
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
    ls.add_argument(
        "--status",
        default="active",
        choices=("active", "shadow", "archived"),
        help="Filter by lifecycle status (default: active). Day 15.",
    )
    ls.add_argument(
        "--all",
        action="store_true",
        help="Show rows of every status (overrides --status). Day 15.",
    )
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
    stats_p.add_argument(
        "--anonymize",
        action="store_true",
        help=(
            "With --sessions, hash session ids and redact tool_input "
            "snippets so the output is safe to share publicly"
        ),
    )
    stats_p.set_defaults(func=cmd_stats)

    # Day 13: session timeline view
    tl = sub.add_parser(
        "timeline",
        help="Render a single session's event timeline as ASCII",
    )
    tl.add_argument("session_id", help="Session id (see `cortex stats --sessions`)")
    tl.add_argument(
        "--anonymize",
        action="store_true",
        help="Hash the session id and redact tool_input snippets in output",
    )
    tl.add_argument(
        "--max-events",
        type=int,
        default=200,
        dest="max_events",
        help="Truncate timeline at N events (default 200)",
    )
    tl.set_defaults(func=cmd_timeline)

    # Day 14: session log rotation.
    sessions_p = sub.add_parser(
        "sessions",
        help="Manage .cortex/sessions/ audit logs (prune/etc.)",
    )
    sessions_sub = sessions_p.add_subparsers(dest="sessions_cmd", required=True)
    prune_p = sessions_sub.add_parser(
        "prune", help="Delete session logs older than N days"
    )
    prune_p.add_argument(
        "--days", type=int, required=True,
        help="Age cutoff in days. Files with mtime older than this are removed.",
    )
    prune_p.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be deleted without touching the filesystem",
    )
    prune_p.add_argument(
        "--verbose", action="store_true",
        help="List every deleted filename (default only prints the count)",
    )
    prune_p.set_defaults(func=cmd_sessions_prune)

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
    ip.add_argument(
        "--to-inbox",
        action="store_true",
        dest="to_inbox",
        help=(
            "Stage hits as draft JSON files under .cortex/inbox/ instead "
            "of printing templates to stdout. Use `cortex inbox` commands "
            "to review and approve."
        ),
    )
    ip.set_defaults(func=cmd_import_palace)

    # ---- Day 8: inbox workflow ----
    inbox_p = sub.add_parser(
        "inbox",
        help="Manage the draft tripwire inbox (list / show / approve / reject)",
    )
    inbox_sub = inbox_p.add_subparsers(dest="inbox_cmd", required=True)

    il = inbox_sub.add_parser("list", help="List pending drafts")
    il.set_defaults(func=cmd_inbox_list)

    ish = inbox_sub.add_parser("show", help="Show one draft with validation status")
    ish.add_argument("draft_id", help="Draft id (see `cortex inbox list`)")
    ish.set_defaults(func=cmd_inbox_show)

    iap = inbox_sub.add_parser(
        "approve",
        help="Promote a draft into the tripwire store",
    )
    iap.add_argument("draft_id", help="Draft id to approve")
    iap.add_argument(
        "--force",
        action="store_true",
        help="Approve even when TODO placeholders remain in the draft",
    )
    iap.add_argument(
        "--shadow",
        action="store_true",
        help=(
            "Promote as a SHADOW tripwire: the rule will match the "
            "classifier and be logged as a `shadow_hit` audit event, "
            "but never rendered into <cortex_brief>. Day 15 safe "
            "probation path for DMN-proposed rules."
        ),
    )
    iap.set_defaults(func=cmd_inbox_approve)

    irj = inbox_sub.add_parser("reject", help="Delete a draft without promoting it")
    irj.add_argument("draft_id", help="Draft id to reject")
    irj.set_defaults(func=cmd_inbox_reject)

    # ---- Day 11: DMN reflection loop ----
    rp = sub.add_parser(
        "reflect",
        help=(
            "Haiku DMN reflection loop: analyze session logs and propose "
            "new tripwires into the inbox for human approval"
        ),
    )
    rp.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days of session history to analyze (default: 7)",
    )
    rp.add_argument(
        "--model",
        default="claude-haiku-4-5-20251001",
        help="Anthropic model id (default: claude-haiku-4-5-20251001)",
    )
    rp.add_argument(
        "--max-proposals",
        type=int,
        default=3,
        dest="max_proposals",
        help="Cap on number of proposals to write to inbox (default: 3)",
    )
    rp.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Build and print the prompt that would be sent to Haiku, "
            "without making an API call. Use to inspect cost/content."
        ),
    )
    rp.set_defaults(func=cmd_reflect)

    # ---- Day 9: pattern authoring helper ----
    spp = sub.add_parser(
        "suggest-patterns",
        help=(
            "Read session logs, surface past tool_calls that followed "
            "injections of a tripwire, highlight recurring identifiers "
            "as regex anchors"
        ),
    )
    spp.add_argument(
        "tripwire_id",
        help="Tripwire to analyze (see `cortex list` or `cortex stats --sessions`)",
    )
    spp.add_argument(
        "--window",
        type=int,
        default=10,
        help="Number of events after each inject to scan (default: 10)",
    )
    spp.add_argument(
        "--fix-example",
        default=None,
        dest="fix_example",
        help=(
            "Optional known-fix snippet. If provided, cortex verifies the "
            "generated regex does NOT match this string. Candidates that DO "
            "match the fix are marked [LOW CONFIDENCE] in the output."
        ),
    )
    spp.set_defaults(func=cmd_suggest_patterns)

    # ---- Day 14: Surprise Engine ----
    sp = sub.add_parser(
        "surprise",
        help=(
            "Show <cortex_predict> blocks paired with their actual "
            "tool_call outcomes (predictive coding / Day 14)"
        ),
    )
    sp.add_argument(
        "--days",
        type=int,
        default=None,
        help="Limit to sessions whose last event is in the last N days",
    )
    sp.add_argument(
        "--max-rows",
        type=int,
        default=30,
        dest="max_rows",
        help="Truncate output at N most recent prediction/outcome pairs (default: 30)",
    )
    sp.set_defaults(func=cmd_surprise)

    # ---- Day 8.5: benchmarks ----
    bp = sub.add_parser(
        "bench",
        help="Benchmark Cortex subsystem latency, storage footprint, brief sizes",
    )
    bp.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="Iterations per in-process latency measurement (default: 1000)",
    )
    bp.add_argument(
        "--no-subprocess",
        action="store_true",
        dest="no_subprocess",
        help="Skip the slower end-to-end cortex-hook subprocess measurement",
    )
    bp.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of human-readable text",
    )
    bp.set_defaults(func=cmd_bench)

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
    ad.add_argument(
        "--status",
        default="active",
        choices=("active", "shadow", "archived"),
        help="Initial lifecycle status (default: active). Day 15.",
    )
    ad.set_defaults(func=cmd_add)

    # ---- skills installer ----
    isk = sub.add_parser(
        "install-skills",
        help=(
            "Copy bundled Claude Code SKILL.md files into "
            "~/.claude/skills/ (or .claude/skills/ with --project)"
        ),
    )
    isk.add_argument(
        "--project",
        action="store_true",
        help=(
            "Install into the current project's .claude/skills/ instead "
            "of the user-level ~/.claude/skills/. Useful when the skills "
            "should ship with the repo."
        ),
    )
    isk.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing skill directories",
    )
    isk.add_argument(
        "--only",
        default=None,
        help=(
            "Comma-separated list of skill names to install (e.g. "
            "'cortex-bootstrap,cortex-status'). Default: install all."
        ),
    )
    isk.add_argument(
        "--list",
        action="store_true",
        help="List bundled skills without copying anything",
    )
    isk.set_defaults(func=cmd_install_skills)

    # ---- Day 15: explicit status transitions ----
    st = sub.add_parser(
        "status",
        help="Transition a tripwire between active / shadow / archived",
    )
    st.add_argument("id", help="Tripwire id")
    st.add_argument(
        "new_status",
        choices=("active", "shadow", "archived"),
        help="New lifecycle status",
    )
    st.set_defaults(func=cmd_status)

    # ---- Day 16: DMN promoter ----
    promo = sub.add_parser(
        "promote",
        help=(
            "DMN promoter: classify surprise pairs via Haiku and "
            "promote/demote tripwires between active/shadow/archived "
            "based on composite fitness"
        ),
    )
    promo_sub = promo.add_subparsers(dest="promote_cmd", required=True)

    pc = promo_sub.add_parser(
        "classify",
        help="Classify unclassified surprise pairs via Haiku",
    )
    pc.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days of session history to scan (default: 7)",
    )
    pc.add_argument(
        "--batch-size",
        type=int,
        default=50,
        dest="batch_size",
        help=(
            "Max number of pairs to classify in one invocation "
            "(default: 50, hard cap: 200)"
        ),
    )
    pc.add_argument(
        "--model",
        default="claude-haiku-4-5",
        help="Anthropic model id (default: claude-haiku-4-5)",
    )
    pc.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview what would be classified without calling Haiku",
    )
    pc.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm when more than 10 pairs would be classified",
    )
    pc.set_defaults(func=cmd_promote_classify)

    pr = promo_sub.add_parser(
        "run",
        help=(
            "Compute promotion/demotion decisions from current fitness "
            "and (with --apply) mutate the store"
        ),
    )
    pr.add_argument(
        "--days",
        type=int,
        default=7,
        help="Fitness window in days (default: 7)",
    )
    pr.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually mutate the store. Without this flag the run is "
            "dry: decisions are printed but nothing is written."
        ),
    )
    pr.add_argument(
        "--session-id",
        default=None,
        dest="session_id",
        help=(
            "Session id to write the status_change audit events to. "
            "Defaults to 'promoter_run'."
        ),
    )
    pr.set_defaults(func=cmd_promote_run)

    pl = promo_sub.add_parser(
        "log",
        help="Show recent status_changes audit rows",
    )
    pl.add_argument(
        "--days",
        type=int,
        default=None,
        help="Window in days (default: all time)",
    )
    pl.set_defaults(func=cmd_promote_log)

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
