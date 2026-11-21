"""Migration for Report 11: Equivalent Models.

Creates the application-owned ``product_equivalent_models`` table and places
the existing Equivalent Models menu under Product Intelligence with the page
route ``/equivalent-models``. The migration is additive and preserves the
existing menu ID (34 in the current database) and any permission grants in
``dashboard_auth.allowed_menus``.

The table stores explicit, directional alternative-product recommendations.
It never infers equivalence from product names or categories, never creates a
reverse relationship automatically, and never changes product, stock, sales or
Ink Dependency data.

Usage:
    python -m db.migrations.migrate_equivalent_models --up
    python -m db.migrations.migrate_equivalent_models --down
    python -m db.migrations.migrate_equivalent_models --status

Rollback safety:
    1. Back up the relationship evidence before dropping the table:
           mysqldump -u root -p print_db product_equivalent_models > equivalent_models_backup.sql
    2. Apply the rollback:
           python -m db.migrations.migrate_equivalent_models --down
    3. Restore the evidence if it must be retained:
           cmd /c "mysql -u root -p print_db < equivalent_models_backup.sql"
    4. Re-run ``--up`` to recreate the table and normalize the menu route.

The rollback removes only the application-owned table. It intentionally leaves
the sidebar menu in place, because the menu pre-existed this feature in the live
database and may carry grants in ``dashboard_auth.allowed_menus``.
"""

from __future__ import annotations

import argparse
import sys

from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS product_equivalent_models (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_product_id VARCHAR(10) NOT NULL,
    alternative_product_id VARCHAR(10) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'needs_review',
    reason TEXT NOT NULL,
    source VARCHAR(200) NOT NULL DEFAULT 'Admin entry',
    verified_by VARCHAR(50) NULL,
    verified_at DATETIME NULL,
    created_by VARCHAR(50) DEFAULT 'admin',
    updated_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_equivalent_directional (source_product_id, alternative_product_id),
    KEY idx_equivalent_source_status (source_product_id, status),
    KEY idx_equivalent_alternative_status (alternative_product_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

EQUIVALENT_MENU_MATCH = """
    LOWER(title) IN ('equivalent models', 'equivalent model')
    OR LOWER(url) IN ('equivalent-models', '/equivalent-models', 'equivalent_models')
"""


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


def _ensure_equivalent_models_menu(cur, parent_id: int) -> None:
    cur.execute(f"SELECT id FROM sidebar_menus WHERE {EQUIVALENT_MENU_MATCH} ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row and row.get("id"):
        cur.execute("""
            UPDATE sidebar_menus
            SET parent_id = %s,
                title = 'Equivalent Models',
                url = '/equivalent-models',
                icon = 'repeat',
                section = 'Analytics Modules',
                menu_order = 6,
                is_active = 1,
                is_disabled = 0
            WHERE id = %s
        """, (parent_id, int(row["id"])))
        return

    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order,
             is_active, is_disabled, target_blank)
        VALUES (%s, 'Equivalent Models', '', '/equivalent-models',
                'repeat', 'Analytics Modules', 6, 1, 0, 0)
    """, (parent_id,))


def _table_exists(cur) -> bool:
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'product_equivalent_models'
    """)
    return int((cur.fetchone() or {}).get("cnt") or 0) > 0


def _menu_status(cur):
    cur.execute(f"""
        SELECT id, parent_id, title, url, icon, menu_order, is_active, is_disabled
        FROM sidebar_menus
        WHERE {EQUIVALENT_MENU_MATCH}
        ORDER BY id LIMIT 1
    """)
    return cur.fetchone()


def migrate_up(conn=None) -> bool:
    """Create the table and normalize the menu without changing its ID."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            cur.execute(UP_SQL)
            parent_id = _ensure_product_intelligence_parent(cur)
            _ensure_equivalent_models_menu(cur, parent_id)
        connection.commit()
        return True
    finally:
        if own_connection:
            connection.close()


def migrate_down(conn=None) -> bool:
    """Drop the application-owned table; keep the pre-existing menu."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS product_equivalent_models")
        connection.commit()
        return True
    finally:
        if own_connection:
            connection.close()


def migration_status(conn=None):
    """Return table and menu status for operational checks."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            return {
                "table_exists": _table_exists(cur),
                "menu": _menu_status(cur),
            }
    finally:
        if own_connection:
            connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Equivalent Models migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Create the table and normalize the menu")
    group.add_argument("--down", action="store_true", help="Drop the table")
    group.add_argument("--status", action="store_true", help="Show table and menu status")
    args = parser.parse_args(argv)

    try:
        if args.up:
            migrate_up()
            print("Equivalent Models migration applied.")
        elif args.down:
            migrate_down()
            print("Equivalent Models table dropped. Menu retained.")
        else:
            status = migration_status()
            print(f"Table exists: {status['table_exists']}")
            print(f"Menu: {status['menu']}")
        return 0
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
