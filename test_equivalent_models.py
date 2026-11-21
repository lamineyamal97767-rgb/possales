"""Tests for Report 11: Equivalent Models.

Coverage:
- Directional relationships, duplicate prevention and no automatic reverse
- Admin verification semantics, deactivation and reactivation
- Missing model/price/stock/attribute handling without inference
- Search, filters, sorting, pagination and comparison detail
- Permission checks, page rendering, API validation and Admin-only writes
- Additive migration and preservation of existing menu IDs
- Regression smoke checks for existing Product Intelligence menus
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from main import app
from db.connection import get_connection
from db.equivalent_models import (
    STATUS_INACTIVE,
    STATUS_NEEDS_REVIEW,
    STATUS_VERIFIED,
    _build_comparison,
    _effective_status,
    _normalize_filters,
    _product_payload,
    add_equivalent_relationship,
    can_view_equivalent_models,
    deactivate_equivalent_relationship,
    get_equivalent_models_metadata,
    get_relationship_detail,
    list_alternative_relationships,
    search_products,
    verify_equivalent_relationship,
)
from db.migrations.migrate_equivalent_models import migrate_up, migration_status


ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_USER = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": ["equivalent-models"]}
VIEWER_BY_ID = {"id": 6, "username": "viewer2", "role": "viewer", "allowed_menus": [34]}
NO_PERM_USER = {"id": 7, "username": "noperm", "role": "viewer", "allowed_menus": [1, 2, 3]}

CATEGORY_ID = "ZECAT00001"
P1 = "ZEQ0000001"
P2 = "ZEQ0000002"
P3 = "ZEQ0000003"
P4 = "ZEQ0000004"
P5 = "ZEQ0000005"


def _raw_product(**overrides):
    row = {
        "product_id": "P1",
        "product_name": "Synthetic Product",
        "description": "Synthetic description",
        "category_id": "C1",
        "category_name": "Synthetic Category",
        "brand_id": "B1",
        "brand_name": "Synthetic Brand",
        "sales_price": 100.0,
        "unit": "PCS",
        "warranty": "6 months",
        "color": "Black",
        "serial": "N",
        "barcode_id": "BAR1",
        "active": "Y",
        "non_serial_stock": 3.0,
        "non_serial_stock_rows": 1,
        "non_serial_observed_at": None,
        "serial_stock": None,
        "serial_stock_rows": 0,
        "serial_observed_at": None,
    }
    row.update(overrides)
    return row


class TestEquivalentModelRules(unittest.TestCase):
    def test_admin_and_menu_permissions(self):
        self.assertTrue(can_view_equivalent_models(ADMIN_USER))
        self.assertTrue(can_view_equivalent_models(VIEWER_USER))
        self.assertTrue(can_view_equivalent_models(VIEWER_BY_ID))
        self.assertFalse(can_view_equivalent_models(NO_PERM_USER))
        self.assertFalse(can_view_equivalent_models(None))
        self.assertTrue(can_view_equivalent_models({"role": "viewer", "allowed_menus": "*"}))

    def test_metadata_explains_calculation_and_compatibility(self):
        metadata = get_equivalent_models_metadata()
        self.assertEqual(metadata["menu_id"], 34)
        self.assertIn("directional", metadata["disclaimer"])
        self.assertIn("Ink Dependency", metadata["compatibility_notice"])
        self.assertIn("no dedicated model column", metadata["model_note"])
        self.assertIn("non-serial", metadata["calculation_basis"])

    def test_filter_validation(self):
        filters = _normalize_filters(verification_status="verified", stock_status="in_stock", sort_by="price")
        self.assertEqual(filters["verification_status"], "verified")
        self.assertEqual(filters["stock_status"], "in_stock")
        self.assertEqual(filters["sort_by"], "price")

        with self.assertRaises(ValueError):
            _normalize_filters(verification_status="confirmed")
        with self.assertRaises(ValueError):
            _normalize_filters(stock_status="maybe")
        with self.assertRaises(ValueError):
            _normalize_filters(sort_by="random")

    def test_missing_values_are_unknown_and_zero_stock_is_known(self):
        unknown = _product_payload(_raw_product(
            sales_price=None,
            non_serial_stock=None,
            non_serial_stock_rows=0,
            serial_stock_rows=0,
            warranty=None,
            color=None,
            description=None,
        ))
        self.assertIsNone(unknown["model"])
        self.assertEqual(unknown["model_display"], "Unknown")
        self.assertFalse(unknown["price_known"])
        self.assertEqual(unknown["price_display"], "Unknown")
        self.assertFalse(unknown["stock_known"])
        self.assertEqual(unknown["stock_status"], "unknown")
        self.assertEqual(unknown["stock_display"], "Unknown")
        self.assertIn("stock", unknown["missing_fields"])

        zero = _product_payload(_raw_product(non_serial_stock=0.0, non_serial_stock_rows=1))
        self.assertTrue(zero["stock_known"])
        self.assertEqual(zero["stock"], 0.0)
        self.assertEqual(zero["stock_status"], "out_of_stock")

    def test_serial_product_never_falls_back_to_non_serial_stock(self):
        product = _product_payload(_raw_product(
            serial="Y",
            serial_stock=None,
            serial_stock_rows=0,
            non_serial_stock=99.0,
            non_serial_stock_rows=1,
        ))
        self.assertFalse(product["stock_known"])
        self.assertEqual(product["stock_source"], "unknown")

    def test_negative_stock_is_reported_as_out_of_stock(self):
        product = _product_payload(_raw_product(non_serial_stock=-4.0, non_serial_stock_rows=1))
        self.assertTrue(product["stock_known"])
        self.assertEqual(product["stock"], -4.0)
        self.assertEqual(product["stock_status"], "out_of_stock")
        self.assertEqual(product["stock_display"], "-4")

    def test_non_positive_price_is_unknown(self):
        for value in (None, 0, -5):
            product = _product_payload(_raw_product(sales_price=value))
            self.assertFalse(product["price_known"])
            self.assertEqual(product["price_display"], "Unknown")

    def test_verified_requires_verifier_and_timestamp(self):
        incomplete = {"status": "verified", "verified_by": "admin", "verified_at": None}
        conflicting = {"status": "needs_review", "verified_by": "admin", "verified_at": "2026-01-01"}
        complete = {"status": "verified", "verified_by": "admin", "verified_at": "2026-01-01"}
        inactive = {"status": "inactive", "verified_by": "admin", "verified_at": "2026-01-01"}

        self.assertEqual(_effective_status(incomplete), STATUS_NEEDS_REVIEW)
        self.assertEqual(_effective_status(conflicting), STATUS_NEEDS_REVIEW)
        self.assertEqual(_effective_status(complete), STATUS_VERIFIED)
        self.assertEqual(_effective_status(inactive), STATUS_INACTIVE)

    def test_comparison_marks_same_different_and_unknown(self):
        source = _product_payload(_raw_product(product_id="A", brand_name="Same Brand", color=None))
        alternative = _product_payload(_raw_product(product_id="B", brand_name="Same Brand", color="Black"))
        rows = {row["field"]: row for row in _build_comparison(source, alternative)}

        self.assertTrue(rows["brand_name"]["same"])
        self.assertFalse(rows["product_id"]["same"])
        self.assertFalse(rows["color"]["same"])
        self.assertFalse(rows["color"]["source_known"])
        self.assertEqual(rows["model_display"]["source"], "Unknown")


class TestEquivalentModelsDatabase(unittest.TestCase):
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
            cur.execute("""
                DELETE FROM product_equivalent_models
                WHERE source_product_id LIKE 'ZEQ%%' OR alternative_product_id LIKE 'ZEQ%%'
            """)
            cur.execute("DELETE FROM stock_non_serial WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("DELETE FROM stock_serial WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("""
                DELETE FROM dashboard_audit_log
                WHERE action LIKE 'EQUIVALENT_MODEL_%%' AND details LIKE '%%ZEQ%%'
            """)
            cur.execute("DELETE FROM product WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("DELETE FROM category WHERE CATEGORY_ID = %s", (CATEGORY_ID,))

    def _seed(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES (%s, %s, NOW())",
                (CATEGORY_ID, "ZEQ Synthetic Category"),
            )
            cur.executemany("""
                INSERT INTO product
                    (PRODUCT_ID, PRODUCT_NAME, DESCRIPTION, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE)
                VALUES (%s, %s, %s, %s, %s, %s, 'Y')
            """, [
                (P1, "ZEQ Alpha Printer", "Synthetic source printer", CATEGORY_ID, 120.0, "Y"),
                (P2, "ZEQ Beta Printer", "Synthetic alternative printer", CATEGORY_ID, 80.0, "N"),
                (P3, "ZEQ Gamma Printer", "Synthetic unknown stock printer", CATEGORY_ID, 0.0, "N"),
                (P4, "ZEQ Delta Serial Printer", "Synthetic serial unknown stock", CATEGORY_ID, 50.0, "Y"),
                (P5, "ZEQ Epsilon Printer", "Synthetic out of stock printer", CATEGORY_ID, 30.0, "N"),
            ])
            cur.execute("""
                INSERT INTO stock_serial
                    (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST,
                     PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """, ("2026-01-01", P1, 1.0, 1.0, 4.0, 4.0))
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("2026-01-01", P2, 0.0, 0.0))
            cur.execute("""
                INSERT INTO stock_serial
                    (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST,
                     PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """, ("2026-01-01", P4, 1.0, 1.0, 3.0, 3.0))
            cur.execute("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, NOW())
            """, ("2026-01-01", P5, 0.0, 0.0))

    def test_search_uses_catalog_fields_and_never_invents_model(self):
        result = search_products("ZEQ Beta", page=1, page_size=10)
        self.assertEqual(result["total_records"], 1)
        product = result["products"][0]
        self.assertEqual(product["product_id"], P2)
        self.assertIsNone(product["model"])
        self.assertEqual(product["model_display"], "Unknown")
        self.assertIn("description", result["search_fields"])
        self.assertIn("no dedicated model column", result["model_note"])

        by_category = search_products("ZEQ", category_id=CATEGORY_ID, page=1, page_size=20)
        self.assertEqual(by_category["total_records"], 5)

    def test_directional_add_duplicate_and_no_auto_reverse(self):
        created = add_equivalent_relationship(P1, P2, "Synthetic verified evidence", username="test_admin")
        self.assertEqual(created["status"], STATUS_NEEDS_REVIEW)
        self.assertFalse(created["reactivated"])
        self.assertGreater(created["id"], 0)

        with self.assertRaises(ValueError):
            add_equivalent_relationship(P1, P2, "Duplicate synthetic evidence", username="test_admin")
        with self.assertRaises(ValueError):
            add_equivalent_relationship(P1, P1, "Self relation", username="test_admin")
        with self.assertRaises(ValueError):
            add_equivalent_relationship(P1, "ZNONEXIST", "Missing alternative", username="test_admin")

        forward = list_alternative_relationships(P1, verification_status="active")
        reverse = list_alternative_relationships(P2, verification_status="active")
        self.assertEqual(forward["summary"]["total_relationships"], 1)
        self.assertEqual(reverse["summary"]["total_relationships"], 0)

    def test_verify_deactivate_and_reactivate_preserve_evidence(self):
        created = add_equivalent_relationship(P1, P2, "Synthetic verification evidence", username="test_admin")
        verified = verify_equivalent_relationship(
            created["id"], username="admin", user_id=1, ip_address="127.0.0.1"
        )
        self.assertEqual(verified["status"], STATUS_VERIFIED)
        self.assertEqual(verified["verified_by"], "admin")
        self.assertIsNotNone(verified["verified_at"])

        deactivated = deactivate_equivalent_relationship(created["id"], username="admin", user_id=1)
        self.assertEqual(deactivated["status"], STATUS_INACTIVE)
        self.assertEqual(deactivated["status_label"], "Inactive")
        self.assertFalse(deactivated["is_active_relationship"])
        self.assertIsNotNone(deactivated["verified_at"])

        deactivated_again = deactivate_equivalent_relationship(created["id"], username="admin", user_id=1)
        self.assertEqual(deactivated_again["status"], STATUS_INACTIVE)
        self.assertEqual(deactivated_again["status_label"], "Inactive")
        self.assertFalse(deactivated_again["is_active_relationship"])

        reactivated = add_equivalent_relationship(P1, P2, "Synthetic reactivation evidence", username="test_admin")
        self.assertTrue(reactivated["reactivated"])
        self.assertEqual(reactivated["id"], created["id"])

        detail = get_relationship_detail(created["id"])
        self.assertEqual(detail["relationship"]["status"], STATUS_NEEDS_REVIEW)
        self.assertIsNone(detail["relationship"]["verified_at"])
        self.assertEqual(detail["relationship"]["reason"], "Synthetic reactivation evidence")

    def test_filters_summary_and_comparison(self):
        first = add_equivalent_relationship(P1, P2, "Synthetic beta evidence", username="test_admin")
        add_equivalent_relationship(P1, P3, "Synthetic gamma evidence", username="test_admin")
        add_equivalent_relationship(P1, P4, "Synthetic delta evidence", username="test_admin")
        add_equivalent_relationship(P2, P1, "Synthetic reverse evidence", username="test_admin")
        verify_equivalent_relationship(first["id"], username="admin", user_id=1)

        active = list_alternative_relationships(P1, verification_status="active", page=1, page_size=10)
        self.assertEqual(active["summary"]["verified"], 1)
        self.assertEqual(active["summary"]["needs_review"], 2)
        self.assertEqual(active["pagination"]["total_records"], 3)

        verified_only = list_alternative_relationships(P1, verification_status="verified")
        self.assertEqual(verified_only["pagination"]["total_records"], 1)

        search_beta = list_alternative_relationships(P1, search="Beta")
        self.assertEqual(search_beta["pagination"]["total_records"], 1)

        in_stock = list_alternative_relationships(P1, stock_status="in_stock")
        self.assertEqual(in_stock["pagination"]["total_records"], 1)
        out_of_stock = list_alternative_relationships(P1, stock_status="out_of_stock")
        self.assertEqual(out_of_stock["pagination"]["total_records"], 1)
        unknown_stock = list_alternative_relationships(P1, stock_status="unknown")
        self.assertEqual(unknown_stock["pagination"]["total_records"], 1)

        detail = get_relationship_detail(first["id"])
        comparison = {row["field"]: row for row in detail["comparison"]}
        self.assertEqual(detail["source_product"]["product_id"], P1)
        self.assertEqual(detail["alternative_product"]["product_id"], P2)
        self.assertTrue(comparison["category_name"]["same"])
        self.assertEqual(detail["source_product"]["model"], None)

    def test_empty_state_and_missing_source(self):
        self.assertIsNone(list_alternative_relationships("ZNONEXIST"))
        empty = list_alternative_relationships(P1)
        self.assertEqual(empty["summary"]["total_relationships"], 0)
        self.assertEqual(empty["alternatives"], [])
        self.assertIn("directional", empty["disclaimer"].lower())

    def test_pagination_and_sorting(self):
        for product_id in (P2, P3, P4, P5):
            add_equivalent_relationship(P1, product_id, f"Evidence for {product_id}", username="test_admin")

        page_one = list_alternative_relationships(
            P1, verification_status="all", sort_by="product_name", sort_order="asc", page=1, page_size=2
        )
        page_two = list_alternative_relationships(
            P1, verification_status="all", sort_by="product_name", sort_order="asc", page=2, page_size=2
        )
        self.assertEqual(len(page_one["alternatives"]), 2)
        self.assertEqual(page_one["pagination"]["total_pages"], 2)
        self.assertTrue(page_one["pagination"]["has_next"])
        self.assertTrue(page_two["pagination"]["has_prev"])
        self.assertNotEqual(
            page_one["alternatives"][0]["alternative_product_id"],
            page_two["alternatives"][0]["alternative_product_id"],
        )

        by_price = list_alternative_relationships(
            P1, verification_status="all", sort_by="price", sort_order="desc", page_size=10
        )
        prices = [item["alternative_product"]["price"] for item in by_price["alternatives"]]
        self.assertEqual(prices, [80.0, 50.0, 30.0, None])

        by_stock = list_alternative_relationships(
            P1, verification_status="all", sort_by="stock", sort_order="desc", page_size=10
        )
        stocks = [item["alternative_product"]["stock"] for item in by_stock["alternatives"]]
        self.assertEqual(stocks, [3.0, 0.0, 0.0, None])


class TestEquivalentModelsApi(unittest.TestCase):
    client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        migrate_up()
        self.conn = get_connection()
        try:
            self._cleanup()
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES (%s, %s, NOW())",
                    (CATEGORY_ID, "ZEQ Synthetic API Category"),
                )
                cur.executemany("""
                    INSERT INTO product
                        (PRODUCT_ID, PRODUCT_NAME, DESCRIPTION, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Y')
                """, [
                    (P1, "ZEQ Alpha API Printer", "Synthetic API source", CATEGORY_ID, 120.0, "Y"),
                    (P2, "ZEQ Beta API Printer", "Synthetic API alternative", CATEGORY_ID, 80.0, "N"),
                ])
                cur.execute("""
                    INSERT INTO stock_serial
                        (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST,
                         PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """, ("2026-01-01", P1, 1.0, 1.0, 4.0, 4.0))
                cur.execute("""
                    INSERT INTO stock_non_serial
                        (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                    VALUES (%s, %s, %s, %s, NOW())
                """, ("2026-01-01", P2, 0.0, 0.0))
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
            cur.execute("""
                DELETE FROM product_equivalent_models
                WHERE source_product_id LIKE 'ZEQ%%' OR alternative_product_id LIKE 'ZEQ%%'
            """)
            cur.execute("DELETE FROM stock_non_serial WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("DELETE FROM stock_serial WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("""
                DELETE FROM dashboard_audit_log
                WHERE action LIKE 'EQUIVALENT_MODEL_%%' AND details LIKE '%%ZEQ%%'
            """)
            cur.execute("DELETE FROM product WHERE PRODUCT_ID LIKE 'ZEQ%%'")
            cur.execute("DELETE FROM category WHERE CATEGORY_ID = %s", (CATEGORY_ID,))

    def test_unauthenticated_and_permission_denied(self):
        with patch("main.is_authenticated", return_value=False):
            page = self.client.get("/equivalent-models", follow_redirects=False)
            bootstrap = self.client.get("/api/equivalent-models/bootstrap")
            products = self.client.get("/api/equivalent-models/products?q=ZEQ")
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers.get("location", ""))
        self.assertEqual(bootstrap.status_code, 401)
        self.assertEqual(products.status_code, 401)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=NO_PERM_USER):
            page = self.client.get("/equivalent-models")
            bootstrap = self.client.get("/api/equivalent-models/bootstrap")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(bootstrap.status_code, 403)

    def test_page_renders_for_admin_and_permitted_viewer(self):
        for user in (ADMIN_USER, VIEWER_USER):
            with patch("main.is_authenticated", return_value=True), \
                 patch("main._get_request_user", return_value=user):
                response = self.client.get("/equivalent-models")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Equivalent Models", response.text)
            self.assertIn("Ink Dependency", response.text)
            self.assertIn("Model is Unknown", response.text)

    def test_bootstrap_search_and_empty_alternatives(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            bootstrap = self.client.get("/api/equivalent-models/bootstrap")
            search = self.client.get("/api/equivalent-models/products?q=ZEQ%20Alpha&page=1&page_size=10")
            alternatives = self.client.get(f"/api/equivalent-models/alternatives?source_product_id={P1}")
            missing = self.client.get("/api/equivalent-models/alternatives?source_product_id=ZNONEXIST")

        self.assertEqual(bootstrap.status_code, 200)
        body = bootstrap.json()
        self.assertEqual(body["metadata"]["menu_id"], 34)
        self.assertFalse(body["user"]["is_admin"])
        self.assertTrue(any(c["category_id"] == CATEGORY_ID for c in body["categories"]))

        self.assertEqual(search.status_code, 200)
        search_body = search.json()
        self.assertEqual(search_body["total_records"], 1)
        self.assertIsNone(search_body["products"][0]["model"])
        self.assertEqual(search_body["products"][0]["model_display"], "Unknown")

        self.assertEqual(alternatives.status_code, 200)
        self.assertEqual(alternatives.json()["alternatives"], [])
        self.assertEqual(missing.status_code, 404)

    def test_admin_create_update_verify_detail_deactivate(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            created = self.client.post("/api/equivalent-models/relationships", json={
                "source_product_id": P1,
                "alternative_product_id": P2,
                "reason": "Synthetic API evidence",
                "source": "Unit test catalogue",
            })
            self.assertEqual(created.status_code, 200)
            rel_id = created.json()["id"]
            self.assertEqual(created.json()["status"], STATUS_NEEDS_REVIEW)

            duplicate = self.client.post("/api/equivalent-models/relationships", json={
                "source_product_id": P1,
                "alternative_product_id": P2,
                "reason": "Duplicate synthetic evidence",
            })
            self.assertEqual(duplicate.status_code, 422)

            listed = self.client.get(f"/api/equivalent-models/alternatives?source_product_id={P1}")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["summary"]["needs_review"], 1)

            updated = self.client.put(f"/api/equivalent-models/relationships/{rel_id}", json={
                "reason": "Updated synthetic evidence",
                "source": "Updated unit test catalogue",
            })
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["relationship"]["reason"], "Updated synthetic evidence")

            verified = self.client.post(f"/api/equivalent-models/relationships/{rel_id}/verify", json={
                "reason": "Admin reviewed synthetic evidence",
            })
            self.assertEqual(verified.status_code, 200)
            self.assertEqual(verified.json()["relationship"]["status"], STATUS_VERIFIED)
            self.assertTrue(verified.json()["relationship"]["is_admin_verified"])

            detail = self.client.get(f"/api/equivalent-models/relationships/{rel_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["source_product"]["product_id"], P1)
            self.assertEqual(detail.json()["alternative_product"]["product_id"], P2)
            self.assertIn("comparison", detail.json())

            deactivated = self.client.post(f"/api/equivalent-models/relationships/{rel_id}/deactivate")
            self.assertEqual(deactivated.status_code, 200)
            self.assertEqual(deactivated.json()["relationship"]["status"], STATUS_INACTIVE)

    def test_admin_only_writes_and_validation(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            denied = self.client.post("/api/equivalent-models/relationships", json={
                "source_product_id": P1,
                "alternative_product_id": P2,
                "reason": "Viewer should not write",
            })
        self.assertEqual(denied.status_code, 403)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            no_reason = self.client.post("/api/equivalent-models/relationships", json={
                "source_product_id": P1,
                "alternative_product_id": P2,
                "reason": "",
            })
            self_product = self.client.post("/api/equivalent-models/relationships", json={
                "source_product_id": P1,
                "alternative_product_id": P1,
                "reason": "Self relation",
            })
            missing = self.client.get("/api/equivalent-models/relationships/99999999")
            bad_filter = self.client.get(
                f"/api/equivalent-models/alternatives?source_product_id={P1}&verification_status=confirmed"
            )
            bad_json = self.client.post(
                "/api/equivalent-models/relationships",
                data="not-json",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(no_reason.status_code, 422)
        self.assertEqual(self_product.status_code, 422)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(bad_filter.status_code, 422)
        self.assertEqual(bad_json.status_code, 400)


class TestEquivalentModelsMigration(unittest.TestCase):
    def test_additive_migration_preserves_existing_menu_ids(self):
        self.assertTrue(migrate_up())
        status = migration_status()
        self.assertTrue(status["table_exists"])
        self.assertIsNotNone(status["menu"])
        self.assertEqual(status["menu"]["id"], 34)
        self.assertEqual(status["menu"]["parent_id"], 1)
        self.assertEqual(status["menu"]["url"], "/equivalent-models")
        self.assertEqual(status["menu"]["icon"], "repeat")
        self.assertEqual(status["menu"]["menu_order"], 6)

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
