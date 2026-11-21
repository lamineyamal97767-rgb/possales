"""Migration for Stock-out Analysis.

Creates the application-owned ``stock_out_daily_snapshots`` table and places the
existing Stock-out Analysis menu under Product Intelligence with the page
route ``/menus/stock-out-analysis``.

The migration is additive and preserves the existing menu ID (33 in the current
database) and any permission grants in ``dashboard_auth.allowed_menus``.

Usage:
    python -m db.migrations.migrate_stock_out_analysis --up
    python -m db.migrations.migrate_stock_out_analysis --down
    python -m db.migrations.migrate_stock_out_analysis --status

Rollback safety:
    1. Back up daily snapshots before dropping the table:
           mysqldump -u root -p print_db stock_out_daily_snapshots > stock_out_snapshots_backup.sql
    2. Apply the rollback:
           python -m db.migrations.migrate_stock_out_analysis --down
    3. Restore the backup if the history must be retained:
           cmd /c "mysql -u root -p print_db < stock_out_snapshots_backup.sql"
    4. Re-run ``--up`` to recreate the table and menu route.

The migration never modifies product, sales, stock_non_serial or stock_serial
data.
"""

from __future__ import annotations

import argparse
import sys

from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS stock_out_daily_snapshots (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    product_id VARCHAR(10) NOT NULL,
    observation_date DATE NOT NULL,
    stock_qty FLOAT NULL,
    stock_status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    stock_source VARCHAR(20) NOT NULL DEFAULT 'non_serial',
    observed_at DATETIME NOT NULL,
    coverage_note VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_stock_out_product_day (product_id, observation_date),
    KEY idx_stock_out_snapshot_date (observation_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

STOCK_MENU_MATCH = """
    LOWER(title) = 'stock-out analysis'
    OR LOWER(title) = 'stock out analysis'
    OR LOWER(url) IN ('stock-out-analysis', '/menus/stock-out-analysis', 'stock_out_analysis')
"""

DOWN_STATEMENTS = [
    "DROP TABLE IF EXISTS stock_out_daily_snapshots",
]


def _ensure_product_intelligence_parent(cur) -> int:
    cur.execute("""
        SELECT id FROM sidebar_menus
        WHERE parent_id IS NULL AND title = 'Product Intelligence'
        ORDER BY id LIMIT 1
    """)
    row = cur.fetchone()
    if row and row.get("id"):
        return int(row["id"])
    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order,
             is_active, is_disabled, target_blank)
        VALUES (NULL, 'Product Intelligence', 'Core Analytics', '#', 'package',
                'Analytics Modules', 1, 1, 0, 0)
    """)
    return int(cur.lastrowid)


def _ensure_stock_out_menu(cur, parent_id: int) -> None:
    cur.execute(f"SELECT id FROM sidebar_menus WHERE {STOCK_MENU_MATCH} ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row and row.get("id"):
        menu_id = int(row["id"])
        cur.execute("""
            UPDATE sidebar_menus
            SET parent_id = %s,
                title = 'Stock-out Analysis',
                url = '/menus/stock-out-analysis',
                icon = 'package-x',
                section = 'Analytics Modules',
                menu_order = 5,
                is_active = 1,
                is_disabled = 0
            WHERE id = %s
        """, (parent_id, menu_id))
        return

    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order,
             is_active, is_disabled, target_blank)
        VALUES (%s, 'Stock-out Analysis', '', '/menus/stock-out-analysis',
                'package-x', 'Analytics Modules', 5, 1, 0, 0)
    """, (parent_id,))


def migrate_up(conn=None) -> bool:
    """Create the snapshot table and normalize the menu without changing IDs."""

    def _execute(c):
        with c.cursor() as cur:
            cur.execute(UP_SQL)
            parent_id = _ensure_product_intelligence_parent(cur)
            _ensure_stock_out_menu(cur, parent_id)
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'stock_out_daily_snapshots'
            """)
            table_row = cur.fetchone()
            if not table_row or int(table_row.get("cnt") or 0) == 0:
                raise RuntimeError("stock_out_daily_snapshots was not created.")
            return True

    if conn is not None:
        _execute(conn)
        conn.commit()
        return True
    with get_connection() as c:
        _execute(c)
        c.commit()
        return True


def migrate_down(conn=None) -> bool:
    """Drop the snapshot table and revert the menu to its pre-migration state."""

    def _execute(c):
        with c.cursor() as cur:
            for statement in DOWN_STATEMENTS:
                cur.execute(statement)
            cur.execute("""
                SELECT id FROM sidebar_menus
                WHERE parent_id IS NULL AND title = 'Client Operations'
                ORDER BY id LIMIT 1
            """)
            parent_row = cur.fetchone()
            client_ops_id = int(parent_row["id"]) if parent_row and parent_row.get("id") else None
            cur.execute(f"""
                UPDATE sidebar_menus
                SET parent_id = %s,
                    url = 'stock-out-analysis',
                    icon = 'package-x',
                    menu_order = 1,
                    is_active = 1,
                    is_disabled = 0
                WHERE {STOCK_MENU_MATCH}
            """, (client_ops_id,))
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'stock_out_daily_snapshots'
            """)
            row = cur.fetchone()
            return bool(row and int(row.get("cnt") or 0) == 0)

    if conn is not None:
        ok = _execute(conn)
        conn.commit()
        return ok
    with get_connection() as c:
        ok = _execute(c)
        c.commit()
        return ok


def check_status(conn=None) -> dict:
    """Return migration status for the table and menu."""

    def _execute(c):
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'stock_out_daily_snapshots'
            """)
            exists = int((cur.fetchone() or {}).get("cnt") or 0) > 0
            snapshot_rows = 0
            if exists:
                cur.execute("SELECT COUNT(*) AS cnt FROM stock_out_daily_snapshots")
                snapshot_rows = int((cur.fetchone() or {}).get("cnt") or 0)
            cur.execute(f"""
                SELECT id, parent_id, title, url, icon, is_active, is_disabled
                FROM sidebar_menus
                WHERE {STOCK_MENU_MATCH}
                ORDER BY id LIMIT 1
            """)
            menu = cur.fetchone()
            return {
                "exists": exists,
                "snapshot_rows": snapshot_rows,
                "menu": menu,
            }

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock-out Analysis Database Migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Create snapshot table and normalize menu")
    group.add_argument("--down", action="store_true", help="Drop snapshot table and revert menu")
    group.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    if args.up:
        if migrate_up():
            print("Migration UP SUCCESS: stock_out_daily_snapshots is ready and the menu is wired.")
            sys.exit(0)
        print("Migration UP FAILED.")
        sys.exit(1)
    elif args.down:
        if migrate_down():
            print("Migration DOWN SUCCESS: snapshot table dropped and menu reverted.")
            sys.exit(0)
        print("Migration DOWN FAILED.")
        sys.exit(1)
    else:
        status = check_status()
        menu = status.get("menu") or {}
        print(
            "Migration Status: "
            f"Table Exists={status['exists']}, Snapshot Rows={status['snapshot_rows']}, "
            f"Menu ID={menu.get('id')}, Parent ID={menu.get('parent_id')}, "
            f"URL={menu.get('url')}"
        )
        sys.exit(0)
