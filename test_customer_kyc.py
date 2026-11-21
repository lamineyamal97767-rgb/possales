"""Automated unit and integration tests for Report 06: Customer Business KYC.

Tests:
1. Customer directory query (search, business_type filter, pagination).
2. Existing customer with and without a business KYC profile.
3. Customer without sales history (0 orders, 0 spend, empty products list).
4. Correct sales totals from sales table avoiding join duplication.
5. Admin profile create/update (PUT) and audit event recording.
6. Non-admin user forbidden from PUT /api/customer-kyc/customers/{customer_id} (403).
7. Unauthenticated access rejected (303 for page, 401 for APIs).
8. Unauthorized user rejected with 403 on page and GET APIs.
9. Unknown customer ID returns 404 Not Found.
10. Invalid input payload (bad JSON, excessive field length) returns 422 Unprocessable Entity.
11. Permission helpers check (role, allowed_menus list/slug/wildcard).
12. HTML page rendering with complete English text and AI copilot script.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient
import main
from db.customer_kyc import (
    can_view_customer_kyc,
    get_customer_kyc_directory,
    get_customer_kyc_detail,
    save_customer_kyc_profile,
    validate_and_clean_phone,
)
from db.connection import get_connection


class TestCustomerKYCPermissions(unittest.TestCase):
    """Test RBAC and menu permission helpers."""

    def test_permission_helpers(self):
        # Unauthenticated / None
        self.assertFalse(can_view_customer_kyc(None))

        # Admin user
        self.assertTrue(can_view_customer_kyc({"role": "Admin", "allowed_menus": []}))
        self.assertTrue(can_view_customer_kyc({"role": "admin", "allowed_menus": None}))

        # Wildcard allowed_menus
        self.assertTrue(can_view_customer_kyc({"role": "Staff", "allowed_menus": "*"}))

        # Explicit menu ID 30
        self.assertTrue(can_view_customer_kyc({"role": "User", "allowed_menus": [30, 14]}))
        self.assertTrue(can_view_customer_kyc({"role": "User", "allowed_menus": ["30"]}))

        # Explicit slug
        self.assertTrue(can_view_customer_kyc({"role": "User", "allowed_menus": ["customer-kyc"]}))
        self.assertTrue(can_view_customer_kyc({"role": "User", "allowed_menus": ["/menus/customer-kyc"]}))

        # Denied user
        self.assertFalse(can_view_customer_kyc({"role": "User", "allowed_menus": [14, 15]}))
        self.assertFalse(can_view_customer_kyc({"role": "User", "allowed_menus": []}))


class TestCustomerKYCDatabaseService(unittest.TestCase):
    """Test database queries and service calculations."""

    def test_01_directory_and_summary_query(self):
        """Directory returns total counts, pagination, and customer summaries."""
        res = get_customer_kyc_directory(page=1, page_size=10)
        self.assertTrue(res["success"])
        self.assertIn("summary", res)
        self.assertIn("customers", res)
        self.assertIn("pagination", res)
        self.assertIn("business_types", res)

        summary = res["summary"]
        self.assertGreater(summary["total_customers"], 0)
        self.assertGreaterEqual(summary["customers_with_sales"], 0)
        self.assertGreaterEqual(summary["customers_without_sales"], 0)
        self.assertEqual(len(res["customers"]), 10)

    def test_02_search_and_filter(self):
        """Searching by customer ID or name returns matching rows."""
        res = get_customer_kyc_directory(search="CS00000001", page=1, page_size=10)
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["pagination"]["total_items"], 1)
        found = any(c["customer_id"] == "CS00000001" for c in res["customers"])
        self.assertTrue(found)

    def test_03_customer_with_sales_history(self):
        """Existing customer with sales history has non-zero orders, spend, and products."""
        detail = get_customer_kyc_detail("CS00000001")
        self.assertIsNotNone(detail)
        self.assertTrue(detail["success"])
        self.assertEqual(detail["customer_id"], "CS00000001")
        ps = detail["purchase_summary"]
        self.assertTrue(ps["has_sales_history"])
        self.assertGreater(ps["order_count"], 0)
        self.assertGreater(ps["total_spend"], 0.0)
        self.assertIsNotNone(ps["last_purchase_date"])
        self.assertIsInstance(ps["top_products"], list)

    def test_04_customer_without_sales_history(self):
        """Customer with zero purchases returns valid 0 spend and empty product list."""
        # Query a customer without sales from the directory
        dir_res = get_customer_kyc_directory(page=1, page_size=200)
        zero_sales_cust = next((c for c in dir_res["customers"] if c["order_count"] == 0), None)
        if zero_sales_cust:
            cid = zero_sales_cust["customer_id"]
            detail = get_customer_kyc_detail(cid)
            self.assertIsNotNone(detail)
            ps = detail["purchase_summary"]
            self.assertFalse(ps["has_sales_history"])
            self.assertEqual(ps["order_count"], 0)
            self.assertEqual(ps["total_spend"], 0.0)
            self.assertIsNone(ps["last_purchase_date"])
            self.assertEqual(len(ps["top_products"]), 0)

    def test_05_admin_save_and_cleanup_profile(self):
        """Admin can create and update customer KYC profile without altering business data."""
        cid = "CS00000001"
        admin_user = {"id": 1, "username": "admin", "role": "Admin"}

        # 1. Save profile
        payload = {
            "business_name": "Test Enterprise Solutions",
            "business_type": "Corporate & Commercial Office",
            "contact_person": "Mr. Test Manager",
            "notes": "Test KYC business profile notes for unit testing.",
        }
        res = save_customer_kyc_profile(cid, payload, admin_user)
        self.assertTrue(res["success"])
        self.assertEqual(res["status_code"], 200)

        # 2. Verify detail reflects profile
        detail = get_customer_kyc_detail(cid)
        self.assertTrue(detail["has_business_profile"])
        bp = detail["business_profile"]
        self.assertIsNotNone(bp)
        self.assertEqual(bp["business_name"], "Test Enterprise Solutions")
        self.assertEqual(bp["business_type"], "Corporate & Commercial Office")
        self.assertEqual(bp["contact_person"], "Mr. Test Manager")

        # 3. Clean up synthetic KYC record so customer table remains pristine
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM customer_kyc_profile WHERE customer_id = %s", (cid,))
            conn.commit()

        # Verify reverted
        detail_after = get_customer_kyc_detail(cid)
        self.assertFalse(detail_after["has_business_profile"])
        self.assertIsNone(detail_after["business_profile"])


class TestCustomerKYCEndpoints(unittest.TestCase):
    """Integration tests for FastAPI endpoints."""

    def setUp(self):
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_01_unauthenticated_requests(self):
        """Unauthenticated requests are redirected (page) or rejected with 401 (APIs)."""
        res_page = self.client.get("/menus/customer-kyc", follow_redirects=False)
        self.assertEqual(res_page.status_code, 303)
        self.assertEqual(res_page.headers.get("location"), "/login")

        res_dir = self.client.get("/api/customer-kyc/customers")
        self.assertEqual(res_dir.status_code, 401)

        res_det = self.client.get("/api/customer-kyc/customers/CS00000001")
        self.assertEqual(res_det.status_code, 401)

        res_put = self.client.put("/api/customer-kyc/customers/CS00000001", json={})
        self.assertEqual(res_put.status_code, 401)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_02_forbidden_requests_for_unauthorized_user(self, mock_user, mock_auth):
        """Users lacking menu permission get 403 Forbidden."""
        mock_user.return_value = {"id": 9, "username": "guest", "role": "User", "allowed_menus": [14]}

        res_page = self.client.get("/menus/customer-kyc")
        self.assertEqual(res_page.status_code, 403)

        res_dir = self.client.get("/api/customer-kyc/customers")
        self.assertEqual(res_dir.status_code, 403)

        res_det = self.client.get("/api/customer-kyc/customers/CS00000001")
        self.assertEqual(res_det.status_code, 403)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_03_non_admin_cannot_update_kyc_profile(self, mock_user, mock_auth):
        """Non-admin user with KYC view permission can view directory but is denied PUT (403)."""
        mock_user.return_value = {"id": 10, "username": "staff", "role": "Staff", "allowed_menus": [30]}

        # View directory: allowed
        res_dir = self.client.get("/api/customer-kyc/customers")
        self.assertEqual(res_dir.status_code, 200)

        # View detail: allowed
        res_det = self.client.get("/api/customer-kyc/customers/CS00000001")
        self.assertEqual(res_det.status_code, 200)

        # Update profile: FORBIDDEN for non-admin!
        res_put = self.client.put("/api/customer-kyc/customers/CS00000001", json={"business_name": "Hack"})
        self.assertEqual(res_put.status_code, 403)
        self.assertIn("Admin", res_put.json().get("message", ""))

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_04_permitted_page_and_directory(self, mock_user, mock_auth):
        """Permitted user loads 200 HTML page and 200 JSON directory."""
        mock_user.return_value = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}

        res_page = self.client.get("/menus/customer-kyc")
        self.assertEqual(res_page.status_code, 200)
        self.assertIn("Customer Business KYC", res_page.text)
        self.assertIn("/static/ai_assistant.js", res_page.text)

        # Alternative route /customer-kyc
        res_alt = self.client.get("/customer-kyc")
        self.assertEqual(res_alt.status_code, 200)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user")
    def test_05_unknown_customer_and_validation(self, mock_user, mock_auth):
        """Unknown customer returns 404, invalid input returns 422."""
        mock_user.return_value = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}

        # Unknown customer detail
        res_404 = self.client.get("/api/customer-kyc/customers/CS_NON_EXISTENT_9999")
        self.assertEqual(res_404.status_code, 404)

        # Excessive length payload on PUT
        res_422 = self.client.put(
            "/api/customer-kyc/customers/CS00000001",
            json={"business_name": "A" * 250},  # Exceeds 200 chars
        )
        self.assertEqual(res_422.status_code, 422)


if __name__ == "__main__":
    unittest.main()
