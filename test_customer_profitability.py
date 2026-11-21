"""Tests for Report 14: Customer Profitability.

All database tests run in a temporary schema (``zz_cpf_test_<random>``) built
with ``CREATE TABLE ... LIKE`` and dropped afterwards, so no row of the real
business database is inserted, updated or deleted. Only the sidebar menu rows
are copied so the menu migration can be checked in isolation.

Synthetic data (dates relative to today, period = last 60 days):
- ZCPF01 Alpha Traders: invoice with a header discount and a service charge,
  duplicate lines, a second invoice and a costed return -> gross profit 580.
- ZCPF02 Beta Missing Cost: one sale line without cost -> gross profit Unknown.
- ZCPF03 Gamma Return No Cost: return without cost -> gross profit Unknown.
- ZCPF04 Delta Zero Price: free item with a cost -> gross profit -200.
- ZCPF05 Epsilon Old Sale: sale outside the period only.
- ZCPF06 Zeta Mismatch: invoice total does not match its lines -> sales Unknown.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pymysql
from starlette.testclient import TestClient

from db.connection import get_connection
from db import customer_profitability as cpf
from db import customer_profitability_rules as rules
from db.customer_profitability_rules import InvoiceHeader, ReturnFact, SaleFact
from db.report_support import ReportInputError
from isolated_test_schema import IsolatedSchema

SCHEMA = IsolatedSchema("zz_cpf_test_", (
    "cs", "sales", "sales_details", "sales_return", "sales_return_details",
    "product", "category", "sidebar_menus", "collection", "collection_master",
    "journal_vouchar", "payment",
))

ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_BY_SLUG = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": ["customer-profitability"]}
VIEWER_BY_ID = {"id": 6, "username": "viewer2", "role": "viewer", "allowed_menus": [38]}
NO_PERM_USER = {"id": 7, "username": "noperm", "role": "viewer", "allowed_menus": [32, 33, 35, 37]}

T = date.today()
START, END = (T - timedelta(days=60)).isoformat(), T.isoformat()


def _d(offset: int) -> str:
    return (T + timedelta(days=offset)).isoformat()


def _seed(cur) -> None:
    cur.executemany("""
        INSERT INTO cs (CS_ID, CS_NAME, MOBILE, CUSTOMER_TYPE, ACTIVE, STATUS, BALANCE, INDATE)
        VALUES (%s, %s, %s, %s, 'Y', 'C', 0, NOW())
    """, [
        ("ZCPF01", "Alpha Traders", "01810000001", "DEALER"),
        ("ZCPF02", "Beta Missing Cost", "01810000002", "DEALER"),
        ("ZCPF03", "Gamma Return No Cost", "01810000003", "RETAILER"),
        ("ZCPF04", "Delta Zero Price", "01810000004", "DEALER"),
        ("ZCPF05", "Epsilon Old Sale", "01810000005", "DEALER"),
        ("ZCPF06", "Zeta Mismatch", "01810000006", "DEALER"),
        ("ZCPF07", "Eta No Sales", "01810000007", "DEALER"),
    ])
    cur.execute("INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES ('ZCPFCAT', 'ZCPF Supplies', NOW())")
    cur.executemany("""
        INSERT INTO product (PRODUCT_ID, PRODUCT_NAME, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE, UNIT)
        VALUES (%s, %s, 'ZCPFCAT', 0, 'N', 'Y', 'PCS')
    """, [("ZCPFP1", "ZCPF Toner A"), ("ZCPFP2", "ZCPF Ink B"), ("ZCPFP3", "ZCPF Drum C")])

    # (invoice, customer, day offset, header total, header discount, service charge,
    #  [(product, qty, unit price, ORG_COST), ...])
    invoices = [
        ("ZCPF-I01", "ZCPF01", -30, 2000, 200, 50,
         [("ZCPFP1", 1, 500, 300), ("ZCPFP1", 1, 500, 300), ("ZCPFP2", 1, 1000, 600)]),
        ("ZCPF-I02", "ZCPF01", -10, 500, 0, 0, [("ZCPFP1", 1, 500, 320)]),
        ("ZCPF-I03", "ZCPF02", -20, 1300, 0, 0, [("ZCPFP1", 1, 500, 0), ("ZCPFP3", 1, 800, 500)]),
        ("ZCPF-I04", "ZCPF03", -15, 1600, 0, 0, [("ZCPFP3", 2, 800, 500)]),
        ("ZCPF-I05", "ZCPF04", -12, 1000, 0, 0, [("ZCPFP2", 1, 0, 600), ("ZCPFP2", 1, 1000, 600)]),
        ("ZCPF-I06", "ZCPF05", -120, 500, 0, 0, [("ZCPFP1", 1, 500, 300)]),
        ("ZCPF-I07", "ZCPF06", -8, 900, 0, 0, [("ZCPFP3", 1, 800, 500)]),
    ]
    for sales_id, customer, offset, total, discount, service, lines in invoices:
        cur.execute("""
            INSERT INTO sales (SALES_ID, CS_ID, SALES_DATE, TOTAL_PRICE, DISCOUNT, SERVICE_CHARGE, NET_SALES, SALES_TYPE, CASH_CS_NAME)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'CREDIT', '')
        """, (sales_id, customer, _d(offset) + " 11:00:00", total, discount, service, total - discount + service))
        for seq, (product, qty, price, cost) in enumerate(lines, start=1):
            cur.execute("""
                INSERT INTO sales_details (SALES_ID, CS_ID, PRODUCT_ID, SALES_DATE, UNIT_PRICE, QTY, ORG_COST, AVG_COST, SEQUANCE_NO)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (sales_id, customer, product, _d(offset), price, qty, cost, cost, seq))

    # (return, customer, day offset, product, qty, total, ORG_COST, linked invoice)
    for ret_id, customer, offset, product, qty, total, cost, sales_id in [
        ("ZCPF-R01", "ZCPF01", -5, "ZCPFP1", 1, 500, 300, "ZCPF-I01"),
        ("ZCPF-R02", "ZCPF03", -3, "ZCPFP3", 1, 800, None, None),
    ]:
        cur.execute("""
            INSERT INTO sales_return (SALES_RETURN_ID, RETURN_DATE, TOTAL_PRICE, CS_ID, APPROVAL)
            VALUES (%s, %s, %s, %s, 'N')
        """, (ret_id, _d(offset), total, customer))
        cur.execute("""
            INSERT INTO sales_return_details
                (SALES_RETURN_ID, RETURN_DATE, CS_ID, PRODUCT_ID, QTY, UNIT_PRICE, TOTAL_PRICE, ORG_COST, SALES_ID)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (ret_id, _d(offset), customer, product, qty, total / qty, total, cost, sales_id))


def setUpModule():
    SCHEMA.create(seed=_seed)


def tearDownModule():
    SCHEMA.drop()


def _sale(**overrides) -> SaleFact:
    base = dict(sales_id="I1", customer_id="C1", product_id="P1", sale_date=T, quantity=1,
                line_count=1, gross_cents=100000, cost_cents=60000, zero_price_lines=0)
    base.update(overrides)
    return SaleFact(**base)


def _group(sales=(), returns=(), headers=None):
    headers = headers or {s.sales_id: InvoiceHeader(s.sales_id, sum(x.gross_cents or 0 for x in sales if x.sales_id == s.sales_id), 0)
                          for s in sales}
    net = rules.apply_invoice_discounts(sales, headers)
    totals = rules.aggregate(net, returns, key=lambda fact: "all")
    return rules.evaluate_group(totals.get("all", rules.GroupTotals()))


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

class TestProfitabilityRules(unittest.TestCase):
    def test_discount_allocation_is_exact_and_proportional(self):
        self.assertEqual(rules.allocate_discount([100000, 100000], 20000), [10000, 10000])
        self.assertEqual(rules.allocate_discount([10000, 10000, 10000], 10000), [3334, 3333, 3333])
        self.assertEqual(rules.allocate_discount([75000, 25000], 999), [749, 250])
        self.assertEqual(rules.allocate_discount([50000], 0), [0])
        self.assertIsNone(rules.allocate_discount([0, 0], 500))

    def test_gross_profit_and_margin(self):
        row = _group([_sale()])
        self.assertEqual(row["net_sales"], 1000.0)
        self.assertEqual(row["cost_of_sales"], 600.0)
        self.assertEqual(row["gross_profit"], 400.0)
        self.assertEqual(row["gross_margin_pct"], 40.0)
        self.assertEqual(row["profit_status"], "known")
        self.assertEqual(row["cost_coverage_pct"], 100.0)

    def test_missing_cost_makes_gross_profit_unknown_not_zero(self):
        row = _group([_sale(), _sale(product_id="P2", gross_cents=50000, cost_cents=None)])
        self.assertEqual(row["net_sales"], 1500.0)
        self.assertIsNone(row["cost_of_sales"])
        self.assertIsNone(row["gross_profit"])
        self.assertEqual(row["gross_profit_display"], "Unknown")
        self.assertIsNone(row["gross_margin_pct"])
        self.assertEqual(row["profit_status"], "unknown")
        self.assertEqual(row["costed_gross_profit"], 400.0)
        self.assertEqual(row["cost_coverage_pct"], 66.7)
        self.assertEqual(row["uncosted_sales"], 500.0)
        self.assertIn("missing_cost", row["reason_codes"])

    def test_returns_reduce_sales_and_cost(self):
        ret = ReturnFact(customer_id="C1", product_id="P1", return_date=T, quantity=1, amount_cents=50000, cost_cents=30000)
        row = _group([_sale()], [ret])
        self.assertEqual((row["sales"], row["returns"], row["net_sales"]), (1000.0, 500.0, 500.0))
        self.assertEqual(row["cost_of_sales"], 300.0)
        self.assertEqual(row["gross_profit"], 200.0)
        self.assertEqual(row["quantity_returned"], 1)

    def test_return_without_cost_makes_gross_profit_unknown(self):
        ret = ReturnFact(customer_id="C1", product_id="P1", return_date=T, quantity=1, amount_cents=50000, cost_cents=None)
        row = _group([_sale()], [ret])
        self.assertEqual(row["net_sales"], 500.0)
        self.assertIsNone(row["gross_profit"])
        self.assertIn("return_missing_cost", row["reason_codes"])

    def test_return_only_period_and_non_positive_sales_have_no_margin(self):
        ret = ReturnFact(customer_id="C1", product_id="P1", return_date=T, quantity=1, amount_cents=50000, cost_cents=30000)
        row = _group([], [ret])
        self.assertEqual(row["net_sales"], -500.0)
        self.assertEqual(row["gross_profit"], -200.0)
        self.assertIsNone(row["gross_margin_pct"])
        self.assertEqual(row["gross_margin_display"], "Not applicable")

    def test_costed_profit_is_unknown_when_no_line_has_a_cost(self):
        row = _group([_sale(cost_cents=None)])
        self.assertIsNone(row["costed_gross_profit"])
        self.assertEqual(row["costed_gross_profit_display"], "Unknown")
        self.assertEqual(row["cost_coverage_pct"], 0.0)  # sales are known, none of them costed

    def test_negative_or_oversized_discount_makes_sales_unknown(self):
        for discount in (-500, 150000):
            row = _group([_sale()], headers={"I1": InvoiceHeader("I1", 100000, discount)})
            self.assertIsNone(row["sales"])
            self.assertIsNone(row["gross_profit"])
            self.assertIn("discount_unallocatable", row["reason_codes"])
        self.assertIsNone(rules.allocate_discount([100, 100], 201))
        self.assertIsNone(rules.allocate_discount([100, 100], -1))

    def test_zero_price_items_count_their_cost(self):
        row = _group([_sale(gross_cents=0, zero_price_lines=1), _sale(sales_id="I1", product_id="P2")])
        self.assertEqual(row["gross_profit"], -200.0)
        self.assertEqual(row["gross_margin_pct"], -20.0)
        self.assertEqual(row["zero_price_lines"], 1)

    def test_invoice_total_mismatch_and_invalid_price_make_sales_unknown(self):
        mismatch = _group([_sale()], headers={"I1": InvoiceHeader("I1", 90000, 0)})
        self.assertIsNone(mismatch["sales"])
        self.assertIsNone(mismatch["gross_profit"])
        self.assertIn("invoice_total_mismatch", mismatch["reason_codes"])
        missing_header = _group([_sale()], headers={"OTHER": InvoiceHeader("OTHER", 0, 0)})
        self.assertIn("missing_invoice", missing_header["reason_codes"])
        bad_price = _group([_sale(gross_cents=None)], headers={"I1": InvoiceHeader("I1", 0, 0)})
        self.assertIsNone(bad_price["sales"])
        self.assertIn("invalid_price", bad_price["reason_codes"])

    def test_header_discount_reduces_product_sales(self):
        sales = [_sale(product_id="P1", gross_cents=100000, cost_cents=60000),
                 _sale(product_id="P2", gross_cents=100000, cost_cents=60000)]
        net = rules.apply_invoice_discounts(sales, {"I1": InvoiceHeader("I1", 200000, 20000)})
        totals = rules.aggregate(net, [], key=lambda fact: fact.product_id)
        self.assertEqual(rules.evaluate_group(totals["P1"])["sales"], 900.0)
        self.assertEqual(rules.evaluate_group(totals["P2"])["gross_profit"], 300.0)

    def test_filter_validation(self):
        clean = rules.normalize_filters({}, today=T)
        self.assertEqual(clean["end_date"], T)
        self.assertEqual(clean["start_date"], T - timedelta(days=89))
        self.assertEqual((clean["sort_by"], clean["sort_order"]), ("gross_profit", "desc"))
        for bad in ({"start_date": "2026-13-01"}, {"start_date": END, "end_date": START},
                    {"start_date": "2010-01-01", "end_date": END}, {"sort_by": "net_profit"},
                    {"profit_status": "maybe"}, {"customer_id": "bad id!"}, {"page_size": 500}, {"q": "x" * 101}):
            with self.assertRaises(ReportInputError):
                rules.normalize_filters(bad, today=T)

    def test_labels_say_gross_profit_never_net_profit(self):
        metadata = cpf.get_customer_profitability_metadata(today=T)
        text = " ".join([metadata["disclaimer"], metadata["calculation_basis"], *metadata["column_labels"].values()])
        self.assertIn("Gross profit", text)
        self.assertNotIn("Net profit", text)
        self.assertIn("not net profit", metadata["disclaimer"])


# ---------------------------------------------------------------------------
# Database queries (temporary schema)
# ---------------------------------------------------------------------------

class TestProfitabilityQueries(unittest.TestCase):
    def setUp(self):
        cpf.clear_customer_profitability_cache()

    def _report(self, view="customers", **filters):
        filters.setdefault("start_date", START)
        filters.setdefault("end_date", END)
        report = cpf.get_customer_profitability_report(view=view, **filters)
        key = "customer_id" if view == "customers" else "product_id"
        return report, {row[key]: row for row in report["rows"]}

    def test_temporary_schema_is_in_use_and_real_data_is_untouched(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE() AS db")
                self.assertEqual(cur.fetchone()["db"], SCHEMA.name)
                cur.execute(f"SELECT COUNT(*) AS n FROM `{SCHEMA.source}`.sales_details WHERE SALES_ID LIKE 'ZCPF%%'")
                self.assertEqual(cur.fetchone()["n"], 0)

    def test_cache_refresh_and_filtered_summary_flag(self):
        first, _ = self._report()
        failure = pymysql.err.OperationalError(2013, "Lost connection")
        with patch.object(cpf, "get_connection", side_effect=failure):
            with self.assertRaises(pymysql.err.OperationalError):
                self._report(refresh=True)  # the connection is still needed for names
        cpf.clear_customer_profitability_cache()
        self._report()
        with patch.object(cpf, "_fetch_sale_facts", side_effect=AssertionError("facts reloaded")):
            self._report(refresh=True)  # within 10 seconds: cached facts are reused
            with patch.object(cpf, "MIN_REFRESH_SECONDS", 0), self.assertRaises(AssertionError):
                self._report(refresh=True)
        self.assertFalse(first["summary_filtered"])
        self.assertTrue(self._report(q="alpha")[0]["summary_filtered"])
        self.assertTrue(self._report(profit_status="known")[0]["summary_filtered"])

    def test_customer_view_values(self):
        report, rows = self._report(page_size=100)
        self.assertEqual(set(rows), {"ZCPF01", "ZCPF02", "ZCPF03", "ZCPF04", "ZCPF06"})
        alpha = rows["ZCPF01"]
        self.assertEqual(alpha["invoice_count"], 2)
        self.assertEqual(alpha["quantity_sold"], 4)            # duplicate lines are real units
        self.assertEqual(alpha["sales"], 2300.0)               # discount applied, service charge excluded
        self.assertEqual(alpha["returns"], 500.0)
        self.assertEqual(alpha["net_sales"], 1800.0)
        self.assertEqual(alpha["cost_of_sales"], 1220.0)
        self.assertEqual(alpha["gross_profit"], 580.0)
        self.assertEqual(alpha["gross_margin_pct"], 32.2)

        beta = rows["ZCPF02"]
        self.assertIsNone(beta["gross_profit"])
        self.assertEqual(beta["costed_gross_profit"], 300.0)
        self.assertEqual(beta["cost_coverage_pct"], 61.5)
        self.assertIsNone(rows["ZCPF03"]["gross_profit"])
        self.assertIn("return_missing_cost", rows["ZCPF03"]["reason_codes"])
        self.assertEqual(rows["ZCPF04"]["gross_profit"], -200.0)
        self.assertIsNone(rows["ZCPF06"]["sales"])
        self.assertIn("invoice_total_mismatch", rows["ZCPF06"]["reason_codes"])

    def test_summary_sorting_and_pagination(self):
        report, _ = self._report(page_size=2, page=3)
        summary = report["summary"]
        self.assertEqual(summary["row_count"], 5)
        self.assertEqual((summary["known_count"], summary["unknown_count"]), (2, 3))
        self.assertIsNone(summary["gross_profit"])
        self.assertEqual(summary["costed_gross_profit"], 1280.0)
        self.assertEqual(summary["cost_coverage_pct"], 91.9)
        self.assertEqual(summary["returns"], 1300.0)
        self.assertEqual([r["customer_id"] for r in report["rows"]], ["ZCPF06"])
        self.assertEqual(report["pagination"]["total_pages"], 3)

        ordered, _ = self._report(page_size=100)
        self.assertEqual([r["customer_id"] for r in ordered["rows"]], ["ZCPF01", "ZCPF04", "ZCPF02", "ZCPF03", "ZCPF06"])
        ascending, _ = self._report(page_size=100, sort_order="asc")
        self.assertEqual([r["customer_id"] for r in ascending["rows"]][:2], ["ZCPF04", "ZCPF01"])
        self.assertEqual([r["customer_id"] for r in ascending["rows"]][2:], ["ZCPF02", "ZCPF03", "ZCPF06"])

    def test_product_view_and_customer_filter(self):
        report, rows = self._report(view="products", page_size=100)
        self.assertEqual([r["product_id"] for r in report["rows"]], ["ZCPFP2", "ZCPFP3", "ZCPFP1"])
        self.assertEqual(rows["ZCPFP2"]["gross_profit"], 100.0)
        self.assertEqual(rows["ZCPFP2"]["gross_margin_pct"], 5.3)
        self.assertIsNone(rows["ZCPFP1"]["gross_profit"])
        self.assertEqual(rows["ZCPFP1"]["product_name"], "ZCPF Toner A")
        self.assertEqual(rows["ZCPFP1"]["category_name"], "ZCPF Supplies")

        alpha, alpha_rows = self._report(view="products", customer_id="ZCPF01")
        self.assertEqual(alpha["customer"]["customer_name"], "Alpha Traders")
        self.assertEqual([r["product_id"] for r in alpha["rows"]], ["ZCPFP2", "ZCPFP1"])
        self.assertEqual(alpha_rows["ZCPFP1"]["gross_profit"], 280.0)
        self.assertEqual(alpha_rows["ZCPFP1"]["returns"], 500.0)
        self.assertEqual(alpha["summary"]["gross_profit"], 580.0)  # matches the customer view
        with self.assertRaises(LookupError):
            self._report(view="products", customer_id="ZCPF99")

    def test_date_filter_search_and_status(self):
        _, wide = self._report(start_date=_d(-200), page_size=100)
        self.assertEqual(wide["ZCPF05"]["gross_profit"], 200.0)
        self.assertNotIn("ZCPF07", wide)                         # no sales or returns in the period
        self.assertEqual(list(self._report(q="alpha")[1]), ["ZCPF01"])
        self.assertEqual(list(self._report(q="zcpf04")[1]), ["ZCPF04"])
        self.assertEqual(list(self._report(view="products", q="ink")[1]), ["ZCPFP2"])
        self.assertEqual(set(self._report(profit_status="known")[1]), {"ZCPF01", "ZCPF04"})
        self.assertEqual(set(self._report(profit_status="unknown")[1]), {"ZCPF02", "ZCPF03", "ZCPF06"})
        empty, rows = self._report(start_date=_d(-400), end_date=_d(-300))
        self.assertEqual(rows, {})
        self.assertEqual(empty["summary"]["row_count"], 0)

    def test_all_statements_are_read_only(self):
        statements = []
        real = cpf.get_connection

        class RecordingCursor:
            def __init__(self, cursor):
                self._cursor = cursor

            def execute(self, sql, args=None):
                statements.append(" ".join(str(sql).split()))
                return self._cursor.execute(sql, args)

            def __getattr__(self, name):
                return getattr(self._cursor, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._cursor.close()

        class RecordingConnection:
            def __init__(self, conn):
                self._conn = conn

            def cursor(self, *args, **kwargs):
                return RecordingCursor(self._conn.cursor(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._conn.close()

        with patch.object(cpf, "get_connection", lambda: RecordingConnection(real())):
            self._report()
            self._report(view="products", customer_id="ZCPF01")
            cpf.get_customer_profitability_data_range()
        self.assertTrue(statements)
        for sql in statements:
            self.assertTrue(sql.upper().startswith(("SELECT", "START TRANSACTION READ ONLY")), sql[:80])


# ---------------------------------------------------------------------------
# Permissions and API
# ---------------------------------------------------------------------------

class TestProfitabilityApi(unittest.TestCase):
    PATHS = (
        "/api/customer-profitability/bootstrap",
        f"/api/customer-profitability/customers?start_date={START}&end_date={END}",
        f"/api/customer-profitability/products?start_date={START}&end_date={END}&customer_id=ZCPF01",
    )

    @classmethod
    def setUpClass(cls):
        from main import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        cpf.clear_customer_profitability_cache()

    def _as(self, user):
        return patch("main.is_authenticated", return_value=True), patch("main._get_request_user", return_value=user)

    def test_permission_rules(self):
        self.assertTrue(cpf.can_view_customer_profitability(ADMIN_USER))
        self.assertTrue(cpf.can_view_customer_profitability(VIEWER_BY_SLUG))
        self.assertTrue(cpf.can_view_customer_profitability(VIEWER_BY_ID))
        self.assertFalse(cpf.can_view_customer_profitability(NO_PERM_USER))
        self.assertFalse(cpf.can_view_customer_profitability(None))
        self.assertEqual(cpf.resolve_customer_profitability_menu_id(), 38)

    def test_unauthenticated_requests_are_rejected(self):
        with patch("main.is_authenticated", return_value=False):
            page = self.client.get("/customer-profitability", follow_redirects=False)
            responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers.get("location", ""))
        self.assertEqual([r.status_code for r in responses], [401, 401, 401])

    def test_users_without_the_menu_grant_are_rejected(self):
        auth, user = self._as(NO_PERM_USER)
        with auth, user:
            page = self.client.get("/customer-profitability")
            responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual(page.status_code, 403)
        self.assertEqual([r.status_code for r in responses], [403, 403, 403])
        self.assertNotIn("Alpha", "".join(r.text for r in responses))

    def test_permitted_users_get_data(self):
        for viewer in (ADMIN_USER, VIEWER_BY_SLUG, VIEWER_BY_ID):
            auth, user = self._as(viewer)
            with auth, user:
                responses = [self.client.get(path) for path in self.PATHS]
            self.assertEqual([r.status_code for r in responses], [200, 200, 200])
            bootstrap, customers, products = (r.json() for r in responses)
            self.assertEqual(bootstrap["metadata"]["menu_id"], 38)
            self.assertEqual(bootstrap["data_range"]["first_date"], _d(-120))
            self.assertEqual(customers["rows"][0]["customer_id"], "ZCPF01")
            self.assertEqual(products["summary"]["gross_profit"], 580.0)
            unknown = next(r for r in customers["rows"] if r["customer_id"] == "ZCPF02")
            self.assertIsNone(unknown["gross_profit"])                 # JSON null, never 0
            self.assertEqual(unknown["gross_profit_display"], "Unknown")

    def test_invalid_input_and_unknown_customer(self):
        auth, user = self._as(ADMIN_USER)
        with auth, user:
            bad_date = self.client.get("/api/customer-profitability/customers?start_date=2026-02-30")
            bad_range = self.client.get(f"/api/customer-profitability/customers?start_date={END}&end_date={START}")
            bad_sort = self.client.get("/api/customer-profitability/products?sort_by=net_profit")
            bad_id = self.client.get("/api/customer-profitability/products?customer_id=bad%20id!")
            missing = self.client.get("/api/customer-profitability/products?customer_id=ZCPF99")
        self.assertEqual([r.status_code for r in (bad_date, bad_range, bad_sort, bad_id)], [422, 422, 422, 422])
        self.assertIn("start_date", bad_date.json()["message"])
        self.assertEqual(missing.status_code, 404)

    def test_database_errors_do_not_leak_customer_data(self):
        failure = pymysql.err.OperationalError(2013, "Lost connection near 'Alpha Traders' profit 580")
        auth, user = self._as(VIEWER_BY_ID)
        with auth, user, patch.object(cpf, "get_connection", side_effect=failure):
            with self.assertLogs(level="DEBUG") as captured:
                responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual([r.status_code for r in responses], [500, 500, 500])
        for response in responses:
            self.assertNotIn("Alpha", response.text)
            self.assertIn("temporarily unavailable", response.json()["message"])
        self.assertNotIn("Alpha", "\n".join(captured.output))

    def _page(self) -> str:
        auth, user = self._as(ADMIN_USER)
        with auth, user:
            response = self.client.get("/customer-profitability")
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_page_renders_english_gross_profit_labels(self):
        html = self._page()
        for text in ("Customer Profitability", "Client Operations", "Gross profit", "Gross margin",
                     "By customer", "By product", "Unknown", "not net profit", 'data-lucide="banknote"'):
            self.assertIn(text, html)
        self.assertNotIn("Net profit", html)

    def test_page_script_is_valid_and_safe(self):
        script = re.findall(r"<script>(.*?)</script>", self._page(), re.S)[-1]
        for entity in ("'&amp;'", "'&lt;'", "'&gt;'", "'&quot;'", "'&#39;'"):
            self.assertIn(entity, script)
        self.assertIn("data-customer-id", script)
        self.assertIn("data-page", script)
        self.assertNotRegex(script, r"onclick=\"[^\"]*\$\{")
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "customer_profitability.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script)
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_reports_still_work(self):
        auth, user = self._as(ADMIN_USER)
        with auth, user, patch("main._report_user", return_value=ADMIN_USER):
            risk = self.client.get("/api/customer-payment-risk/customers")
            sales = self.client.get(f"/api/reports/sales?customer_id=ZCPF01&start_date={_d(-90)}&end_date={_d(0)}")
            old_stock = self.client.get("/old-stock-buyers")
        self.assertEqual(risk.status_code, 200, risk.text[:200])
        self.assertEqual(sales.status_code, 200, sales.text[:200])
        self.assertEqual(sales.json()["summary"]["revenue"], 2500.0)  # gross line revenue, unchanged report
        self.assertEqual(old_stock.status_code, 200)


class TestProfitabilityMigration(unittest.TestCase):
    def _menus(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, parent_id, title, url, icon, menu_order, is_active, is_disabled FROM sidebar_menus ORDER BY id")
                return {row["id"]: row for row in cur.fetchall()}

    def test_migration_normalizes_menu_38_and_preserves_other_menus(self):
        from db.migrations.migrate_customer_profitability import migrate_up, migration_status
        before = self._menus()
        self.assertTrue(migrate_up())
        self.assertTrue(migrate_up())
        after = self._menus()
        self.assertEqual(set(before), set(after))
        for menu_id, row in before.items():
            if menu_id != 38:
                self.assertEqual(after[menu_id], row)
        menu = migration_status()["menu"]
        self.assertEqual((menu["id"], menu["parent_id"]), (38, 2))
        self.assertEqual(menu["url"], "/customer-profitability")
        self.assertEqual(menu["icon"], "banknote")
        self.assertEqual((menu["is_active"], menu["is_disabled"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
