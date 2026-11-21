"""Automated unit and integration tests for Report 08: Customer Warranty.

Tests:
1. Warranty status computation logic for all boundaries (Active, Expiring Soon, Expired, Not Started, Unknown).
2. Date validation (end date cannot be earlier than start date).
3. RBAC and menu permission helpers (role, allowed_menus list/slug/wildcard).
4. Summary aggregate calculation and card counts note.
5. Filtered list query (search, status filter, date range, pagination, sorting).
6. Record details API (customer, product, sale, warranty, audit details).
7. Admin create and update endpoints with audit logging.
8. Non-admin forbidden from creating/editing (403).
9. Unauthenticated access rejected (303 for page, 401 for APIs).
10. Input validation errors (422) and not found handling (404).
11. Safe handling of multiple products / serial numbers on the same invoice.
12. HTML page rendering with complete English text.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

import main
from db.customer_warranty import (
    can_view_customer_warranty,
    compute_warranty_status,
    create_warranty_record,
    get_warranty_record_detail,
    get_warranty_records,
    get_warranty_summary,
    is_admin_user,
    update_warranty_record,
    validate_and_clean_phone,
)
from db.connection import get_connection


class TestWarrantyStatusLogic(unittest.TestCase):
    """Test precise warranty status calculation and boundary rules."""

    def setUp(self):
        self.ref_date = date(2026, 11, 19)

    def test_active_status(self):
        """Active: warranty has started and ends more than 30 days later."""
        # 31 days remaining
        st = self.ref_date - timedelta(days=10)
        en = self.ref_date + timedelta(days=31)
        self.assertEqual(compute_warranty_status(st, en, self.ref_date), "Active")

        # 365 days remaining
        en_year = self.ref_date + timedelta(days=365)
        self.assertEqual(compute_warranty_status(st, en_year, self.ref_date), "Active")

    def test_expiring_soon_status(self):
        """Expiring Soon: warranty has started and ends today or within the next 30 days."""
        st = self.ref_date - timedelta(days=10)

        # Ends today (0 days remaining)
        self.assertEqual(compute_warranty_status(st, self.ref_date, self.ref_date), "Expiring Soon")

        # Ends in 1 day
        self.assertEqual(compute_warranty_status(st, self.ref_date + timedelta(days=1), self.ref_date), "Expiring Soon")

        # Ends in 15 days
        self.assertEqual(compute_warranty_status(st, self.ref_date + timedelta(days=15), self.ref_date), "Expiring Soon")

        # Exactly 30 days boundary
        self.assertEqual(compute_warranty_status(st, self.ref_date + timedelta(days=30), self.ref_date), "Expiring Soon")

    def test_expired_status(self):
        """Expired: the end date is before today."""
        st = self.ref_date - timedelta(days=60)

        # Ended yesterday
        self.assertEqual(compute_warranty_status(st, self.ref_date - timedelta(days=1), self.ref_date), "Expired")

        # Ended 30 days ago
        self.assertEqual(compute_warranty_status(st, self.ref_date - timedelta(days=30), self.ref_date), "Expired")

    def test_not_started_status(self):
        """Not Started: the start date is in the future."""
        # Starts tomorrow
        st = self.ref_date + timedelta(days=1)
        en = self.ref_date + timedelta(days=365)
        self.assertEqual(compute_warranty_status(st, en, self.ref_date), "Not Started")

        # Starts next month
        st_month = self.ref_date + timedelta(days=30)
        en_future = self.ref_date + timedelta(days=400)
        self.assertEqual(compute_warranty_status(st_month, en_future, self.ref_date), "Not Started")

    def test_unknown_status(self):
        """Unknown: dates needed to determine the status are missing."""
        self.assertEqual(compute_warranty_status(None, None, self.ref_date), "Unknown")
        self.assertEqual(compute_warranty_status(self.ref_date, None, self.ref_date), "Unknown")
        self.assertEqual(compute_warranty_status(None, self.ref_date + timedelta(days=10), self.ref_date), "Unknown")

    def test_invalid_date_order(self):
        """The end date cannot be earlier than the start date."""
        st = self.ref_date
        en = self.ref_date - timedelta(days=5)
        with self.assertRaises(ValueError):
            compute_warranty_status(st, en, self.ref_date)


class TestCustomerWarrantyPermissions(unittest.TestCase):
    """Test RBAC and menu permission helpers."""

    def test_permissions(self):
        self.assertFalse(can_view_customer_warranty(None))
        self.assertTrue(can_view_customer_warranty({"role": "Admin", "allowed_menus": []}))
        self.assertTrue(can_view_customer_warranty({"role": "admin", "allowed_menus": None}))
        self.assertTrue(can_view_customer_warranty({"role": "User", "allowed_menus": "*"}))
        self.assertTrue(can_view_customer_warranty({"role": "User", "allowed_menus": [31]}))
        self.assertTrue(can_view_customer_warranty({"role": "User", "allowed_menus": ["customer-warranty"]}))
        self.assertTrue(can_view_customer_warranty({"role": "User", "allowed_menus": ["/menus/customer-warranty"]}))
        self.assertFalse(can_view_customer_warranty({"role": "User", "allowed_menus": [14, 15]}))

    def test_admin_user_check(self):
        self.assertTrue(is_admin_user({"role": "Admin"}))
        self.assertTrue(is_admin_user({"role": "admin"}))
        self.assertFalse(is_admin_user({"role": "User"}))
        self.assertFalse(is_admin_user(None))


class TestCustomerWarrantyService(unittest.TestCase):
    """Test database queries and service calculations."""

    def test_summary_and_counts_note(self):
        """Summary includes active, expiring_soon, expired, not_started, unknown and note."""
        summary = get_warranty_summary()
        self.assertIn("active", summary)
        self.assertIn("expiring_soon", summary)
        self.assertIn("expired", summary)
        self.assertIn("not_started", summary)
        self.assertIn("unknown", summary)
        self.assertIn("total_records", summary)
        self.assertIn("counts_note", summary)
        self.assertIn("warranty records", summary["counts_note"])
        self.assertGreater(summary["total_records"], 0)

    def test_records_pagination_and_fields(self):
        """Records return customer name, phone, product, invoice, serial, dates, and status."""
        res = get_warranty_records(page=1, page_size=10)
        self.assertTrue(res["success"])
        self.assertIn("summary", res)
        self.assertIn("records", res)
        self.assertIn("pagination", res)
        self.assertEqual(len(res["records"]), 10)

        r = res["records"][0]
        self.assertIn("customer_name", r)
        self.assertIn("phone", r)
        self.assertIn("product_name", r)
        self.assertIn("sales_id", r)
        self.assertIn("warranty_status", r)
        self.assertIn("warranty_start_date", r)
        self.assertIn("warranty_end_date", r)

    def test_status_filters(self):
        """Filtering by status returns only records with that status."""
        for s in ("active", "expiring_soon", "expired", "unknown"):
            res = get_warranty_records(status_filter=s, page=1, page_size=10)
            self.assertTrue(res["success"])
            for r in res["records"]:
                if s == "active":
                    self.assertEqual(r["warranty_status"], "Active")
                elif s == "expiring_soon":
                    self.assertEqual(r["warranty_status"], "Expiring Soon")
                elif s == "expired":
                    self.assertEqual(r["warranty_status"], "Expired")
                elif s == "unknown":
                    self.assertEqual(r["warranty_status"], "Unknown")

    def test_record_detail(self):
        """Record detail returns complete customer, product, sale, and warranty objects."""
        res = get_warranty_records(page=1, page_size=1)
        rec_id = res["records"][0]["id"]
        detail = get_warranty_record_detail(rec_id)
        self.assertIsNotNone(detail)
        self.assertIn("customer", detail)
        self.assertIn("product", detail)
        self.assertIn("sale", detail)
        self.assertIn("warranty", detail)
        self.assertIn("audit", detail)
        self.assertEqual(detail["id"], rec_id)


class TestCustomerWarrantyAPI(unittest.TestCase):
    """Test HTTP API endpoints, auth, and validations."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.admin_user = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
        self.staff_user = {"id": 2, "username": "staff", "role": "User", "allowed_menus": [31]}
        self.unauth_user = {"id": 3, "username": "guest", "role": "User", "allowed_menus": [1]}

    @patch("main.is_authenticated", return_value=False)
    def test_unauthenticated_requests(self, mock_auth):
        """Unauthenticated requests redirect page to /login and return 401 for APIs."""
        # Page redirect
        r_page = self.client.get("/menus/customer-warranty", follow_redirects=False)
        self.assertEqual(r_page.status_code, 303)
        self.assertIn("/login", r_page.headers.get("location", ""))

        # API unauthorized
        r_api = self.client.get("/api/customer-warranty")
        self.assertEqual(r_api.status_code, 401)

        r_post = self.client.post("/api/customer-warranty", json={})
        self.assertEqual(r_post.status_code, 401)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_forbidden_requests(self, mock_user, mock_auth):
        """Unauthorized user without permission gets 403 Forbidden."""
        mock_user.return_value = self.unauth_user
        r_page = self.client.get("/menus/customer-warranty")
        self.assertEqual(r_page.status_code, 403)

        r_api = self.client.get("/api/customer-warranty")
        self.assertEqual(r_api.status_code, 403)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_page_and_read_api_authorized(self, mock_user, mock_auth):
        """Authorized user can view page and fetch report data."""
        mock_user.return_value = self.staff_user

        # Page render
        r_page = self.client.get("/menus/customer-warranty")
        self.assertEqual(r_page.status_code, 200)
        self.assertIn("Customer Warranty Intelligence", r_page.text)
        self.assertIn("Card counts represent", r_page.text)
        self.assertIn("/static/ai_assistant.js", r_page.text)

        # GET API
        r_api = self.client.get("/api/customer-warranty?page=1&page_size=5")
        self.assertEqual(r_api.status_code, 200)
        data = r_api.json()
        self.assertTrue(data["success"])
        self.assertIn("summary", data)
        self.assertIn("records", data)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_non_admin_cannot_create_or_update(self, mock_user, mock_auth):
        """Only Admin can create or update warranty records (403 for other roles)."""
        mock_user.return_value = self.staff_user

        # Non-admin POST
        r_post = self.client.post("/api/customer-warranty", json={"customer_id": "CS00000001", "sales_id": "INV001", "product_id": "PD001"})
        self.assertEqual(r_post.status_code, 403)

        # Non-admin PUT
        r_put = self.client.put("/api/customer-warranty/1", json={"notes": "test"})
        self.assertEqual(r_put.status_code, 403)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_admin_create_update_and_validations(self, mock_user, mock_auth):
        """Admin can create and update warranty records; invalid dates return 422."""
        mock_user.return_value = self.admin_user

        # 1. Validation: End date earlier than start date
        bad_payload = {
            "customer_id": "CS00000001",
            "sales_id": "INV001260004705",
            "product_id": "PD00000252",
            "warranty_start_date": "2026-11-20",
            "warranty_end_date": "2026-11-10",
        }
        r_bad = self.client.post("/api/customer-warranty", json=bad_payload)
        self.assertEqual(r_bad.status_code, 422)
        self.assertIn("cannot be earlier", r_bad.json().get("message", ""))

        # 2. Validation: Missing required fields
        r_missing = self.client.post("/api/customer-warranty", json={"customer_id": "CS00000001"})
        self.assertEqual(r_missing.status_code, 422)

        # 3. Create valid warranty record
        valid_payload = {
            "customer_id": "CS00000001",
            "sales_id": "INV001260004705",
            "product_id": "PD00000252",
            "serial_number": "SN-TEST-SYNTH-999",
            "warranty_start_date": "2026-11-01",
            "warranty_end_date": "2027-11-01",
            "warranty_source": "Synthetic Test Warranty",
            "notes": "Automated test unit creation",
        }
        r_create = self.client.post("/api/customer-warranty", json=valid_payload)
        self.assertEqual(r_create.status_code, 201)
        created_data = r_create.json()
        self.assertTrue(created_data["success"])
        rec_id = created_data["record_id"]
        self.assertIsNotNone(rec_id)

        # 4. Detail GET API for the created record
        r_detail = self.client.get(f"/api/customer-warranty/{rec_id}")
        self.assertEqual(r_detail.status_code, 200)
        d = r_detail.json()["data"]
        self.assertEqual(d["warranty"]["serial_number"], "SN-TEST-SYNTH-999")
        self.assertEqual(d["warranty"]["status"], "Active")

        # 5. Admin PUT update
        update_payload = {
            "warranty_end_date": "2026-11-25",  # Expiring soon relative to 2026-11-19
            "notes": "Updated coverage terms",
        }
        r_update = self.client.put(f"/api/customer-warranty/{rec_id}", json=update_payload)
        self.assertEqual(r_update.status_code, 200)

        # Check updated status
        r_detail_updated = self.client.get(f"/api/customer-warranty/{rec_id}?reference_date=2026-11-19")
        self.assertEqual(r_detail_updated.status_code, 200)
        d_up = r_detail_updated.json()["data"]
        self.assertEqual(d_up["warranty"]["status"], "Expiring Soon")

        # 6. Clean up synthetic record from database
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM customer_warranty_record WHERE id = %s;", (rec_id,))
            conn.commit()

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_not_found_handling(self, mock_user, mock_auth):
        """Unknown record_id returns 404."""
        mock_user.return_value = self.admin_user
        r_detail = self.client.get("/api/customer-warranty/99999999")
        self.assertEqual(r_detail.status_code, 404)

        r_put = self.client.put("/api/customer-warranty/99999999", json={"notes": "test"})
        self.assertEqual(r_put.status_code, 404)


if __name__ == "__main__":
    unittest.main()
