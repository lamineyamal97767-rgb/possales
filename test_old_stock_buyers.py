"""Tests for Report 12: Old Stock & Buyers.

Coverage:
- Stock age calculated from PURCHASE_DATE (not last sale date)
- Products with positive stock only, serial vs non-serial tables
- Buyer lookup uses COUNT(DISTINCT SALES_ID) on sales_details
- Permission checks, page rendering, API validation
- Additive migration preserves existing menu IDs
- Regression checks for existing Product Intelligence menus
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from main import app
from db.connection import get_connection
from db.old_stock_buyers import (
    can_view_old_stock_buyers,
    get_old_stock_buyers_metadata,
    get_old_stock_categories,
    search_products_with_stock_age,
    get_product_buyers,
)
from db.migrations.migrate_old_stock_buyers import migrate_up, migration_status


ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_USER = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": ["old-stock-buyers"]}
VIEWER_BY_ID = {"id": 6, "username": "viewer2", "role": "viewer", "allowed_menus": [35]}
NO_PERM_USER = {"id": 7, "username": "noperm", "role": "viewer", "allowed_menus": [1, 2, 3]}

CATEGORY_ID = "ZOSCAT001"
P1 = "ZOS000001"   # Serialized, in stock, recent purchase
P2 = "ZOS000002"   # Non-serial, in stock, old purchase
P3 = "ZOS000003"   # Non-serial, zero stock
P4 = "ZOS000004"   # Serialized, no stock rows
P5 = "ZOS000005"   # Non-serial, in stock, no purchase date


class TestOldStockBuyersRules(unittest.TestCase):
    def test_admin_and_menu_permissions(self):
        self.assertTrue(can_view_old_stock_buyers(ADMIN_USER))
        self.assertTrue(can_view_old_stock_buyers(VIEWER_USER))
        self.assertTrue(can_view_old_stock_buyers(VIEWER_BY_ID))
        self.assertFalse(can_view_old_stock_buyers(NO_PERM_USER))
        self.assertFalse(can_view_old_stock_buyers(None))
        self.assertTrue(can_view_old_stock_buyers({"role": "viewer", "allowed_menus": "*"}))

    def test_metadata_explains_calculation_and_compatibility(self):
        metadata = get_old_stock_buyers_metadata()
        self.assertEqual(metadata["menu_id"], 35)
        self.assertIn("receipt date", metadata["disclaimer"])
        self.assertIn("not based on the last sale", metadata["disclaimer"])
        self.assertIn("Previously Purchased", metadata["buyer_disclaimer"])
        self.assertIn("PURCHASE_DATE", metadata["calculation_basis"])
        self.assertIn("stock_serial", metadata["calculation_basis"])
        self.assertIn("stock_non_serial", metadata["calculation_basis"])

    def test_buyers_disclaimer_present(self):
        metadata = get_old_stock_buyers_metadata()
        self.assertIn("buyer_disclaimer", metadata)
        self.assertIn("does not prove", metadata["buyer_disclaimer"])


class TestOldStockBuyersDatabase(unittest.TestCase):
    def setUp(self):
        migrate_up()
        self.conn = get_connection()
        try:
            self._cleanup()
            self._seed()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        try:
            self._cleanup()
            self.conn.commit()
        finally:
            self.conn.close()

    def _cleanup(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM stock_non_serial WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM stock_serial WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM sales_details WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM sales WHERE SALES_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM cs WHERE CS_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM product WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM category WHERE CATEGORY_ID = %s", (CATEGORY_ID,))

    def _seed(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES (%s, %s, NOW())",
                (CATEGORY_ID, "ZOS Synthetic Category"),
            )
            cur.executemany("""
                INSERT INTO product
                    (PRODUCT_ID, PRODUCT_NAME, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE, UNIT)
                VALUES (%s, %s, %s, %s, %s, 'Y', 'PCS')
            """, [
                (P1, "ZOS Alpha Serial", CATEGORY_ID, 100.0, "Y"),
                (P2, "ZOS Beta NonSerial", CATEGORY_ID, 80.0, "N"),
                (P3, "ZOS Gamma ZeroStock", CATEGORY_ID, 50.0, "N"),
                (P4, "ZOS Delta NoRows", CATEGORY_ID, 30.0, "Y"),
                (P5, "ZOS Epsilon NoDate", CATEGORY_ID, 20.0, "N"),
            ])

            # P1: serialized, in stock (4 units), purchased 2026-01-01
            cur.execute("""
                INSERT INTO stock_serial
                    (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """, ("2026-01-01", P1, 1.0, 1.0, 4.0, 4.0))

            # P2: non-serial, in stock (10 units), purchased 2026-01-01 (old)
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("2026-01-01", P2, 10.0, 10.0))

            # P3: non-serial, zero stock
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("2026-01-01", P3, 5.0, 0.0))

            # P4: serialized, no stock rows at all

            # P5: non-serial, in stock (7 units), purchase date '1970-01-01' (sentinel for unknown)
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("1970-01-01", P5, 7.0, 7.0))

            # Customers
            cur.executemany("""
                INSERT INTO cs (CS_ID, CS_NAME, MOBILE, ADDRESS) VALUES (%s, %s, %s, %s)
            """, [
                ("ZOSC001", "ZOS Buyer One", "01711111111", "Dhaka"),
                ("ZOSC002", "ZOS Buyer Two", "01722222222", "Chittagong"),
                ("ZOSC003", "ZOS Buyer Three", "01733333333", "Sylhet"),
            ])

            # Sales invoices
            cur.executemany("""
                INSERT INTO sales (SALES_ID, SALES_DATE, NET_SALES, CS_ID, CASH_CS_NAME)
                VALUES (%s, %s, %s, %s, %s)
            """, [
                ("ZOS-S1", "2026-05-01", 500.0, "ZOSC001", ""),
                ("ZOS-S2", "2026-06-15", 300.0, "ZOSC001", ""),
                ("ZOS-S3", "2026-07-20", 200.0, "ZOSC002", ""),
                ("ZOS-S4", "2026-08-01", 150.0, "ZOSC003", ""),
            ])

            # Sales details: P2 bought by C001 (twice), C002; P5 bought by C003
            cur.executemany("""
                INSERT INTO sales_details (SALES_ID, PRODUCT_ID, QTY, UNIT_PRICE, CS_ID, SALES_DATE)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, [
                ("ZOS-S1", P2, 2.0, 80.0, "ZOSC001", "2026-05-01"),
                ("ZOS-S2", P2, 1.0, 80.0, "ZOSC001", "2026-06-15"),
                ("ZOS-S3", P2, 3.0, 80.0, "ZOSC002", "2026-07-20"),
                ("ZOS-S4", P5, 1.0, 20.0, "ZOSC003", "2026-08-01"),
            ])

    def test_search_returns_products_with_positive_stock_only(self):
        result = search_products_with_stock_age(query="ZOS", page=1, page_size=20)
        ids = {p["product_id"] for p in result["products"]}
        self.assertIn(P1, ids)
        self.assertIn(P2, ids)
        self.assertIn(P5, ids)
        self.assertNotIn(P3, ids)   # zero stock
        self.assertNotIn(P4, ids)   # no stock rows

    def test_serial_product_uses_stock_serial_table(self):
        result = search_products_with_stock_age(query="Alpha", page=1, page_size=10)
        self.assertEqual(result["total_records"], 1)
        product = result["products"][0]
        self.assertEqual(product["product_id"], P1)
        self.assertTrue(product["serialized"])
        self.assertEqual(product["stock_source"], "serial")
        self.assertEqual(product["stock_quantity"], 4.0)

    def test_non_serial_product_uses_stock_non_serial_table(self):
        result = search_products_with_stock_age(query="Beta", page=1, page_size=10)
        self.assertEqual(result["total_records"], 1)
        product = result["products"][0]
        self.assertEqual(product["product_id"], P2)
        self.assertFalse(product["serialized"])
        self.assertEqual(product["stock_source"], "non_serial")
        self.assertEqual(product["stock_quantity"], 10.0)

    def test_product_with_null_purchase_date_shows_unknown_age(self):
        result = search_products_with_stock_age(query="Epsilon", page=1, page_size=10)
        self.assertEqual(result["total_records"], 1)
        product = result["products"][0]
        self.assertEqual(product["product_id"], P5)
        self.assertIsNone(product["age_days"])
        self.assertEqual(product["age_display"], "Unknown")
        self.assertEqual(product["age_category"], "unknown")
        self.assertEqual(product["last_purchase_date"], None)
        self.assertEqual(product["last_purchase_date_display"], "Unknown")

    def test_stock_filter_known_unknown(self):
        known = search_products_with_stock_age(query="ZOS", stock_filter="known", page=1, page_size=20)
        unknown = search_products_with_stock_age(query="ZOS", stock_filter="unknown", page=1, page_size=20)
        known_ids = {p["product_id"] for p in known["products"]}
        unknown_ids = {p["product_id"] for p in unknown["products"]}
        self.assertIn(P1, known_ids)
        self.assertIn(P2, known_ids)
        self.assertIn(P5, unknown_ids)
        self.assertEqual(known_ids & unknown_ids, set())

    def test_min_age_days_filter(self):
        # P1 and P2 both purchased 2026-01-01; today is 2026-11-20 => ~324 days
        # P5 has NULL purchase date
        aged = search_products_with_stock_age(query="ZOS", min_age_days=300, page=1, page_size=20)
        aged_ids = {p["product_id"] for p in aged["products"]}
        self.assertIn(P1, aged_ids)
        self.assertIn(P2, aged_ids)
        self.assertNotIn(P5, aged_ids)

        recent = search_products_with_stock_age(query="ZOS", min_age_days=50, page=1, page_size=20)
        recent_ids = {p["product_id"] for p in recent["products"]}
        self.assertIn(P1, recent_ids)
        self.assertIn(P2, recent_ids)
        # P5 excluded because NULL

    def test_sorting_by_stock_age_and_quantity(self):
        by_age = search_products_with_stock_age(query="ZOS", sort_by="stock_age", sort_order="desc", page=1, page_size=20)
        # P1 and P2 have same age (2026-01-01), P5 has NULL age
        ages = [p["age_days"] for p in by_age["products"]]
        # Non-None ages first in desc, then None
        self.assertGreaterEqual(ages[0], ages[1] if len(ages) > 1 else 0)

        by_qty = search_products_with_stock_age(query="ZOS", sort_by="stock_quantity", sort_order="desc", page=1, page_size=20)
        qty = [p["stock_quantity"] for p in by_qty["products"]]
        self.assertEqual(qty, sorted(qty, reverse=True))

    def test_pagination_total_records_matches_filtered(self):
        result = search_products_with_stock_age(query="ZOS", page=1, page_size=2)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_size"], 2)
        self.assertLessEqual(len(result["products"]), 2)
        self.assertEqual(result["total_records"], len([P1, P2, P5]))  # 3 products with positive stock
        self.assertEqual(result["total_pages"], 2)  # ceil(3/2)

    def test_category_filter(self):
        cat = search_products_with_stock_age(category_id=CATEGORY_ID, page=1, page_size=20)
        cat_ids = {p["product_id"] for p in cat["products"]}
        self.assertTrue(cat_ids.issuperset({P1, P2, P5}))

    def test_get_product_buyers_uses_distinct_sales_id(self):
        # P2 has 3 sales details lines across 3 invoices from 2 customers
        buyers = get_product_buyers(P2, page=1, page_size=10)
        self.assertEqual(buyers["total_buyers"], 2)  # C001 and C002
        self.assertEqual(buyers["pagination"]["total_records"], 2)
        # C001 appears twice (S1, S2) but COUNT(DISTINCT SALES_ID) = 2, and we group by CS_ID
        c001 = next(b for b in buyers["buyers"] if b["customer_id"] == "ZOSC001")
        self.assertEqual(c001["order_count"], 2)  # 2 distinct invoices
        c002 = next(b for b in buyers["buyers"] if b["customer_id"] == "ZOSC002")
        self.assertEqual(c002["order_count"], 1)

    def test_get_product_buyers_no_buyers_returns_empty(self):
        buyers = get_product_buyers(P1, page=1, page_size=10)
        self.assertEqual(buyers["total_buyers"], 0)
        self.assertEqual(buyers["buyers"], [])

    def test_get_product_buyers_invalid_product_id_returns_empty(self):
        buyers = get_product_buyers("ZNONEXIST", page=1, page_size=10)
        self.assertEqual(buyers["total_buyers"], 0)
        self.assertEqual(buyers["buyers"], [])

    def test_buyers_pagination(self):
        buyers = get_product_buyers(P2, page=1, page_size=1)
        self.assertEqual(buyers["pagination"]["total_records"], 2)
        self.assertEqual(len(buyers["buyers"]), 1)
        self.assertTrue(buyers["pagination"]["has_next"])
        self.assertFalse(buyers["pagination"]["has_prev"])

    def test_search_includes_distinct_buyer_count_per_product(self):
        result = search_products_with_stock_age(query="ZOS", page=1, page_size=20)
        counts = {p["product_id"]: p["buyer_count"] for p in result["products"]}
        self.assertEqual(counts[P1], 0)
        self.assertEqual(counts[P2], 2)  # ZOSC001 (two invoices) + ZOSC002
        self.assertEqual(counts[P5], 1)

    def test_stock_age_ignores_sold_out_receipt_rows(self):
        # A newer receipt that has already sold out must not make old stock look new.
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("2026-10-01", P2, 5.0, 0.0))
        self.conn.commit()

        result = search_products_with_stock_age(query="Beta", page=1, page_size=10)
        product = result["products"][0]
        self.assertEqual(product["product_id"], P2)
        self.assertEqual(product["last_purchase_date"], "2026-01-01")
        self.assertEqual(product["stock_quantity"], 10.0)

    def test_categories_only_include_products_with_stock(self):
        cats = get_old_stock_categories()
        cat_ids = {c["category_id"] for c in cats}
        self.assertIn(CATEGORY_ID, cat_ids)
        for cat in cats:
            self.assertGreaterEqual(cat["product_count"], 1)


class TestOldStockBuyersApi(unittest.TestCase):
    client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        migrate_up()
        self.conn = get_connection()
        try:
            self._cleanup()
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES (%s, %s, NOW())",
                    (CATEGORY_ID, "ZOS Synthetic API Category"),
                )
                cur.executemany("""
                    INSERT INTO product
                        (PRODUCT_ID, PRODUCT_NAME, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE, UNIT)
                    VALUES (%s, %s, %s, %s, %s, 'Y', 'PCS')
                """, [
                    (P1, "ZOS Alpha API", CATEGORY_ID, 100.0, "Y"),
                    (P2, "ZOS Beta API", CATEGORY_ID, 80.0, "N"),
                ])
                cur.execute("""
                    INSERT INTO stock_serial
                        (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """, ("2026-01-01", P1, 1.0, 1.0, 4.0, 4.0))
                cur.execute("""
                    INSERT INTO stock_non_serial
                        (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                    VALUES (%s, %s, %s, %s, NOW())
                """, ("2026-01-01", P2, 10.0, 10.0))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        try:
            self._cleanup()
            self.conn.commit()
        finally:
            self.conn.close()

    def _cleanup(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM stock_non_serial WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM stock_serial WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM product WHERE PRODUCT_ID LIKE 'ZOS%%'")
            cur.execute("DELETE FROM category WHERE CATEGORY_ID = %s", (CATEGORY_ID,))

    def test_unauthenticated_and_permission_denied(self):
        with patch("main.is_authenticated", return_value=False):
            page = self.client.get("/old-stock-buyers", follow_redirects=False)
            bootstrap = self.client.get("/api/old-stock-buyers/bootstrap")
            products = self.client.get("/api/old-stock-buyers/products?q=ZOS")
            buyers = self.client.get("/api/old-stock-buyers/buyers?product_id=ZOS000001")
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers.get("location", ""))
        self.assertEqual(bootstrap.status_code, 401)
        self.assertEqual(products.status_code, 401)
        self.assertEqual(buyers.status_code, 401)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=NO_PERM_USER):
            page = self.client.get("/old-stock-buyers")
            bootstrap = self.client.get("/api/old-stock-buyers/bootstrap")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(bootstrap.status_code, 403)

    def test_page_renders_for_admin_and_permitted_viewer(self):
        for user in (ADMIN_USER, VIEWER_USER):
            with patch("main.is_authenticated", return_value=True), \
                 patch("main._get_request_user", return_value=user):
                response = self.client.get("/old-stock-buyers")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Old Stock & Buyers", response.text)
            self.assertIn("PURCHASE_DATE", response.text)
            self.assertIn("Previously Purchased", response.text)

    def _render_page_script(self) -> str:
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            response = self.client.get("/old-stock-buyers")
        self.assertEqual(response.status_code, 200)
        scripts = re.findall(r"<script>(.*?)</script>", response.text, re.S)
        self.assertTrue(scripts, "page has no inline script")
        return scripts[-1]

    def test_page_script_is_valid_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = self._render_page_script()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old_stock_buyers.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script)
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_script_escapes_quotes_and_avoids_inline_row_handlers(self):
        script = self._render_page_script()
        self.assertIn("'&#39;'", script)
        self.assertIn("'&quot;'", script)
        # Product names can contain quotes, so rows must not build JS calls from them.
        self.assertNotIn("onclick=\"openBuyersModal(", script)
        self.assertNotIn("fetchBuyersCount(", script)

    def test_buyers_modal_closes_on_escape(self):
        script = self._render_page_script()
        self.assertIn("e.key === 'Escape'", script)
        self.assertIn("closeModal('buyers-modal')", script)

    def test_bootstrap_search_and_buyers(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            bootstrap = self.client.get("/api/old-stock-buyers/bootstrap")
            search = self.client.get("/api/old-stock-buyers/products?q=ZOS%20Alpha&page=1&page_size=10")
            buyers = self.client.get("/api/old-stock-buyers/buyers?product_id=" + P1)

        self.assertEqual(bootstrap.status_code, 200)
        body = bootstrap.json()
        self.assertEqual(body["metadata"]["menu_id"], 35)
        self.assertFalse(body["user"]["is_admin"])
        self.assertTrue(any(c["category_id"] == CATEGORY_ID for c in body["categories"]))

        self.assertEqual(search.status_code, 200)
        search_body = search.json()
        self.assertEqual(search_body["total_records"], 1)
        self.assertIsNotNone(search_body["products"][0]["age_days"])  # age_days present
        self.assertTrue(search_body["products"][0]["serialized"])

        self.assertEqual(buyers.status_code, 200)
        buyers_body = buyers.json()
        self.assertEqual(buyers_body["product_id"], P1)
        self.assertEqual(buyers_body["total_buyers"], 0)

    def test_api_filters_and_sorting(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            stock_known = self.client.get("/api/old-stock-buyers/products?stock_filter=known")
            stock_unknown = self.client.get("/api/old-stock-buyers/products?stock_filter=unknown")
            min_age = self.client.get("/api/old-stock-buyers/products?min_age_days=50")
            sort_age_desc = self.client.get("/api/old-stock-buyers/products?sort_by=stock_age&sort_order=desc")
            sort_qty_asc = self.client.get("/api/old-stock-buyers/products?sort_by=stock_quantity&sort_order=asc")

        for r in (stock_known, stock_unknown, min_age, sort_age_desc, sort_qty_asc):
            self.assertEqual(r.status_code, 200)


class TestOldStockBuyersMigration(unittest.TestCase):
    def test_additive_migration_preserves_existing_menu_ids(self):
        self.assertTrue(migrate_up())
        status = migration_status()
        self.assertFalse(status["table_exists"])
        self.assertIsNotNone(status["menu"])
        self.assertEqual(status["menu"]["id"], 35)
        self.assertEqual(status["menu"]["parent_id"], 1)
        self.assertEqual(status["menu"]["url"], "/old-stock-buyers")
        self.assertEqual(status["menu"]["icon"], "package-search")
        self.assertEqual(status["menu"]["menu_order"], 7)

    def test_existing_product_intelligence_menus_remain_intact(self):
        migrate_up()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, parent_id, url FROM sidebar_menus
                    WHERE title = 'Stock-out Analysis'
                """)
                stock_out = cur.fetchone()
                cur.execute("""
                    SELECT id, parent_id, url FROM sidebar_menus
                    WHERE title = 'Customer Gifts'
                """)
                gifts = cur.fetchone()
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM product
                """)
                product_count = int(cur.fetchone()["cnt"])
        self.assertEqual(stock_out["id"], 33)
        self.assertEqual(stock_out["parent_id"], 1)
        self.assertEqual(stock_out["url"], "/menus/stock-out-analysis")
        self.assertEqual(gifts["id"], 32)
        self.assertEqual(gifts["parent_id"], 2)
        self.assertEqual(gifts["url"], "/menus/customer-gifts")
        self.assertGreaterEqual(product_count, 0)


if __name__ == "__main__":
    unittest.main()