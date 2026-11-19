"""
Migration script for Report 03: Printer <-> Ink Dependency Mapping.
Usage:
    python -m db.migrations.migrate_printer_ink --up
    python -m db.migrations.migrate_printer_ink --down
    python -m db.migrations.migrate_printer_ink --status
"""

import sys
import argparse
from typing import Optional
from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS printer_ink_compatibility (
    id INT AUTO_INCREMENT PRIMARY KEY,
    printer_id VARCHAR(10) NOT NULL,
    ink_id VARCHAR(10) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    source VARCHAR(100) NOT NULL DEFAULT 'Admin confirmed',
    notes TEXT NULL,
    created_by VARCHAR(50) DEFAULT 'admin',
    updated_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_printer_ink (printer_id, ink_id),
    KEY idx_pic_printer_status (printer_id, status),
    KEY idx_pic_ink_status (ink_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DOWN_SQL = "DROP TABLE IF EXISTS printer_ink_compatibility;"


def migrate_up(conn=None) -> bool:
    """Run additive migration to create printer_ink_compatibility table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(UP_SQL)
            # Verify table exists
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'printer_ink_compatibility';
            """)
            row = cur.fetchone()
            return bool(row and (row.get("cnt") or 0) > 0)

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        res = _execute(c)
        c.commit()
        return res


def migrate_down(conn=None) -> bool:
    """Roll back migration: drops printer_ink_compatibility table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(DOWN_SQL)
            return True

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        res = _execute(c)
        c.commit()
        return res


def migration_status(conn=None) -> dict:
    """Check whether migration table exists and count relations."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'printer_ink_compatibility';
            """)
            row = cur.fetchone()
            exists = bool(row and (row.get("cnt") or 0) > 0)
            count = 0
            active_count = 0
            if exists:
                cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active FROM printer_ink_compatibility;")
                stats = cur.fetchone() or {}
                count = int(stats.get("total") or 0)
                active_count = int(stats.get("active") or 0)
            return {
                "table_exists": exists,
                "total_relations": count,
                "active_relations": active_count,
            }

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


def main():
    parser = argparse.ArgumentParser(description="Printer <-> Ink Dependency Database Migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Apply additive migration (create table)")
    group.add_argument("--down", action="store_true", help="Rollback migration (drop table)")
    group.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    try:
        if args.up:
            ok = migrate_up()
            print(f"[Migration UP] Status: {'Success' if ok else 'Failed'}")
        elif args.down:
            ok = migrate_down()
            print(f"[Migration DOWN] Rollback status: {'Success' if ok else 'Failed'}")
        elif args.status:
            status = migration_status()
            print(f"[Migration Status] Table Exists: {status['table_exists']}, Total Relations: {status['total_relations']}, Active: {status['active_relations']}")
    except Exception as e:
        print(f"[Migration Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
