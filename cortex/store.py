"""Cortex store — SQLite-backed persistence for tripwires, cost components,
synthesis rules, and violations.

The store is the single source of truth. Schema is stable, diffable in git, and
versioned inline with idempotent DDL. No ORM, no migration framework.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tripwires (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    domain              TEXT NOT NULL,
    triggers            TEXT NOT NULL,        -- JSON array of strings
    body                TEXT NOT NULL,
    verify_cmd          TEXT,
    cost_usd            REAL NOT NULL DEFAULT 0,
    born_at             TEXT NOT NULL,
    last_violated_at    TEXT,
    violation_count     INTEGER NOT NULL DEFAULT 0,
    source_file         TEXT,
    violation_patterns  TEXT                   -- JSON array of regex strings (Day 6)
);

CREATE INDEX IF NOT EXISTS idx_tripwires_domain ON tripwires(domain);
CREATE INDEX IF NOT EXISTS idx_tripwires_severity ON tripwires(severity);

CREATE TABLE IF NOT EXISTS cost_components (
    id           TEXT PRIMARY KEY,
    tripwire_id  TEXT NOT NULL REFERENCES tripwires(id) ON DELETE CASCADE,
    metric       TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT NOT NULL,
    sign         TEXT NOT NULL CHECK (sign IN ('drag','boost'))
);

CREATE INDEX IF NOT EXISTS idx_cost_tripwire ON cost_components(tripwire_id);

CREATE TABLE IF NOT EXISTS synthesis_rules (
    id           TEXT PRIMARY KEY,
    triggers     TEXT NOT NULL,            -- JSON array
    sum_over     TEXT NOT NULL,            -- JSON array of cost_component ids
    threshold    REAL NOT NULL,
    op           TEXT NOT NULL CHECK (op IN ('gte','lte','gt','lt')),
    message      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tripwire_id  TEXT NOT NULL REFERENCES tripwires(id) ON DELETE CASCADE,
    session_id   TEXT,
    at           TEXT NOT NULL,
    evidence     TEXT
);

CREATE INDEX IF NOT EXISTS idx_violations_tripwire ON violations(tripwire_id);
CREATE INDEX IF NOT EXISTS idx_violations_at ON violations(at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_tripwire(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["triggers"] = json.loads(d["triggers"]) if d["triggers"] else []
    raw_patterns = d.get("violation_patterns")
    d["violation_patterns"] = json.loads(raw_patterns) if raw_patterns else []
    return d


class CortexStore:
    """SQLite-backed store. Thread-unsafe — use one instance per thread."""

    def __init__(self, db_path: str | Path = ".cortex/store.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self._migrate_schema()
            row = self.conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )

    def _migrate_schema(self) -> None:
        """Apply forward-compatible schema deltas for stores created by
        older versions of this module. Each delta is idempotent."""
        # Day 6: add tripwires.violation_patterns column if missing
        cur = self.conn.execute("PRAGMA table_info(tripwires)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "violation_patterns" not in existing_cols:
            self.conn.execute(
                "ALTER TABLE tripwires ADD COLUMN violation_patterns TEXT"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> CortexStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- tripwires ----

    def add_tripwire(
        self,
        *,
        id: str,
        title: str,
        severity: str,
        domain: str,
        triggers: list[str],
        body: str,
        verify_cmd: str | None = None,
        cost_usd: float = 0.0,
        source_file: str | None = None,
        violation_patterns: list[str] | None = None,
    ) -> None:
        """Insert or update a tripwire.

        Upsert preserves `born_at`, `violation_count`, and `last_violated_at`
        across re-imports so that a `cortex migrate` rerun never clobbers
        accumulated stats.
        """
        patterns_json = (
            json.dumps(violation_patterns) if violation_patterns else None
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tripwires
                    (id, title, severity, domain, triggers, body,
                     verify_cmd, cost_usd, born_at, source_file,
                     violation_patterns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title              = excluded.title,
                    severity           = excluded.severity,
                    domain             = excluded.domain,
                    triggers           = excluded.triggers,
                    body               = excluded.body,
                    verify_cmd         = excluded.verify_cmd,
                    cost_usd           = excluded.cost_usd,
                    source_file        = excluded.source_file,
                    violation_patterns = excluded.violation_patterns
                """,
                (
                    id, title, severity, domain,
                    json.dumps(triggers),
                    body, verify_cmd, cost_usd, _now_iso(), source_file,
                    patterns_json,
                ),
            )

    def get_tripwire(self, tripwire_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tripwires WHERE id = ?", (tripwire_id,)
        ).fetchone()
        return _row_to_tripwire(row) if row else None

    def list_tripwires(
        self,
        *,
        domain: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tripwires"
        where: list[str] = []
        params: list[Any] = []
        if domain:
            where.append("domain = ?")
            params.append(domain)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY CASE severity"
            " WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
            " WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, id"
        )
        return [_row_to_tripwire(r) for r in self.conn.execute(sql, params).fetchall()]

    def find_by_triggers(self, words: list[str]) -> list[dict[str, Any]]:
        """Return tripwires whose triggers intersect the given words (case-insensitive)."""
        words_lower = {w.lower() for w in words}
        hits: list[dict[str, Any]] = []
        for row in self.conn.execute("SELECT * FROM tripwires").fetchall():
            tw = _row_to_tripwire(row)
            if any(t.lower() in words_lower for t in tw["triggers"]):
                hits.append(tw)
        return hits

    def delete_tripwire(self, tripwire_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM tripwires WHERE id = ?", (tripwire_id,)
            )
        return cur.rowcount > 0

    # ---- cost components ----

    def add_cost_component(
        self,
        *,
        id: str,
        tripwire_id: str,
        metric: str,
        value: float,
        unit: str,
        sign: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO cost_components
                    (id, tripwire_id, metric, value, unit, sign)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (id, tripwire_id, metric, value, unit, sign),
            )

    def list_cost_components(self, tripwire_id: str | None = None) -> list[dict[str, Any]]:
        if tripwire_id:
            rows = self.conn.execute(
                "SELECT * FROM cost_components WHERE tripwire_id = ?",
                (tripwire_id,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM cost_components").fetchall()
        return [dict(r) for r in rows]

    # ---- synthesis rules ----

    def add_synthesis_rule(
        self,
        *,
        id: str,
        triggers: list[str],
        sum_over: list[str],
        threshold: float,
        op: str,
        message: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO synthesis_rules
                    (id, triggers, sum_over, threshold, op, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    id,
                    json.dumps(triggers),
                    json.dumps(sum_over),
                    threshold,
                    op,
                    message,
                ),
            )

    def get_synthesis_rule(self, rule_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM synthesis_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["triggers"] = json.loads(d["triggers"]) if d["triggers"] else []
        d["sum_over"] = json.loads(d["sum_over"]) if d["sum_over"] else []
        return d

    def list_synthesis_rules(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM synthesis_rules").fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["triggers"] = json.loads(d["triggers"]) if d["triggers"] else []
            d["sum_over"] = json.loads(d["sum_over"]) if d["sum_over"] else []
            out.append(d)
        return out

    # ---- violations ----

    def record_violation(
        self,
        *,
        tripwire_id: str,
        session_id: str | None = None,
        evidence: str | None = None,
    ) -> int:
        now = _now_iso()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO violations (tripwire_id, session_id, at, evidence)
                VALUES (?, ?, ?, ?)
                """,
                (tripwire_id, session_id, now, evidence),
            )
            self.conn.execute(
                """
                UPDATE tripwires
                SET violation_count = violation_count + 1,
                    last_violated_at = ?
                WHERE id = ?
                """,
                (now, tripwire_id),
            )
            return int(cur.lastrowid or 0)

    def list_violations(self, tripwire_id: str | None = None) -> list[dict[str, Any]]:
        if tripwire_id:
            rows = self.conn.execute(
                "SELECT * FROM violations WHERE tripwire_id = ? ORDER BY at DESC",
                (tripwire_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM violations ORDER BY at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- stats ----

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) FROM tripwires").fetchone()[0]
        total_viol = self.conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
        by_sev = self.conn.execute(
            """
            SELECT severity,
                   COUNT(*) AS n,
                   COALESCE(SUM(cost_usd), 0) AS cost,
                   COALESCE(SUM(violation_count), 0) AS violations
            FROM tripwires
            GROUP BY severity
            """
        ).fetchall()
        by_domain = self.conn.execute(
            "SELECT domain, COUNT(*) AS n FROM tripwires GROUP BY domain"
        ).fetchall()
        return {
            "total_tripwires": total,
            "total_violations": total_viol,
            "by_severity": {r["severity"]: dict(r) for r in by_sev},
            "by_domain": {r["domain"]: dict(r) for r in by_domain},
        }
