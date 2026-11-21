"""Migration script for Report 06: Customer Business KYC.

Usage:
    python -m db.migrations.migrate_customer_kyc --up
    python -m db.migrations.migrate_customer_kyc --down
    python -m db.migrations.migrate_customer_kyc --status
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS customer_kyc_profile (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id VARCHAR(10) NOT NULL UNIQUE,
    business_name VARCHAR(200) NULL,
    business_type VARCHAR(100) NULL,
    contact_person VARCHAR(100) NULL,
    notes TEXT NULL,
    updated_by_user_id INT NULL,
    updated_by_username VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_ckp_customer (customer_id),
    KEY idx_ckp_type (business_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DOWN_SQL = "DROP TABLE IF EXISTS customer_kyc_profile;"


def migrate_up(conn=None) -> bool:
    """Run additive migration to create customer_kyc_profile table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(UP_SQL)
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_kyc_profile';
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
    """Roll back migration: drops customer_kyc_profile table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(DOWN_SQL)
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_kyc_profile';
            """)
            row = cur.fetchone()
            return bool(row and (row.get("cnt") or 0) == 0)

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        res = _execute(c)
        c.commit()
        return res


def check_status(conn=None) -> dict:
    """Check status of customer_kyc_profile table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_kyc_profile';
            """)
            row = cur.fetchone()
            exists = bool(row and (row.get("cnt") or 0) > 0)
            rows_count = 0
            if exists:
                cur.execute("SELECT COUNT(*) as cnt FROM customer_kyc_profile;")
                r = cur.fetchone()
                rows_count = int(r.get("cnt") or 0) if r else 0
            return {"exists": exists, "rows": rows_count}

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Customer KYC Database Migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Apply additive migration (create table)")
    group.add_argument("--down", action="store_true", help="Roll back migration (drop table)")
    group.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    if args.up:
        ok = migrate_up()
        if ok:
            print("Migration UP SUCCESS: 'customer_kyc_profile' table is ready.")
            sys.exit(0)
        else:
            print("Migration UP FAILED.")
            sys.exit(1)
    elif args.down:
        ok = migrate_down()
        if ok:
            print("Migration DOWN SUCCESS: 'customer_kyc_profile' table dropped.")
            sys.exit(0)
        else:
            print("Migration DOWN FAILED.")
            sys.exit(1)
    elif args.status:
        st = check_status()
        print(f"Migration Status: Table Exists={st['exists']}, Total Profiles={st['rows']}")
        sys.exit(0)
