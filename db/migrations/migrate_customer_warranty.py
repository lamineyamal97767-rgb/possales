"""Migration script for Report 08: Customer Warranty.

Usage:
    python -m db.migrations.migrate_customer_warranty --up
    python -m db.migrations.migrate_customer_warranty --down
    python -m db.migrations.migrate_customer_warranty --status
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from typing import Optional

from db.connection import get_connection

UP_SQL = """
CREATE TABLE IF NOT EXISTS customer_warranty_record (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id VARCHAR(10) NOT NULL,
    sales_id VARCHAR(15) NOT NULL,
    product_id VARCHAR(10) NOT NULL,
    sales_detail_seq FLOAT NULL,
    serial_number VARCHAR(100) NULL,
    warranty_start_date DATE NULL,
    warranty_end_date DATE NULL,
    warranty_source VARCHAR(100) DEFAULT 'Admin entry',
    notes TEXT NULL,
    updated_by_user_id INT NULL,
    updated_by_username VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_cwr_customer (customer_id),
    KEY idx_cwr_sales (sales_id),
    KEY idx_cwr_product (product_id),
    KEY idx_cwr_serial (serial_number),
    KEY idx_cwr_dates (warranty_start_date, warranty_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

MENU_SQL = """
UPDATE sidebar_menus 
SET url = '/menus/customer-warranty', icon = 'circle', is_disabled = 0 
WHERE id = 31;
"""

DOWN_SQL = "DROP TABLE IF EXISTS customer_warranty_record;"


def seed_sample_warranty_records(conn, ref_date: Optional[date] = None) -> int:
    """Seed initial warranty records from existing sales details if table is empty.

    Creates realistic records across all statuses:
    - Active (ends > 30 days)
    - Expiring Soon (ends 0-30 days)
    - Expired (ended before today)
    - Not Started (starts in future)
    - Unknown (missing start/end dates)
    """
    if ref_date is None:
        ref_date = date.today()

    with conn.cursor() as cur:
        # Check if records already exist
        cur.execute("SELECT COUNT(*) as cnt FROM customer_warranty_record;")
        row = cur.fetchone()
        if row and (row.get("cnt") or 0) > 0:
            return int(row["cnt"])

        # Fetch sample distinct sales items with existing customer and product
        cur.execute("""
            SELECT DISTINCT
                sd.SALES_ID,
                sd.CS_ID,
                sd.PRODUCT_ID,
                sd.SALES_DATE,
                sd.WARRANTY as raw_warranty,
                sd.SERIAL as raw_serial
            FROM sales_details sd
            JOIN cs c ON sd.CS_ID = c.CS_ID
            JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
            WHERE sd.SALES_DATE IS NOT NULL
            ORDER BY sd.SALES_DATE DESC
            LIMIT 40
        """)
        sales_items = cur.fetchall()
        if not sales_items:
            return 0

        inserted = 0
        for idx, item in enumerate(sales_items):
            s_date = item.get("SALES_DATE")
            if not s_date:
                continue

            # Assign varied warranty scenarios based on index modulo
            serial_num = item.get("raw_serial") or f"SN-PRN-{100000 + idx}"
            notes = None
            source = "Invoice / Standard"

            if idx % 5 == 0:
                # Active: ends 180 days after today (>30 days)
                start_d = min(s_date, ref_date)
                end_d = ref_date + timedelta(days=180)
                source = "Manufacturer Warranty"
                notes = "1 Year standard manufacturer warranty"
            elif idx % 5 == 1:
                # Expiring Soon: ends within 12 days of ref_date
                end_d = ref_date + timedelta(days=12)
                start_d = min(s_date, end_d - timedelta(days=90))
                source = "Dealer 6-Month"
                notes = "Expiring soon; customer notified for renewal"
            elif idx % 5 == 2:
                # Expired: ended 35 days before ref_date
                end_d = ref_date - timedelta(days=35)
                start_d = end_d - timedelta(days=180)
                source = "180-Day Standard"
                notes = "Standard warranty expired"
            elif idx % 5 == 3:
                # Not Started: starts 10 days in future
                start_d = ref_date + timedelta(days=10)
                end_d = ref_date + timedelta(days=375)
                source = "Extended Future Warranty"
                notes = "Post-installation contract starting next month"
            else:
                # Unknown: Missing start and end dates (unconfirmed)
                start_d = None
                end_d = None
                raw_w = item.get("raw_warranty") or "Unspecified"
                source = f"Sales Record ({raw_w})"
                notes = "Warranty period not yet entered by administrator"

            cur.execute("""
                INSERT INTO customer_warranty_record (
                    customer_id, sales_id, product_id, serial_number,
                    warranty_start_date, warranty_end_date, warranty_source,
                    notes, updated_by_user_id, updated_by_username
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                item["CS_ID"],
                item["SALES_ID"],
                item["PRODUCT_ID"],
                serial_num,
                start_d,
                end_d,
                source,
                notes,
                1,
                "admin",
            ))
            inserted += 1

        conn.commit()
        return inserted


def migrate_up(conn=None, seed: bool = True) -> bool:
    """Run additive migration to create customer_warranty_record table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(UP_SQL)
            cur.execute(MENU_SQL)
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_warranty_record';
            """)
            row = cur.fetchone()
            table_ok = bool(row and (row.get("cnt") or 0) > 0)
            if table_ok and seed:
                seed_sample_warranty_records(c)
            return table_ok

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        res = _execute(c)
        c.commit()
        return res


def migrate_down(conn=None) -> bool:
    """Roll back migration: drops customer_warranty_record table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute(DOWN_SQL)
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_warranty_record';
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
    """Check status of customer_warranty_record table."""
    def _execute(c):
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt 
                FROM information_schema.TABLES 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'customer_warranty_record';
            """)
            row = cur.fetchone()
            exists = bool(row and (row.get("cnt") or 0) > 0)
            rows_count = 0
            menu_info = None
            if exists:
                cur.execute("SELECT COUNT(*) as cnt FROM customer_warranty_record;")
                r = cur.fetchone()
                rows_count = int(r.get("cnt") or 0) if r else 0

            cur.execute("SELECT id, title, url, icon, is_disabled FROM sidebar_menus WHERE id = 31;")
            menu_info = cur.fetchone()

            return {"exists": exists, "rows": rows_count, "menu": menu_info}

    if conn is not None:
        return _execute(conn)
    with get_connection() as c:
        return _execute(c)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration for Report 08: Customer Warranty")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="Apply migration")
    group.add_argument("--down", action="store_true", help="Roll back migration")
    group.add_argument("--status", action="store_true", help="Check status")
    args = parser.parse_args()

    if args.up:
        ok = migrate_up()
        status = check_status()
        print(f"[SUCCESS] Migration UP applied: exists={status['exists']}, rows={status['rows']}")
        return 0 if ok else 1
    elif args.down:
        ok = migrate_down()
        print(f"[SUCCESS] Migration DOWN applied: table dropped={ok}")
        return 0 if ok else 1
    elif args.status:
        s = check_status()
        print(f"[STATUS] Table customer_warranty_record exists: {s['exists']}, rows: {s['rows']}, menu: {s['menu']}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
