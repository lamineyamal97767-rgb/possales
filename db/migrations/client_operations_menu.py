"""Shared helper for migrations that place a report under the Client Operations menu.

An existing menu row (matched by title or URL) is updated in place so its ID
and every grant in ``dashboard_auth.allowed_menus`` survive; a row is inserted
only when the menu is genuinely missing. No other menu row is changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from db.connection import get_connection


@dataclass(frozen=True)
class SubmenuSpec:
    title: str
    url: str
    icon: str
    match_titles: Tuple[str, ...]
    match_urls: Tuple[str, ...]

    def match_sql(self) -> Tuple[str, Tuple[str, ...]]:
        titles = ", ".join(["%s"] * len(self.match_titles))
        urls = ", ".join(["%s"] * len(self.match_urls))
        return f"LOWER(title) IN ({titles}) OR LOWER(url) IN ({urls})", self.match_titles + self.match_urls


def _ensure_client_operations_parent(cur) -> int:
    cur.execute("""
        SELECT id FROM sidebar_menus
        WHERE (parent_id IS NULL OR parent_id = 0) AND title = 'Client Operations'
        ORDER BY id LIMIT 1
    """)
    row = cur.fetchone()
    if row and row.get("id"):
        return int(row["id"])
    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order, is_active, is_disabled, target_blank)
        VALUES (NULL, 'Client Operations', '', '#', 'users', 'Analytics Modules', 2, 1, 0, 0)
    """)
    return int(cur.lastrowid)


def _ensure_submenu(cur, spec: SubmenuSpec, parent_id: int) -> None:
    where, params = spec.match_sql()
    cur.execute(f"SELECT id FROM sidebar_menus WHERE {where} ORDER BY id LIMIT 1", params)
    row = cur.fetchone()
    if row and row.get("id"):
        cur.execute("""
            UPDATE sidebar_menus
            SET parent_id = %s, title = %s, url = %s, icon = %s, is_active = 1, is_disabled = 0
            WHERE id = %s
        """, (parent_id, spec.title, spec.url, spec.icon, int(row["id"])))
        return
    cur.execute("""
        INSERT INTO sidebar_menus
            (parent_id, title, subtitle, url, icon, section, menu_order, is_active, is_disabled, target_blank)
        VALUES (%s, %s, '', %s, %s, 'Analytics Modules', 1, 1, 0, 0)
    """, (parent_id, spec.title, spec.url, spec.icon))


def migrate_submenu_up(spec: SubmenuSpec, conn=None) -> bool:
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            _ensure_submenu(cur, spec, _ensure_client_operations_parent(cur))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        if own_connection:
            connection.close()


def submenu_status(spec: SubmenuSpec, conn=None) -> Dict[str, Optional[Any]]:
    own_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cur:
            where, params = spec.match_sql()
            cur.execute(f"""
                SELECT id, parent_id, title, url, icon, menu_order, is_active, is_disabled
                FROM sidebar_menus WHERE {where} ORDER BY id LIMIT 1
            """, params)
            return {"table_exists": False, "menu": cur.fetchone()}
    finally:
        if own_connection:
            connection.close()


def run_cli(spec: SubmenuSpec, argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=f"{spec.title} menu migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Normalize the menu entry")
    group.add_argument("--down", action="store_true", help="No-op rollback (menu retained)")
    group.add_argument("--status", action="store_true", help="Show menu status")
    args = parser.parse_args(argv)
    try:
        if args.up:
            migrate_submenu_up(spec)
            print(f"{spec.title} menu normalized.")
        elif args.down:
            print(f"{spec.title} migration rolled back (no-op). Menu retained.")
        else:
            print(f"Menu: {submenu_status(spec)['menu']}")
        return 0
    except Exception as exc:
        print(f"Migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
