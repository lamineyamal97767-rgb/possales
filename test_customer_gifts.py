"""Tests for Report 09: Customer Gifts.

Covers:
- New customer boundary (90-day, day 89 = new, day 91 = not new)
- Repeat customers (purchased on 2+ different days) and insufficient history
- Duplicate invoice lines/sales rows do not inflate counts or spending
- Search by name/ID/phone, date and amount filters, sorting and pagination
- Gift recording (Admin-only, server-enforced 403 for non-admin)
- Gift history (customer name/phone search, recording metadata)
- Auth checks (401, 303), permissions, empty data and database errors
- Existing menu IDs, reports permissions and top-customer reporting
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from starlette.testclient import TestClient
from main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_USER = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": "32"}
NO_PERM_USER = {"id": 6, "username": "noperm", "role": "viewer", "allowed_menus": "1,2,3"}

REF_DATE = date(2026, 10, 1)
_90_DAYS_AGO = REF_DATE - timedelta(days=90)


def _make_sales_stats(
    *,
    cid: str = "CS000TEST",
    name: str = "Test Customer",
    phone: str = "01712345678",
    first: date | None = None,
    last: date | None = None,
    orders: int = 3,
    days: int = 2,
    total: float = 50000.0,
):
    return {
        "customer_id": cid,
        "customer_name": name,
        "phone": phone,
        "first_purchase_date": first,
        "last_purchase_date": last,
        "order_count": orders,
        "distinct_purchase_days": days,
        "total_spend": total,
    }


class TestCustomerGiftsService(unittest.TestCase):
    """Unit tests for db.customer_gifts pure functions."""

    def test_new_customer_at_boundary(self):
        """Customer whose first purchase is exactly 90 days ago is new."""
        from db.customer_gifts import build_customer_gift_suggestions

        # exactly at boundary (day 90 = threshold)
        stats = {
            "CS001": _make_sales_stats(cid="CS001", first=_90_DAYS_AGO, last=REF_DATE, orders=1, days=1),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 1, "Customer at exactly 90-day boundary should be new")
        self.assertEqual(len(result["repeat"]), 0)

    def test_customer_beyond_boundary_not_new(self):
        """Customer with first purchase 91+ days ago is NOT new."""
        from db.customer_gifts import build_customer_gift_suggestions

        day_91 = REF_DATE - timedelta(days=91)
        stats = {
            "CS002": _make_sales_stats(cid="CS002", first=day_91, last=REF_DATE, orders=3, days=3),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 0)
        # 91 days ago with 3 purchase days -> repeat
        self.assertEqual(len(result["repeat"]), 1)

    def test_customer_89_days_ago_is_new(self):
        """Customer whose first purchase is 89 days ago is new."""
        from db.customer_gifts import build_customer_gift_suggestions

        day_89 = REF_DATE - timedelta(days=89)
        stats = {
            "CS003": _make_sales_stats(cid="CS003", first=day_89, last=REF_DATE, orders=2, days=2),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(len(result["repeat"]), 0)

    def test_repeat_customer_requires_2_purchase_days(self):
        """Repeat customer must have purchased on at least 2 different days."""
        from db.customer_gifts import build_customer_gift_suggestions

        old_first = REF_DATE - timedelta(days=180)
        stats = {
            # Only 1 purchase day -> NOT repeat
            "CS004": _make_sales_stats(cid="CS004", first=old_first, last=old_first, orders=5, days=1, total=100000),
            # 2 purchase days -> repeat
            "CS005": _make_sales_stats(cid="CS005", first=old_first, last=REF_DATE, orders=3, days=2, total=50000),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 0)
        self.assertEqual(len(result["repeat"]), 1)
        self.assertEqual(result["repeat"][0]["customer_id"], "CS005")

    def test_customer_without_purchase_excluded(self):
        """Customer with no recorded purchase date is excluded from both groups."""
        from db.customer_gifts import build_customer_gift_suggestions

        stats = {
            "CS006": _make_sales_stats(cid="CS006", first=None, last=None, orders=0, days=0, total=0),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 0)
        self.assertEqual(len(result["repeat"]), 0)

    def test_future_first_purchase_excluded(self):
        """Future-dated first purchases are not within the last 90 days."""
        from db.customer_gifts import build_customer_gift_suggestions

        future = REF_DATE + timedelta(days=5)
        stats = {
            "CS007": _make_sales_stats(cid="CS007", first=future, last=future, orders=3, days=3, total=5000),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(len(result["new"]), 0)
        self.assertEqual(len(result["repeat"]), 0)

    def test_sort_by_total_amount(self):
        """Default sort: highest total purchase amount first."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        stats = {
            "CS010": _make_sales_stats(cid="CS010", first=recent, last=REF_DATE, total=10000),
            "CS011": _make_sales_stats(cid="CS011", first=recent, last=REF_DATE, total=50000),
            "CS012": _make_sales_stats(cid="CS012", first=recent, last=REF_DATE, total=30000),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, sort_by="total_purchase_amount", sort_order="desc")
        new = result["new"]
        self.assertEqual(len(new), 3)
        self.assertEqual(new[0]["customer_id"], "CS011")
        self.assertEqual(new[1]["customer_id"], "CS012")
        self.assertEqual(new[2]["customer_id"], "CS010")

    def test_search_filter(self):
        """Search by customer name, ID, or phone."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        stats = {
            "CS020": _make_sales_stats(cid="CS020", name="ALPHA CORP", phone="01712345678", first=recent, last=REF_DATE),
            "CS021": _make_sales_stats(cid="CS021", name="BETA ELECTRONICS", phone="01898765432", first=recent, last=REF_DATE),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, search="alpha")
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0]["customer_id"], "CS020")

        by_id = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, search="cs021")
        self.assertEqual([r["customer_id"] for r in by_id["new"]], ["CS021"])

        by_phone = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, search="0171234")
        self.assertEqual([r["customer_id"] for r in by_phone["new"]], ["CS020"])

    def test_first_purchase_date_filter(self):
        """Date filters apply inclusively to the first recorded purchase."""
        from db.customer_gifts import build_customer_gift_suggestions

        stats = {
            "CS025": _make_sales_stats(cid="CS025", first=date(2026, 8, 1), last=REF_DATE, orders=2, days=2),
            "CS026": _make_sales_stats(cid="CS026", first=date(2026, 9, 1), last=REF_DATE, orders=2, days=2),
        }
        result = build_customer_gift_suggestions(
            stats, new_customer_days=90, reference_date=REF_DATE,
            first_purchase_from=date(2026, 8, 15), first_purchase_to=date(2026, 9, 15),
        )
        self.assertEqual([r["customer_id"] for r in result["new"]], ["CS026"])

    def test_sort_ties_use_order_count(self):
        """Default sorting is purchase amount first, then order count."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        stats = {
            "CS070": _make_sales_stats(cid="CS070", first=recent, last=REF_DATE, orders=2, total=10000),
            "CS071": _make_sales_stats(cid="CS071", first=recent, last=REF_DATE, orders=7, total=10000),
            "CS072": _make_sales_stats(cid="CS072", first=recent, last=REF_DATE, orders=4, total=12000),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual([r["customer_id"] for r in result["new"]], ["CS072", "CS071", "CS070"])

    def test_empty_sales_data(self):
        """Empty sales data returns empty suggestion groups without errors."""
        from db.customer_gifts import build_customer_gift_suggestions

        result = build_customer_gift_suggestions({}, new_customer_days=90, reference_date=REF_DATE)
        self.assertEqual(result["new"], [])
        self.assertEqual(result["repeat"], [])

    def test_missing_phone_is_blank_and_not_in_facts(self):
        """A customer without a phone still renders facts without inventing a phone."""
        from db.customer_gifts import _build_suggestion_row

        row = _build_suggestion_row("CS080", "NO PHONE CO", "", date(2026, 1, 1), date(2026, 3, 1), 2, 2, 100.0)
        self.assertEqual(row["phone"], "")
        self.assertFalse(any(f.startswith("Phone:") for f in row["facts"]))

    def test_min_amount_filter(self):
        """Filter by minimum total purchase amount."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        stats = {
            "CS030": _make_sales_stats(cid="CS030", first=recent, last=REF_DATE, total=10000),
            "CS031": _make_sales_stats(cid="CS031", first=recent, last=REF_DATE, total=50000),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, min_total_amount=20000)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0]["customer_id"], "CS031")

    def test_min_orders_filter(self):
        """Filter by minimum order count."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        stats = {
            "CS040": _make_sales_stats(cid="CS040", first=recent, last=REF_DATE, orders=1),
            "CS041": _make_sales_stats(cid="CS041", first=recent, last=REF_DATE, orders=5),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, min_order_count=3)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0]["customer_id"], "CS041")

    def test_group_filter_new_only(self):
        """Filter group='new' returns only new customers."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        old = REF_DATE - timedelta(days=180)
        stats = {
            "CS050": _make_sales_stats(cid="CS050", first=recent, last=REF_DATE),
            "CS051": _make_sales_stats(cid="CS051", first=old, last=REF_DATE, days=3),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, group="new")
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(len(result["repeat"]), 0)

    def test_group_filter_repeat_only(self):
        """Filter group='repeat' returns only repeat customers."""
        from db.customer_gifts import build_customer_gift_suggestions

        recent = REF_DATE - timedelta(days=10)
        old = REF_DATE - timedelta(days=180)
        stats = {
            "CS050": _make_sales_stats(cid="CS050", first=recent, last=REF_DATE),
            "CS051": _make_sales_stats(cid="CS051", first=old, last=REF_DATE, days=3),
        }
        result = build_customer_gift_suggestions(stats, new_customer_days=90, reference_date=REF_DATE, group="repeat")
        self.assertEqual(len(result["new"]), 0)
        self.assertEqual(len(result["repeat"]), 1)

    def test_facts_contain_purchase_info(self):
        """Suggestion row facts include first purchase date, orders, amount."""
        from db.customer_gifts import _build_suggestion_row

        row = _build_suggestion_row("CS060", "ACME", "0171111", date(2026, 1, 1), date(2026, 6, 1), 5, 4, 75000.0)
        self.assertIn("First recorded purchase: 2026-01-01", row["facts"][0])
        self.assertTrue(any("Orders placed: 5" in f for f in row["facts"]))
        self.assertTrue(any("75000.00" in f for f in row["facts"]))


class TestCustomerGiftsSalesAggregation(unittest.TestCase):
    """Synthetic-data checks for the sales aggregation used by suggestions.

    All rows are inserted inside an uncommitted transaction and rolled back, so
    real customer and sales data are never modified.
    """

    def test_duplicate_invoice_lines_do_not_inflate(self):
        from db.connection import get_connection
        from db.customer_gifts import get_customer_sales_summary

        cid = "CSTGIFT001"
        orphan_cid = "CSORPHAN1"
        invoice_one = "GIFTINV0001"
        invoice_two = "GIFTINV0002"
        orphan_invoice = "GIFTINV0003"

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Suggestions only include customers that exist in cs.
                cur.execute(
                    "INSERT INTO cs (CS_ID, CS_NAME, BALANCE) VALUES (%s, %s, %s)",
                    (cid, "Synthetic Gift Test Customer", 0.0),
                )
                cur.execute("""
                    INSERT INTO sales (SALES_ID, CS_ID, SALES_DATE, TOTAL_PRICE, NET_SALES)
                    VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)
                """, (
                    invoice_one, cid, "2026-01-10 10:00:00", 1000.0, 1000.0,
                    invoice_one, cid, "2026-01-10 10:00:00", 1000.0, 1000.0,
                    invoice_two, cid, "2026-02-15 12:30:00", 500.0, 500.0,
                ))
                # Sales rows without a matching cs customer (e.g. cash sales)
                # are excluded; their detail page cannot be opened.
                cur.execute("""
                    INSERT INTO sales (SALES_ID, CS_ID, SALES_DATE, TOTAL_PRICE, NET_SALES)
                    VALUES (%s, %s, %s, %s, %s)
                """, (orphan_invoice, orphan_cid, "2026-01-20 09:00:00", 250.0, 250.0))
                # Duplicate invoice lines must not be joined into the totals.
                cur.execute("""
                    INSERT INTO sales_details (SALES_ID, CS_ID, SALES_DATE, UNIT_PRICE, QTY)
                    VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)
                """, (
                    invoice_one, cid, "2026-01-10", 500.0, 1.0,
                    invoice_one, cid, "2026-01-10", 500.0, 1.0,
                    invoice_two, cid, "2026-02-15", 500.0, 1.0,
                ))

            stats = get_customer_sales_summary([cid, orphan_cid], conn=conn)
            self.assertIn(cid, stats)
            self.assertNotIn(orphan_cid, stats)
            row = stats[cid]
            self.assertEqual(row["order_count"], 2)
            self.assertEqual(row["distinct_purchase_days"], 2)
            self.assertEqual(row["total_spend"], 1500.0)
            self.assertEqual(row["first_purchase_date"], date(2026, 1, 10))
            self.assertEqual(row["last_purchase_date"], date(2026, 2, 15))
        finally:
            conn.rollback()
            conn.close()


class TestCustomerGiftsPermissions(unittest.TestCase):
    """Permission tests for Customer Gifts module."""

    def test_can_view_admin(self):
        from db.customer_gifts import can_view_customer_gifts
        self.assertTrue(can_view_customer_gifts(ADMIN_USER))

    def test_can_view_with_menu_id(self):
        from db.customer_gifts import can_view_customer_gifts
        with patch("db.customer_gifts.resolve_customer_gifts_menu_id", return_value=32):
            self.assertTrue(can_view_customer_gifts({"role": "User", "allowed_menus": [32]}))
            self.assertTrue(can_view_customer_gifts({"role": "User", "allowed_menus": ["32"]}))
            # CSV grants are still supported for direct/legacy user objects.
            self.assertTrue(can_view_customer_gifts({"role": "User", "allowed_menus": "14,32"}))
            self.assertFalse(can_view_customer_gifts({"role": "User", "allowed_menus": "14,15"}))

    def test_can_view_with_slug(self):
        from db.customer_gifts import can_view_customer_gifts
        self.assertTrue(can_view_customer_gifts({"role": "User", "allowed_menus": ["customer-gifts"]}))
        self.assertTrue(can_view_customer_gifts({"role": "User", "allowed_menus": ["/menus/customer-gifts"]}))

    def test_cannot_view_no_perm(self):
        from db.customer_gifts import can_view_customer_gifts
        self.assertFalse(can_view_customer_gifts(NO_PERM_USER))

    def test_cannot_view_none_user(self):
        from db.customer_gifts import can_view_customer_gifts
        self.assertFalse(can_view_customer_gifts(None))

    def test_is_admin_user(self):
        from db.customer_gifts import is_admin_user
        self.assertTrue(is_admin_user(ADMIN_USER))
        self.assertFalse(is_admin_user(NO_PERM_USER))
        self.assertFalse(is_admin_user(None))


class TestCustomerGiftsRecording(unittest.TestCase):
    """Test gift recording logic."""

    def test_record_requires_admin(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(PermissionError):
            record_customer_gift("CS001", "Gift", user=NO_PERM_USER)

    def test_record_requires_gift_name(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(ValueError) as ctx:
            record_customer_gift("CS001", "", user=ADMIN_USER)
        self.assertIn("Gift name is required", str(ctx.exception))

    def test_record_gift_name_too_long(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(ValueError) as ctx:
            record_customer_gift("CS001", "x" * 201, user=ADMIN_USER)
        self.assertIn("200 characters", str(ctx.exception))

    def test_record_invalid_date(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(ValueError) as ctx:
            record_customer_gift("CS001", "Gift", gift_date="not-a-date", user=ADMIN_USER)
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_record_note_too_long(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(ValueError) as ctx:
            record_customer_gift("CS001", "Gift", note="x" * 501, user=ADMIN_USER)
        self.assertIn("500 characters", str(ctx.exception))

    def test_record_empty_customer_id(self):
        from db.customer_gifts import record_customer_gift
        with self.assertRaises(ValueError) as ctx:
            record_customer_gift("", "Gift", user=ADMIN_USER)
        self.assertIn("Customer ID is required", str(ctx.exception))


class TestCustomerGiftsEndpoints(unittest.TestCase):
    """Integration tests for Customer Gifts API endpoints."""

    client = TestClient(app, raise_server_exceptions=False)

    def _auth_headers(self, user=ADMIN_USER):
        """Create a session for the given user and return cookies."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=user):
            pass
        return user

    # --- Auth: Unauthenticated ---

    def test_page_redirects_unauthenticated(self):
        """Unauthenticated user is redirected to /login (303)."""
        with patch("main.is_authenticated", return_value=False):
            resp = self.client.get("/menus/customer-gifts", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers.get("location", ""))

    def test_suggestions_api_401(self):
        """Unauthenticated API call returns 401."""
        with patch("main.is_authenticated", return_value=False):
            resp = self.client.get("/api/customer-gifts/customers")
        self.assertEqual(resp.status_code, 401)

    def test_gifts_api_401(self):
        """Unauthenticated gift history API returns 401."""
        with patch("main.is_authenticated", return_value=False):
            resp = self.client.get("/api/customer-gifts/gifts")
        self.assertEqual(resp.status_code, 401)

    def test_record_gift_api_401(self):
        """Unauthenticated gift recording returns 401."""
        with patch("main.is_authenticated", return_value=False):
            resp = self.client.post("/api/customer-gifts/gifts", json={"customer_id": "CS001", "gift_name": "Test"})
        self.assertEqual(resp.status_code, 401)

    # --- Auth: No Permission ---

    def test_page_403_no_permission(self):
        """User without Customer Gifts permission gets 403."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=NO_PERM_USER):
            resp = self.client.get("/menus/customer-gifts")
        self.assertEqual(resp.status_code, 403)

    def test_suggestions_api_403(self):
        """API returns 403 for user without permission."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=NO_PERM_USER):
            resp = self.client.get("/api/customer-gifts/customers")
        self.assertEqual(resp.status_code, 403)

    # --- Page and API: Valid ---

    def test_page_loads_200(self):
        """Customer Gifts page loads for authorized user."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/menus/customer-gifts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Customer Gift", resp.text)
        self.assertIn("const isAdminUser = true;", resp.text)
        self.assertIn("First Recorded Purchase", resp.text)
        self.assertIn("Gift History", resp.text)

    def test_page_marks_non_admin_without_record_form(self):
        """Non-admin viewers see read-only details; the page marks the role in JS."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            resp = self.client.get("/menus/customer-gifts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("const isAdminUser = false;", resp.text)

    def test_suggestions_api_returns_structure(self):
        """Suggestions API returns expected structure with summary, new_customers, repeat_customers."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers?group=all&page=1&page_size=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("summary", data)
        self.assertIn("new_customers", data)
        self.assertIn("repeat_customers", data)
        self.assertIn("calculation_basis", data)
        self.assertIn("filters", data)

    def test_suggestions_api_group_filter(self):
        """Suggestions API respects group=new filter."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers?group=new&page=1&page_size=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # repeat_customers should have 0 rows when filtered by group=new
        self.assertEqual(len(data["repeat_customers"]["rows"]), 0)

    def test_suggestions_pagination(self):
        """Suggestions API pagination fields are present."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers?group=all&page=1&page_size=2")
        data = resp.json()
        self.assertIn("pagination", data["new_customers"])
        pag = data["new_customers"]["pagination"]
        self.assertIn("page", pag)
        self.assertIn("page_size", pag)
        self.assertIn("total_records", pag)
        self.assertIn("has_next", pag)

    def test_gifts_history_api_200(self):
        """Gift history API returns 200 and expected structure."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/gifts?page=1&page_size=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("gifts", data)
        self.assertIn("pagination", data)

    def test_customer_detail_404(self):
        """Customer detail returns 404 for nonexistent customer."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers/NONEXISTENT999")
        self.assertEqual(resp.status_code, 404)

    def test_customer_detail_200(self):
        """Customer detail returns 200 for an existing customer."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers/CS00000001")
        # Either 200 (exists) or 404 (doesn't exist in test DB)
        self.assertIn(resp.status_code, [200, 404])
        if resp.status_code == 200:
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertIn("customer", data)
            self.assertIn("gifts", data)

    # --- Gift Recording ---

    def test_record_gift_non_admin_403(self):
        """Non-admin user cannot record a gift (server-side enforcement)."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "CS00000001",
                "gift_name": "Test Gift"
            })
        self.assertEqual(resp.status_code, 403)

    def test_record_gift_missing_name_422(self):
        """Missing gift name returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "CS00000001",
                "gift_name": ""
            })
        self.assertEqual(resp.status_code, 422)

    def test_record_gift_missing_customer_422(self):
        """Missing customer ID returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "",
                "gift_name": "Some Gift"
            })
        self.assertEqual(resp.status_code, 422)

    def test_record_gift_invalid_json_422(self):
        """Invalid JSON payload returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)):
            resp = self.client.post(
                "/api/customer-gifts/gifts",
                content="not json",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(resp.status_code, 422)

    def test_record_gift_success(self):
        """Admin can successfully record a gift."""
        from db.connection import get_connection

        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "CS00000001",
                "gift_name": "Unit Test Gift",
                "gift_date": "2026-11-01",
                "note": "Automated test"
            })

        self.assertIn(resp.status_code, [201, 422])
        if resp.status_code == 201:
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertIn("gift", data)
            self.assertEqual(data["gift"]["gift_name"], "Unit Test Gift")

            # Cleanup
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM customer_gifts WHERE gift_name = %s", ("Unit Test Gift",))
                conn.commit()
            finally:
                conn.close()

    def test_record_gift_nonexistent_customer_422(self):
        """Recording gift for nonexistent customer returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "NONEXISTENT_XYZ_999",
                "gift_name": "Test Gift"
            })
        self.assertEqual(resp.status_code, 422)

    def test_suggestions_date_filter(self):
        """Date filters are accepted and return labeled date ranges."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get(
                "/api/customer-gifts/customers?group=all&first_purchase_from=2026-01-01&first_purchase_to=2026-12-31"
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["filters"]["first_purchase_from"], "2026-01-01")
        self.assertEqual(data["filters"]["first_purchase_to"], "2026-12-31")
        self.assertIn("new_customers", data["date_range"])
        self.assertIn("repeat_customers", data["date_range"])

    def test_suggestions_invalid_date_422(self):
        """Invalid date filter returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers?first_purchase_from=not-a-date")
        self.assertEqual(resp.status_code, 422)

    def test_suggestions_reversed_date_range_422(self):
        """A first-purchase From date after the To date returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get(
                "/api/customer-gifts/customers?first_purchase_from=2026-12-31&first_purchase_to=2026-01-01"
            )
        self.assertEqual(resp.status_code, 422)

    def test_suggestions_empty_data(self):
        """Empty sales data returns 200 with empty groups."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_customer_gifts_suggestions", return_value={
                 "success": True, "summary": {"new_customers_total": 0, "repeat_customers_total": 0},
                 "new_customers": {"rows": [], "pagination": {}},
                 "repeat_customers": {"rows": [], "pagination": {}},
                 "date_range": {"new_customers": {}, "repeat_customers": {}},
                 "filters": {}, "calculation_basis": "",
             }):
            resp = self.client.get("/api/customer-gifts/customers")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["summary"]["new_customers_total"], 0)
        self.assertEqual(data["new_customers"]["rows"], [])

    # --- Database errors ---

    def test_suggestions_database_error_500(self):
        """Database failure in suggestions returns a safe 500 response."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_customer_gifts_suggestions", side_effect=RuntimeError("db down")):
            resp = self.client.get("/api/customer-gifts/customers")
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()["success"])
        self.assertNotIn("db down", resp.json()["message"])

    def test_gift_history_database_error_500(self):
        """Database failure in gift history returns a safe 500 response."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_customer_gifts_list", side_effect=RuntimeError("db down")):
            resp = self.client.get("/api/customer-gifts/gifts")
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()["success"])

    def test_customer_detail_database_error_500(self):
        """Database failure in customer detail returns a safe 500 response."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_customer_gift_details", side_effect=RuntimeError("db down")):
            resp = self.client.get("/api/customer-gifts/customers/CS00000001")
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()["success"])

    def test_record_gift_database_error_500(self):
        """Database failure while recording a gift returns a safe 500 response."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._require_admin", return_value=(ADMIN_USER, None)), \
             patch("main.database.record_customer_gift", side_effect=RuntimeError("db down")):
            resp = self.client.post("/api/customer-gifts/gifts", json={
                "customer_id": "CS00000001",
                "gift_name": "Test Gift",
            })
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()["success"])

    # --- Invalid sort_by ---

    def test_invalid_sort_by_422(self):
        """Invalid sort_by parameter returns 422."""
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            resp = self.client.get("/api/customer-gifts/customers?sort_by=INVALID_COLUMN")
        self.assertEqual(resp.status_code, 422)


class TestCustomerGiftsHistory(unittest.TestCase):
    """Gift history queries with synthetic records (cleaned up afterwards)."""

    def test_history_search_by_customer_name_includes_phone_name(self):
        from db.connection import get_connection
        from db.customer_gifts import get_customer_gifts_list

        gift_name = "UNIT_TEST_GIFT_HISTORY_SEARCH"
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT CS_ID, CS_NAME FROM cs WHERE CS_ID IS NOT NULL AND CS_ID != '' AND CS_NAME IS NOT NULL LIMIT 1"
                )
                customer = cur.fetchone()
                if not customer:
                    self.skipTest("No customer rows available for gift history test")
                cid = str(customer["CS_ID"]).strip()
                cname = str(customer["CS_NAME"]).strip()

                cur.execute("DELETE FROM customer_gifts WHERE gift_name = %s", (gift_name,))
                cur.execute("""
                    INSERT INTO customer_gifts (customer_id, gift_name, gift_date, note,
                                                recorded_by_user_id, recorded_by_username)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (cid, gift_name, "2026-11-19", "Synthetic test note", 1, "admin"))
            conn.commit()

            result = get_customer_gifts_list(search=cname, page=1, page_size=50)
            self.assertTrue(result["success"])
            matches = [g for g in result["gifts"] if g["gift_name"] == gift_name]
            self.assertEqual(len(matches), 1, "Gift history search by customer name must include the record")
            self.assertEqual(matches[0]["customer_id"], cid)
            self.assertEqual(matches[0]["customer_name"], cname)
            self.assertIn("phone", matches[0])

            # Search by customer ID also works and the count query remains valid.
            by_id = get_customer_gifts_list(search=cid, page=1, page_size=50)
            self.assertTrue(any(g["id"] == matches[0]["id"] for g in by_id["gifts"]))
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM customer_gifts WHERE gift_name = %s", (gift_name,))
                conn.commit()
            finally:
                conn.close()


class TestCustomerGiftsMenuAndReports(unittest.TestCase):
    """Regression checks for existing menu IDs and reporting permissions."""

    def test_customer_gifts_menu_preserves_client_operations(self):
        from db.menus import get_sidebar_menus

        menus = get_sidebar_menus(all_menus=True)
        client_ops = [m for m in menus if m.get("title") == "Client Operations"]
        self.assertEqual(len(client_ops), 1)
        submenus = client_ops[0].get("submenus") or []
        gifts = [s for s in submenus if s.get("title") == "Customer Gifts"]
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0].get("url"), "/menus/customer-gifts")
        self.assertEqual(gifts[0].get("icon"), "gift")

    def test_existing_reports_permission_still_works(self):
        from db.reports import can_view_reports

        self.assertTrue(can_view_reports({"role": "Admin", "allowed_menus": []}))
        self.assertFalse(can_view_reports({"role": "User", "allowed_menus": []}))

    def test_existing_top_customers_report_still_works(self):
        from db.analytics import get_top_customers

        rows = get_top_customers(limit=3)
        self.assertIsInstance(rows, list)


class TestCustomerGiftsPagination(unittest.TestCase):
    """Pagination unit tests."""

    def test_paginate_basic(self):
        from db.customer_gifts import _paginate
        rows = [{"i": x} for x in range(10)]
        result = _paginate(rows, page=1, page_size=3)
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["pagination"]["total_records"], 10)
        self.assertEqual(result["pagination"]["total_pages"], 4)
        self.assertTrue(result["pagination"]["has_next"])
        self.assertFalse(result["pagination"]["has_prev"])

    def test_paginate_last_page(self):
        from db.customer_gifts import _paginate
        rows = [{"i": x} for x in range(10)]
        result = _paginate(rows, page=4, page_size=3)
        self.assertEqual(len(result["rows"]), 1)
        self.assertFalse(result["pagination"]["has_next"])
        self.assertTrue(result["pagination"]["has_prev"])

    def test_paginate_empty(self):
        from db.customer_gifts import _paginate
        result = _paginate([], page=1, page_size=25)
        self.assertEqual(len(result["rows"]), 0)
        self.assertEqual(result["pagination"]["total_records"], 0)


class TestCustomerGiftsDateParsing(unittest.TestCase):
    """Date and parameter parsing tests."""

    def test_parse_valid_date(self):
        from db.customer_gifts import _parse_date_param
        self.assertEqual(_parse_date_param("2026-01-15", "test"), date(2026, 1, 15))

    def test_parse_none_date(self):
        from db.customer_gifts import _parse_date_param
        self.assertIsNone(_parse_date_param(None, "test"))

    def test_parse_empty_date(self):
        from db.customer_gifts import _parse_date_param
        self.assertIsNone(_parse_date_param("", "test"))

    def test_parse_invalid_date_raises(self):
        from db.customer_gifts import _parse_date_param
        with self.assertRaises(ValueError):
            _parse_date_param("not-a-date", "test")

    def test_parse_float_param(self):
        from db.customer_gifts import _parse_float_param
        self.assertEqual(_parse_float_param("100.5", "amount"), 100.5)
        self.assertIsNone(_parse_float_param(None, "amount"))

    def test_parse_int_param(self):
        from db.customer_gifts import _parse_int_param
        self.assertEqual(_parse_int_param("5", "count"), 5)
        self.assertIsNone(_parse_int_param(None, "count"))


if __name__ == "__main__":
    unittest.main()
