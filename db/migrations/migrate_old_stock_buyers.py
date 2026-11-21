"""Migration for Report 12: Old Stock & Buyers.

This migration places the existing Old Stock & Buyers menu under Product
Intelligence with the page route ``/old-stock-buyers``. The migration is
additive and preserves the existing menu ID (35 in the current database) and
any permission grants in ``dashboard_auth.allowed_menus``.

The report does not create an application-owned table. It reads directly
from the existing stock tables (``stock_serial`` and ``stock_non_serial``),
the product catalog, and the sales records. The migration therefore only
normalizes the sidebar menu entry.

Usage:
    python -m db.migrations.migrate_old_stock_buyers --up
    python -m db.migrations.migrate_old_stock_buyers --down
    python -m db.migrations.migrate_old_stock_buyers --status

Rollback safety:
    The rollback intentionally leaves the sidebar menu in place, because the
    menu pre-existed this feature in the live database and may carry grants in
    ``dashboard_auth.allowed_menus``. There is no application-owned table to
    drop.
"""

from __future__ import annotations

import argparse
import sys

from db.connection import get_connection

OLD_STOCK_BUYERS_MENU_MATCH = """
    LOWER(title) IN ('old stock & buyers', 'old stock and buyers', 'old stock buyers')
    OR LOWER(url) IN ('old-stock-buyers', '/old-stock-buyers', 'old_stock_buyers')
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


def _ensure_old_stock_buyers_menu(cur, parent_id: int) -> None:
    cur.execute(f"SELECT id FROM sidebar_menus WHERE {OLD_STOCK_BUYERS_MENU_MATCH} ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row and row.get("id"):
        cur.execute("""
            UPDATE sidebar_menus
            SET parent_id = %s,
                title = 'Old Stock & Buyers',
                url = '/old-stock-buyers',
                icon = 'package-search',
                section = 'Analytics Modules',
                menu_order = 7,
                is_active = 1,
                is_disabled = 0
            WHERE id = %s
        """, (parent_id, int(row["id"])))
        return

    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order,
             is_active, is_disabled, target_blank)
        VALUES (%s, 'Old Stock & Buyers', '', '/old-stock-buyers',
                'package-search', 'Analytics Modules', 7, 1, 0, 0)
    """, (parent_id,))


def _menu_status(cur):
    cur.execute(f"""
        SELECT id, parent_id, title, url, icon, menu_order, is_active, is_disabled
        FROM sidebar_menus
        WHERE {OLD_STOCK_BUYERS_MENU_MATCH}
        ORDER BY id LIMIT 1
    """)
    return cur.fetchone()


def migrate_up(conn=None) -> bool:
    """Normalize the Old Stock & Buyers menu under Product Intelligence."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            parent_id = _ensure_product_intelligence_parent(cur)
            _ensure_old_stock_buyers_menu(cur, parent_id)
        connection.commit()
        return True
    finally:
        if own_connection:
            connection.close()


def migrate_down(conn=None) -> bool:
    """No-op rollback: no application-owned table to drop; menu is retained."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        # Intentionally leave the menu in place so existing grants survive.
        return True
    finally:
        if own_connection:
            connection.close()


def migration_status(conn=None):
    """Return menu status for operational checks."""
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            return {
                "table_exists": False,
                "menu": _menu_status(cur),
            }
    finally:
        if own_connection:
            connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Old Stock & Buyers migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Normalize the menu entry")
    group.add_argument("--down", action="store_true", help="No-op rollback (menu retained)")
    group.add_argument("--status", action="store_true", help="Show menu status")
    args = parser.parse_args(argv)

    try:
        if args.up:
            migrate_up()
            print("Old Stock & Buyers menu normalized.")
        elif args.down:
            migrate_down()
            print("Old Stock & Buyers migration rolled back (no-op). Menu retained.")
        else:
            status = migration_status()
            print(f"Menu: {status['menu']}")
        return 0
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())