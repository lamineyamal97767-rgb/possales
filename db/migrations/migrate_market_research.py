"""Migration for Report 10: Market Discovery evidence storage.

Creates the application-owned tables used by ``market_research_service``:

- ``market_research_runs``
- ``market_research_observations``
- ``market_research_sources``
- ``market_research_usage``

The migration is additive and only touches application-owned tables. It never
modifies product, sales, stock or customer data.

Usage:
    python -m db.migrations.migrate_market_research --up
    python -m db.migrations.migrate_market_research --down
    python -m db.migrations.migrate_market_research --status
"""

from __future__ import annotations

import argparse
import sys

from db.connection import get_connection
from db.market_research import TABLES_SQL

TABLES = (
    "market_research_runs",
    "market_research_observations",
    "market_research_sources",
    "market_research_usage",
)

DOWN_STATEMENTS = [
    "DROP TABLE IF EXISTS market_research_usage",
    "DROP TABLE IF EXISTS market_research_sources",
    "DROP TABLE IF EXISTS market_research_observations",
    "DROP TABLE IF EXISTS market_research_runs",
]


def migrate_up(conn=None) -> bool:
    def _execute(c):
        with c.cursor() as cur:
            for statement in TABLES_SQL:
                cur.execute(statement)
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN (%s, %s, %s, %s)
                """,
                TABLES,
            )
            return int((cur.fetchone() or {}).get("cnt") or 0) == len(TABLES)

    if conn is not None:
        ok = _execute(conn)
        conn.commit()
        return ok
    with get_connection() as c:
        ok = _execute(c)
        c.commit()
        return ok


def migrate_down(conn=None) -> bool:
    def _execute(c):
        with c.cursor() as cur:
            for statement in DOWN_STATEMENTS:
                cur.execute(statement)
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN (%s, %s, %s, %s)
                """,
                TABLES,
            )
            return int((cur.fetchone() or {}).get("cnt") or 0) == 0

    if conn is not None:
        ok = _execute(conn)
        conn.commit()
        return ok
    with get_connection() as c:
        ok = _execute(c)
        c.commit()
        return ok


def check_status(conn=None) -> dict:
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN (%s, %s, %s, %s)
                """,
                TABLES,
            )
            existing = {row["TABLE_NAME"] for row in cur.fetchall()}
            counts = {}
            for table in TABLES:
                if table in existing:
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                    counts[table] = int((cur.fetchone() or {}).get("cnt") or 0)
                else:
                    counts[table] = None
            return {"existing": sorted(existing), "counts": counts}

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Research Database Migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Create market research tables")
    group.add_argument("--down", action="store_true", help="Drop market research tables")
    group.add_argument("--status", action="store_true", help="Check migration status")
    args = parser.parse_args()

    if args.up:
        if migrate_up():
            print("Migration UP SUCCESS: market research tables are ready.")
            sys.exit(0)
        print("Migration UP FAILED.")
        sys.exit(1)
    elif args.down:
        if migrate_down():
            print("Migration DOWN SUCCESS: market research tables dropped.")
            sys.exit(0)
        print("Migration DOWN FAILED.")
        sys.exit(1)
    else:
        status = check_status()
        print(f"Existing tables: {status['existing']}")
        print(f"Row counts: {status['counts']}")
        sys.exit(0)
