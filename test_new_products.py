"""
Unit & Integration Test Suite for New Products Controller & API.
Tests cover:
1. Authentication: 401 on unauthenticated API requests, 303 redirect to /login on page.
2. Authorization / Permissions: 403 on non-permitted users, Admin-only research trigger.
3. Request Validation: 422 on invalid view, invalid dates, and reversed date range.
4. Default Date Calculation: 30-day default window when dates are omitted.
5. Background Research: 202 Accepted with job_id, status check, 404 on unknown job.
6. Service / Template Readiness: 503 Feature not ready with pending task list.
7. Security: No secrets or raw stack traces exposed on error.
"""

import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app
import product_research_service


class NewProductsControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    # -------------------------------------------------------------------------
    # 1. Authentication Tests
    # -------------------------------------------------------------------------
    @patch("main.is_authenticated", return_value=False)
    @patch("main._get_request_user", return_value=None)
    def test_01_unauthenticated_page_redirects_to_login(self, mock_user, mock_auth):
        res = self.client.get("/new-products", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers.get("location"), "/login")

    @patch("main._get_request_user", return_value=None)
    def test_02_unauthenticated_apis_return_401(self, mock_user):
        res_report = self.client.get("/api/new-products/report")
        self.assertEqual(res_report.status_code, 401)
        self.assertFalse(res_report.json().get("success"))

        res_research = self.client.post("/api/new-products/research")
        self.assertEqual(res_research.status_code, 401)

        res_status = self.client.get("/api/new-products/research/some-job")
        self.assertEqual(res_status.status_code, 401)

    # -------------------------------------------------------------------------
    # 2. Permission / Role Tests
    # -------------------------------------------------------------------------
    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user", return_value={"id": 5, "username": "guest_user", "role": "User", "allowed_menus": []})
    def test_03_unauthorized_user_forbidden_403(self, mock_user, mock_auth):
        res_page = self.client.get("/new-products")
        self.assertEqual(res_page.status_code, 403)

        res_report = self.client.get("/api/new-products/report")
        self.assertEqual(res_report.status_code, 403)
        self.assertIn("Permission denied", res_report.json().get("message", ""))

        res_research = self.client.post("/api/new-products/research")
        self.assertEqual(res_research.status_code, 403)

        res_status = self.client.get("/api/new-products/research/some-job")
        self.assertEqual(res_status.status_code, 403)

    @patch("main._get_request_user", return_value={"id": 6, "username": "analyst", "role": "User", "allowed_menus": ["new-products"]})
    def test_04_permitted_non_admin_can_view_report_but_not_trigger_research(self, mock_user):
        # Can view report
        res_report = self.client.get("/api/new-products/report")
        self.assertEqual(res_report.status_code, 200)

        # Cannot trigger research (Admin only)
        res_research = self.client.post("/api/new-products/research")
        self.assertEqual(res_research.status_code, 403)
        self.assertIn("Admin privileges required", res_research.json().get("message", ""))

    # -------------------------------------------------------------------------
    # 3. Request Validation Tests (Report API)
    # -------------------------------------------------------------------------
    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    def test_05_report_validation_errors(self, mock_user):
        # Invalid view
        res = self.client.get("/api/new-products/report?view=invalid_view")
        self.assertEqual(res.status_code, 422)
        self.assertIn("Invalid view", res.json().get("message", ""))

        # Invalid start_date format
        res = self.client.get("/api/new-products/report?start_date=2026-02-30")
        self.assertEqual(res.status_code, 422)
        self.assertIn("Invalid start_date", res.json().get("message", ""))

        # Invalid end_date format
        res = self.client.get("/api/new-products/report?end_date=bad-date")
        self.assertEqual(res.status_code, 422)
        self.assertIn("Invalid end_date", res.json().get("message", ""))

        # Reversed date range
        res = self.client.get("/api/new-products/report?start_date=2026-05-10&end_date=2026-05-01")
        self.assertEqual(res.status_code, 422)
        self.assertIn("start_date cannot be after end_date", res.json().get("message", ""))

    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    def test_06_report_success_with_default_and_custom_filters(self, mock_user):
        # Default view and dates
        res = self.client.get("/api/new-products/report")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("view"), "local")
        self.assertTrue(data.get("date_range", {}).get("is_default_range"))
        self.assertIn("products", data)

        # Market view with valid custom dates
        res_mkt = self.client.get("/api/new-products/report?view=market&start_date=2026-01-01&end_date=2026-01-31")
        self.assertEqual(res_mkt.status_code, 200)
        data_mkt = res_mkt.json()
        self.assertEqual(data_mkt.get("view"), "market")
        self.assertFalse(data_mkt.get("date_range", {}).get("is_default_range"))

        # Comparison view with keyword filter
        res_cmp = self.client.get("/api/new-products/report?view=comparison&q=Epson")
        self.assertEqual(res_cmp.status_code, 200)
        self.assertEqual(res_cmp.json().get("view"), "comparison")

    # -------------------------------------------------------------------------
    # 4. Background Research Job Tests
    # -------------------------------------------------------------------------
    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    def test_07_admin_can_start_research_and_check_status(self, mock_user):
        # 1. Start research -> 202 Accepted
        res = self.client.post("/api/new-products/research")
        self.assertEqual(res.status_code, 202)
        payload = res.json()
        self.assertTrue(payload.get("success"))
        self.assertIn("job_id", payload)
        job_id = payload["job_id"]

        # 2. Check status -> 200 OK
        status_res = self.client.get(f"/api/new-products/research/{job_id}")
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertTrue(status_data.get("success"))
        self.assertEqual(status_data.get("job_id"), job_id)
        self.assertIn("status", status_data.get("data", {}))

        # 3. Unknown job_id -> 404 Not Found
        unknown_res = self.client.get("/api/new-products/research/non-existent-job-999")
        self.assertEqual(unknown_res.status_code, 404)
        self.assertIn("not found", unknown_res.json().get("message", "").lower())

    # -------------------------------------------------------------------------
    # 5. Service / Template 503 "Feature Not Ready" & Page Rendering Tests
    # -------------------------------------------------------------------------
    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    def test_08_page_renders_when_template_exists_and_503_when_missing(self, mock_user, mock_auth):
        # 1. Template exists -> 200 HTML response
        res = self.client.get("/new-products")
        self.assertEqual(res.status_code, 200)
        self.assertIn("New Products from Websites", res.text)

        # 2. Template missing -> 503 Feature not ready
        with patch("pathlib.Path.exists", return_value=False):
            res_missing = self.client.get("/new-products")
            self.assertEqual(res_missing.status_code, 503)
            data = res_missing.json()
            self.assertFalse(data.get("success"))
            self.assertIn("503 Feature not ready", data.get("message", ""))
            self.assertIn("pending_tasks", data)

    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    @patch("product_research_service.get_product_report", side_effect=NotImplementedError("Market scraper is under construction"))
    def test_09_service_not_implemented_returns_503(self, mock_service, mock_user):
        res = self.client.get("/api/new-products/report")
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertFalse(data.get("success"))
        self.assertIn("503 Feature not ready", data.get("message", ""))
        self.assertIn("pending_tasks", data)

    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    @patch("product_research_service.get_product_report", side_effect=RuntimeError("Secret DB connection string leaked!"))
    def test_10_unexpected_error_sanitized_500(self, mock_service, mock_user):
        res = self.client.get("/api/new-products/report")
        self.assertEqual(res.status_code, 500)
        data = res.json()
        self.assertFalse(data.get("success"))
        # Must not leak secret or raw stack trace
        self.assertNotIn("Secret DB connection string", data.get("message", ""))
        self.assertIn("unexpected error occurred", data.get("message", "").lower())


if __name__ == "__main__":
    unittest.main()
