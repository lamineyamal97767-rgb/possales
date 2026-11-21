"""Migration script for Report 09: Customer Gifts.

Creates the application-owned ``customer_gifts`` table (gift records linked to
customers in ``cs``) and ensures the Customer Gifts menu points at
``/menus/customer-gifts``.

Usage:
    python -m db.migrations.migrate_customer_gifts --up
    python -m db.migrations.migrate_customer_gifts --down
    python -m db.migrations.migrate_customer_gifts --status
"""

from __future__ import annotations

import argparse
import sys

from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS customer_gifts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id VARCHAR(10) NOT NULL,
    gift_name VARCHAR(200) NOT NULL,
    gift_date DATE NOT NULL,
    note TEXT NULL,
    recorded_by_user_id INT NOT NULL,
    recorded_by_username VARCHAR(50) NOT NULL DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_customer_gifts_customer (customer_id),
    KEY idx_customer_gifts_date (gift_date),
    KEY idx_customer_gifts_recorded (recorded_by_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Existing menu rows carry url 'customer-gifts'; normalize to the page route.
MENU_UPDATE_SQL = """
UPDATE sidebar_menus
SET url = '/menus/customer-gifts', icon = 'gift', is_active = 1, is_disabled = 0
WHERE title = 'Customer Gifts';
"""

# Safety net for databases without a Customer Gifts menu row.
MENU_INSERT_SQL = """
INSERT INTO sidebar_menus (parent_id, title, subtitle, url, icon, section, menu_order, is_active, is_disabled, target_blank)
SELECT p.id, 'Customer Gifts', '', '/menus/customer-gifts', 'gift', 'Analytics Modules',
       COALESCE((SELECT MAX(m.menu_order) FROM sidebar_menus m WHERE m.parent_id = p.id), 0) + 1, 1, 0, 0
FROM sidebar_menus p
WHERE p.parent_id IS NULL AND p.title = 'Client Operations'
LIMIT 1;
"""

DOWN_SQL_STATEMENTS = [
    "DROP TABLE IF EXISTS customer_gifts",
    """
    UPDATE sidebar_menus
    SET url = 'customer-gifts', icon = 'gift', is_active = 1, is_disabled = 0
    WHERE title = 'Customer Gifts'
    """,
]


def migrate_up(conn=None) -> bool:
    """Run the additive migration to create customer_gifts and register the menu.

    The migration only creates the application-owned table, normalizes the
    existing Customer Gifts menu URL/icon, and inserts a menu row if missing.
    Existing menu IDs, grants and permission rows are left untouched.
    """

    def _execute(c):
        with c.cursor() as cur:
            cur.execute(UP_SQL)
            cur.execute(MENU_UPDATE_SQL)
            cur.execute("SELECT COUNT(*) AS cnt FROM sidebar_menus WHERE title = 'Customer Gifts'")
            menu_row = cur.fetchone()
            if not menu_row or int(menu_row.get("cnt") or 0) == 0:
                cur.execute(MENU_INSERT_SQL)
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
    """Roll back the Customer Gifts migration.

    Safe rollback instructions (run during a maintenance window):

    1. Back up recorded gifts first; dropping the table removes them permanently:
           mysqldump -u root -p print_db customer_gifts > customer_gifts_backup.sql
    2. Apply the rollback:
           python -m db.migrations.migrate_customer_gifts --down
    3. If the Customer Gifts menu row was inserted by this migration, remove it:
           DELETE FROM sidebar_menus WHERE title = 'Customer Gifts';
    4. Restore the backup (Windows PowerShell):
           cmd /c "mysql -u root -p print_db < customer_gifts_backup.sql"
    5. Re-run the migration with ``--up`` if the menu row must be re-added.
    """

    def _execute(c):
        with c.cursor() as cur:
            for statement in DOWN_SQL_STATEMENTS:
                cur.execute(statement)
            cur.execute("""
                SELECT COUNT(*) as cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'customer_gifts';
            """)
            row = cur.fetchone()
            return bool(row and (row.get("cnt") or 0) == 0)

    if conn is not None:
        _execute(conn)
        conn.commit()
        return True
    with get_connection() as c:
        ok = _execute(c)
        c.commit()
        return ok


def check_status(conn=None) -> dict:
    """Check status of the customer_gifts table and Customer Gifts menu row."""

    def _execute(c):
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'customer_gifts';
            """)
            row = cur.fetchone()
            exists = bool(row and (row.get("cnt") or 0) > 0)
            rows_count = 0
            if exists:
                cur.execute("SELECT COUNT(*) as cnt FROM customer_gifts;")
                r = cur.fetchone()
                rows_count = int(r.get("cnt") or 0) if r else 0
            cur.execute("SELECT COUNT(*) as cnt FROM sidebar_menus WHERE title = 'Customer Gifts';")
            menu_row = cur.fetchone()
            menu_count = int(menu_row.get("cnt") or 0) if menu_row else 0
            return {"exists": exists, "rows": rows_count, "menu_rows": menu_count}

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Customer Gifts Database Migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Apply additive migration (create table, fix menu URL)")
    group.add_argument("--down", action="store_true", help="Roll back migration (drop table, revert menu URL)")
    group.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    if args.up:
        if migrate_up():
            print("Migration UP SUCCESS: 'customer_gifts' table is ready.")
            sys.exit(0)
        print("Migration UP FAILED.")
        sys.exit(1)
    elif args.down:
        if migrate_down():
            print("Migration DOWN SUCCESS: 'customer_gifts' table dropped, menu URL reverted.")
            sys.exit(0)
        print("Migration DOWN FAILED.")
        sys.exit(1)
    else:
        st = check_status()
        print(f"Migration Status: Table Exists={st['exists']}, Gift Records={st['rows']}, Menu Rows={st['menu_rows']}")
        sys.exit(0)
