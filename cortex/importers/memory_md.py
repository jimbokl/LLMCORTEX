"""Seed the cortex store with example tripwires.

The seed below is a starter set distilled to the minimum needed to (1)
exercise every Cortex feature — keyword triggers, regex `violation_patterns`,
`affected_files` globs, `verify_cmd`, cost components, and synthesis — and
(2) deliver lessons that are broadly applicable across software projects:
production deploys, schema migrations, security logging, ML feature
pipelines, and shared-branch git hygiene.

Treat these as templates. After running `cortex migrate` once, replace
or extend this file with tripwires distilled from your own project's
post-mortems, RUNBOOKs, and CLAUDE.md / AGENTS.md notes. Re-running
`cortex migrate` is idempotent (upsert) and preserves accumulated
`violation_count` / `last_violated_at` stats on existing tripwires.
"""
from __future__ import annotations

from cortex.store import CortexStore

SEED_TRIPWIRES: list[dict] = [
    {
        "id": "secrets_in_logs",
        "title": "Never log secrets — tokens, keys, passwords end up in log aggregators forever",
        "severity": "critical",
        "domain": "security",
        "triggers": [
            "log", "logger", "logging", "print", "debug",
            "secret", "token", "password", "api_key", "credential",
            "authorization",
        ],
        "body": (
            "Never log secrets or token-like values. Once written to a log file, they propagate to\n"
            "log aggregators (Datadog, CloudWatch, ELK), backup systems, and anyone with read\n"
            "access to logs — including third-party SaaS vendors. Rotating the secret is the only\n"
            "fix, and every dependent service needs to be updated.\n"
            "\n"
            "Why: a single `print(request.headers)` or `logger.debug(f\"payload={data}\")` can dump\n"
            "Authorization headers, signed JWTs, OAuth refresh tokens, or DB passwords. Most\n"
            "incident reports name 'logs' as the leak source.\n"
            "\n"
            "How to apply: (1) Never f-string a request/response object into a log statement —\n"
            "explicitly select non-secret fields. (2) Use a structured logger with a redaction\n"
            "filter for known secret keys. (3) Before merging logging changes, search the diff for\n"
            "/token|key|secret|password|auth/i adjacent to /log|print/. (4) Rotate any secret you\n"
            "have ever logged — assume it is compromised."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "violation_patterns": [
            r"(logger|log|print)\s*[\.\(][^\)]{0,200}?\b(token|api_key|password|secret|credential|authorization)\b",
        ],
        # Intentionally NO `affected_files`: secrets-in-logs is a
        # keyword-driven warning. Globbing `*.py` would fire on any diff
        # that touches a Python file, which is far too broad. Rely on
        # the trigger words to surface this tripwire.
    },
    {
        "id": "migration_destructive",
        "title": "DROP COLUMN/TABLE in a migration is irreversible — use the two-deploy pattern",
        "severity": "critical",
        "domain": "database",
        "triggers": [
            "migration", "migrate", "drop", "alter", "schema",
            "column", "table", "ddl", "rollback",
        ],
        "body": (
            "DROP COLUMN, DROP TABLE, and DROP INDEX in a migration are irreversible — once the\n"
            "DDL runs in production, the data is gone unless restored from backup. Any deployment\n"
            "race between code that still reads the column and the migration that drops it causes\n"
            "production errors that look like 'column does not exist'.\n"
            "\n"
            "Why: a single combined deploy (drop column + remove all reads in same release)\n"
            "crashes the still-running old containers during rollout. The old code reads the\n"
            "column for the 30-90s the deploy takes; if the migration ran first, every request\n"
            "500s.\n"
            "\n"
            "How to apply: (1) Two-deploy pattern: deploy A removes all reads/writes. Deploy A is\n"
            "live and stable for at least one full release cycle. (2) Deploy B drops the column.\n"
            "(3) Never combine. (4) For critical tables, capture a manual snapshot before B.\n"
            "(5) Test the migration against a copy of prod data, not just an empty staging\n"
            "schema."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "violation_patterns": [
            r"\b(DROP\s+(COLUMN|TABLE|INDEX))\b",
        ],
        "affected_files": [
            "*migration*.py", "*migration*.sql",
            "migrations/*.py", "migrations/*.sql",
            "alembic/versions/*.py",
        ],
    },
    {
        "id": "lookahead_parquet",
        "title": "Features must be computable from data strictly before the decision time",
        "severity": "critical",
        "domain": "data",
        "triggers": [
            "backtest", "parquet", "features", "slot", "lookahead",
            "replay", "walkforward", "detector", "training", "label",
        ],
        "body": (
            "A feature is HONEST only if computable from data with timestamps strictly BEFORE the\n"
            "decision time. The classic bug is binning by `slot_ts = (ts // N) * N`, which floors\n"
            "the bar OPEN time — so any computed value (return, volume, ratio) covers the window\n"
            "AFTER slot_ts. The training labels are the future of the feature. Pure lookahead.\n"
            "\n"
            "Why: backtests with this bug routinely show near-100% accuracy on >100 trades. The\n"
            "model deploys, real-time inference uses honest data, accuracy collapses to ~50%.\n"
            "Walk-forward validation does not catch it — the hold-out has the same bug.\n"
            "\n"
            "How to apply: (1) Before any backtest, run a SHIFT TEST — replay with the feature\n"
            "shifted back one decision boundary. If accuracy drops >10pp, it contains lookahead.\n"
            "(2) 100% accuracy on >100 samples is ALWAYS suspect — first hypothesis must be\n"
            "lookahead, not 'edge so strong it is perfect'. (3) For windowed features, the slot's\n"
            "decision time must be slot_ts + N, never slot_ts. (4) Wire the standalone verifier\n"
            "into CI: `cortex-check-lookahead --features-dir <path>`."
        ),
        "verify_cmd": "cortex-check-lookahead --features-dir features",
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "violation_patterns": [
            r"slot_ts[^\n]*?=[^\n]*?//\s*\d+[^\n]*?\*\s*\d+\b(?!\s*\+)",
        ],
        "affected_files": [
            "*features*.py", "*feature*.py", "*_parquet.py",
            "*prepare.py", "*prepare_*.py", "*slot*.py",
        ],
    },
    {
        "id": "backtest_must_match_prod",
        "title": "Staging / backtest config MUST match production — caps, limits, timeouts",
        "severity": "critical",
        "domain": "infra",
        "triggers": [
            "backtest", "prod", "production", "config", "live", "validate",
            "cap", "risk", "position", "limit", "timeout", "staging",
        ],
        "body": (
            "Every staging or backtest evaluation MUST use the EXACT production parameters —\n"
            "request rate limits, position caps, timeout values, retry counts, batch sizes. Never\n"
            "silently change a cap between staging and prod, or between a backtest and the live\n"
            "deployment.\n"
            "\n"
            "Why: a staging system with rate_limit=10000 looks fast and stable. Prod has\n"
            "rate_limit=100 because the upstream provider charges per call. The release that\n"
            "ships hits the prod cap immediately and cascades into 429s and dropped traffic. The\n"
            "user makes financial / business decisions on these numbers; inflation is dangerous\n"
            "and dishonest.\n"
            "\n"
            "How to apply: (1) Before ANY backtest or sign-off, READ the prod config first. Use\n"
            "EXACT same values for caps, retries, timeouts, and ALL other params. (2) If testing\n"
            "alternative configs, label them clearly 'PROD' vs 'HYPOTHETICAL'. (3) When a\n"
            "backtest reports a number, attach the config used; reject reports that do not show\n"
            "the same caps as prod. (4) Bind both environments to the same canonical config and\n"
            "use environment overrides only for credentials and host names."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "affected_files": [
            "*backtest*.py", "replay_*.py", "*_replay.py",
            "config.py", "*config*.yaml", "*config*.yml", "*config*.toml",
        ],
    },
    {
        "id": "never_single_strategy",
        "title": "Never deploy a single dependency on a critical path — SPOFs erase redundancy",
        "severity": "critical",
        "domain": "infra",
        "triggers": [
            "live", "deploy", "bot", "portfolio", "single", "diversify",
            "dependency", "spof", "redundancy", "fallback", "primary",
        ],
        "body": (
            "Critical paths must not depend on a single provider, replica, or service instance.\n"
            "One outage of the SPOF erases your service. This is the first thing a post-mortem\n"
            "uncovers and the last thing the original architect was thinking about.\n"
            "\n"
            "Why: the team validated that each individual dependency met its uptime SLA, then\n"
            "wired them sequentially. Joint reliability of N dependencies is the product of\n"
            "individual SLAs — three 99.9% deps wired in series is 99.7%, three hours of\n"
            "downtime per month, not the marketed 43 minutes per dep.\n"
            "\n"
            "How to apply: (1) For each critical path, name the failure of each dependency and\n"
            "the recovery action. If any answer is 'manual restore', the path is fragile.\n"
            "(2) Use circuit breakers + idempotent retries for transient failures. (3) Cache the\n"
            "last good response on read paths so a brief upstream outage degrades rather than\n"
            "500s. (4) Where the same logical decision rides on a single signal, replicate the\n"
            "signal — at least one independent fallback before going live."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
    },
    {
        "id": "no_budget_paper",
        "title": "Never apply prod caps to evaluation / paper / shadow runs — survivorship bias",
        "severity": "high",
        "domain": "data",
        "triggers": [
            "paper", "paper-trading", "shadow", "evaluation", "budget",
            "max_concurrent", "config", "cap", "slot_cost", "survivorship",
        ],
        "body": (
            "NEVER apply production caps (rate limits, concurrency limits, slot costs, daily\n"
            "max-trade caps) to evaluation, paper, or shadow runs. A capped paper run produces\n"
            "poisoned evaluation data — survivorship bias in reverse, where fast-firing entries\n"
            "consume the budget and starve slow-but-better candidates.\n"
            "\n"
            "Why: an evaluation pipeline with `max_concurrent=N` matching prod is testing the\n"
            "wrong distribution. Slow-but-higher-edge candidates get systematically blocked when\n"
            "the cap is hit; the unblocked candidates are not the highest-quality, just the\n"
            "fastest. Backfill of blocked items often shows higher win rate than those that got\n"
            "through.\n"
            "\n"
            "How to apply: paper / evaluation / shadow config MUST have caps set high enough to\n"
            "be effectively unbounded. The entire purpose of these runs is data collection; caps\n"
            "only belong in live. If a paper report shows 'budget exceeded N times', re-run\n"
            "without caps before interpreting any PnL or accuracy number."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
    },
    {
        "id": "auth_token_in_url",
        "title": "Auth tokens in URL query strings leak via access logs and Referer headers",
        "severity": "critical",
        "domain": "security",
        "triggers": [
            "url", "query", "querystring", "param", "auth", "token",
            "header", "redirect", "jwt", "oauth",
        ],
        "body": (
            "Authentication tokens, API keys, and signed URLs in query strings get logged by\n"
            "every reverse proxy, CDN edge, and access-log aggregator on the request path. They\n"
            "also appear in the browser Referer header on every cross-origin redirect, leaking\n"
            "to third-party services the user did not intend to share auth with.\n"
            "\n"
            "Why: a single redirect from your authenticated page to an external image CDN sends\n"
            "the token in the Referer to the CDN's access log. Standard log retention is 30-90\n"
            "days; the CDN may persist longer.\n"
            "\n"
            "How to apply: (1) Pass tokens in the Authorization header, never the URL.\n"
            "(2) Signed URLs for short-lived asset access are OK if they cannot be used for\n"
            "full-account auth. (3) Add `rel=\"noreferrer\"` on links from authenticated pages\n"
            "to external domains. (4) On any redirect chain that exits your origin, strip query\n"
            "params."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "violation_patterns": [
            r"\?[^\"'\s]*\b(token|api_key|access_token|auth)=",
        ],
    },
    {
        "id": "feature_flag_default_off",
        "title": "New feature flags default to OFF — never ship code that defaults to ON",
        "severity": "high",
        "domain": "deploy",
        "triggers": [
            "flag", "feature", "rollout", "toggle", "release", "default",
            "killswitch", "experiment",
        ],
        "body": (
            "A new feature flag must default to OFF on first deploy. The PR that adds the flag\n"
            "should ship the new code path behind a flag whose default is `False`. Turning the\n"
            "flag on is a separate, reversible action with its own audit trail.\n"
            "\n"
            "Why: defaulting a flag to ON in the same PR that adds it gives you no rollback\n"
            "without a code revert. The whole point of flags is to decouple code release from\n"
            "feature release. A flag whose default is true is just ordinary code with extra\n"
            "indirection.\n"
            "\n"
            "How to apply: (1) On flag creation, default = False. (2) Roll out by flipping the\n"
            "flag in the flag service, not by editing code. (3) After the flag has been 100% on\n"
            "for one release cycle, remove the flag and the old code path in a follow-up PR.\n"
            "(4) Never default a feature flag to True 'because we are sure'. The cost of being\n"
            "wrong is a code revert; the cost of being right is one extra flag flip."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
    },
    {
        "id": "force_push_main_blocked",
        "title": "Never force-push main / master — it overwrites teammates' commits silently",
        "severity": "critical",
        "domain": "git",
        "triggers": [
            "force", "push", "force-push", "main", "master", "rewrite",
            "rebase", "git", "history",
        ],
        "body": (
            "`git push --force` on a shared branch (main, master, develop, release-*) discards\n"
            "any commits your local does not have. If a teammate pushed in the last few minutes,\n"
            "their commit is silently destroyed. Recovery requires reflog from another\n"
            "developer's machine — which may not exist.\n"
            "\n"
            "Why: even with `--force-with-lease`, a stale fetch can still cause data loss if\n"
            "another commit lands during your push. Branch protection rules on the remote are\n"
            "the only durable guard.\n"
            "\n"
            "How to apply: (1) Configure branch protection on main and master to disallow force\n"
            "pushes. (2) For local fixups, prefer `git revert` over `git reset` + force-push.\n"
            "(3) If you must rewrite history, push to a renamed branch and open a PR — never\n"
            "overwrite the canonical branch. (4) Add a pre-push hook that refuses force-push to\n"
            "the main and master refs."
        ),
        "verify_cmd": None,
        "cost_usd": 0.0,
        "source_file": "example_seed",
        "violation_patterns": [
            r"git\s+push\s+(--force\b|-f\b)(?:[^\n]{0,80})?\b(main|master|develop|release)\b",
        ],
    },
]


# Cost components are tied to specific tripwires and contribute to the
# synthesis sum below. Units (`pts` here) are arbitrary — pick whatever
# makes the message read naturally for your domain (basis points,
# minutes of downtime, dollars, latency ms, …).
SEED_COST_COMPONENTS: list[dict] = [
    {
        "id": "deploy_no_ff_risk",
        "tripwire_id": "feature_flag_default_off",
        "metric": "rollout_risk",
        "value": 3.0,
        "unit": "pts",
        "sign": "drag",
    },
    {
        "id": "deploy_staging_drift_risk",
        "tripwire_id": "backtest_must_match_prod",
        "metric": "staging_drift_risk",
        "value": 2.0,
        "unit": "pts",
        "sign": "drag",
    },
    {
        "id": "deploy_spof_risk",
        "tripwire_id": "never_single_strategy",
        "metric": "spof_risk",
        "value": 4.0,
        "unit": "pts",
        "sign": "drag",
    },
]


SEED_SYNTHESIS_RULES: list[dict] = [
    {
        "id": "prod_deploy_unsafe",
        "triggers": ["deploy", "release", "ship", "rollout", "prod"],
        "sum_over": [
            "deploy_no_ff_risk",
            "deploy_staging_drift_risk",
            "deploy_spof_risk",
        ],
        "threshold": 5.0,
        "op": "gte",
        "message": (
            "Sum unmitigated deploy risk = {sum} pts ({n} components) >= {threshold} pts floor. "
            "Each component is one of: feature flag default-on, staging-prod config drift, "
            "single point of failure on a critical dependency. Resolve each before shipping. "
            "See feature_flag_default_off for the safer default and "
            "backtest_must_match_prod / never_single_strategy for the supporting practices."
        ),
    },
]


def run_migration(db_path: str = ".cortex/store.db") -> int:
    """Seed the store with tripwires + cost components + synthesis rules.

    Returns the number of tripwires migrated. Idempotent: re-running preserves
    accumulated violation stats, overwrites body/triggers/cost with latest values.
    """
    store = CortexStore(db_path)
    try:
        for tw in SEED_TRIPWIRES:
            store.add_tripwire(**tw)
        for cc in SEED_COST_COMPONENTS:
            store.add_cost_component(**cc)
        for rule in SEED_SYNTHESIS_RULES:
            store.add_synthesis_rule(**rule)
        return len(SEED_TRIPWIRES)
    finally:
        store.close()


if __name__ == "__main__":
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else ".cortex/store.db"
    n = run_migration(db)
    print(f"Migrated {n} tripwires to {db}")
