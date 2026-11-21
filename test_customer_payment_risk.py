"""Tests for Report 13: Customer Payment Risk.

Isolation
---------
Every database test runs inside a temporary schema named ``zz_cpr_test_<random>``.
The schema is created with ``CREATE TABLE ... LIKE`` copies of the real tables
(structure only), filled with synthetic ``ZCPR`` rows, and dropped when the
module finishes. Only the sidebar menu rows are copied, so the migration can be
checked without touching the real ``sidebar_menus`` table. No row in the real
business database is inserted, updated or deleted by this module.

Coverage
--------
- Pure rules: due today, overdue, missing/conflicting due dates, full and
  partial payments, returns, overpayments, unconfirmed cheques, unknown
  BALANCE / CREDIT_LIMIT, exact money arithmetic, sorting, filters.
- Database queries: duplicate invoice lines and collection headers are not
  double counted, list and detail agree, all statements are read-only.
- API: authentication, menu permissions, validation, database errors without
  leaking customer data, page script validity and safe rendering.
- Migration: menu 37 is normalized, existing menu IDs and rows survive.
- Regression: the sales report and dormant clients APIs still work.
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
from db import customer_payment_risk as cpr
from db import customer_payment_risk_rules as rules
from db.customer_payment_risk_rules import CustomerLedger, InvoiceFacts
from isolated_test_schema import IsolatedSchema


CLONED_TABLES = (
    "cs", "sales", "sales_details", "collection", "collection_master",
    "sales_return", "sales_return_details", "journal_vouchar", "payment",
    "sidebar_menus", "product", "category",
)
SCHEMA = IsolatedSchema("zz_cpr_test_", CLONED_TABLES)
SOURCE_DB = SCHEMA.source
TEST_DB = SCHEMA.name

ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_BY_SLUG = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": ["customer-payment-risk"]}
VIEWER_BY_ID = {"id": 6, "username": "viewer2", "role": "viewer", "allowed_menus": [37]}
NO_PERM_USER = {"id": 7, "username": "noperm", "role": "viewer", "allowed_menus": [32, 33, 35]}

T = date.today()
QUOTED_NAME = "Zeta O'Brien \"Overdue\" <b>Partial</b>"


def _d(offset: int) -> str:
    return (T + timedelta(days=offset)).isoformat()


def _seed(cur) -> None:
    customers = [
        ("ZCPR01", QUOTED_NAME, "01710000001", "DEALER", 0, 0),
        ("ZCPR02", "Zeta Paid In Full", "01710000002", "RETAILER", 0, 0),
        ("ZCPR03", "Zeta Return Credit", "01710000003", "DEALER", 0, 0),
        ("ZCPR04", "Zeta Unlinked Payment", "01710000004", "DEALER", 900, 0),
        ("ZCPR05", "Zeta Missing Due Date", "01710000005", "DEALER", 0, 0),
        ("ZCPR06", "Zeta Future Due", "01710000006", "DEALER", 0, None),
        ("ZCPR07", "Zeta Overpaid Credit", "01710000007", "RETAILER", -100, 0),
        ("ZCPR08", "Zeta No Invoices", None, "DEALER", 5000, 350000),
        ("ZCPR09", "Zeta Journal Adjust", "01710000009", "DEALER", 0, 0),
        ("ZCPR10", "Zeta Cheque Pending", "01710000010", "DEALER", 0, 0),
        ("ZCPR11", "Zeta Conflict Due", "01710000011", "DEALER", 0, 0),
    ]
    cur.executemany("""
        INSERT INTO cs (CS_ID, CS_NAME, MOBILE, CUSTOMER_TYPE, ACTIVE, STATUS, BALANCE, CREDIT_LIMIT, INDATE)
        VALUES (%s, %s, %s, %s, 'Y', 'C', %s, %s, NOW())
    """, customers)

    cur.execute("INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES ('ZCPRCAT', 'ZCPR Category', NOW())")
    cur.execute("""
        INSERT INTO product (PRODUCT_ID, PRODUCT_NAME, CATEGORY_ID, SALES_PRICE, SERIAL, ACTIVE, UNIT)
        VALUES ('ZCPRP1', 'ZCPR Toner', 'ZCPRCAT', 100, 'N', 'Y', 'PCS')
    """)

    # (sales_id, customer, sale offset, net amount, [(qty, unit price, due offset or None), ...])
    invoices = [
        ("ZCPR-I01", "ZCPR01", -30, 1000, [(1, 500, -10), (1, 500, -10)]),  # duplicate lines
        ("ZCPR-I02", "ZCPR01", -5, 500, [(1, 500, 0)]),                     # due today
        ("ZCPR-I03", "ZCPR02", -40, 800, [(1, 300, -20), (1, 300, -20), (1, 200, -20)]),
        ("ZCPR-I04", "ZCPR03", -40, 1000, [(1, 1000, -15)]),
        ("ZCPR-I05", "ZCPR03", -20, 200, [(1, 200, -2)]),
        ("ZCPR-I06", "ZCPR04", -60, 900, [(1, 900, -30)]),
        ("ZCPR-I07", "ZCPR05", -10, 300, [(1, 300, None)]),                 # no due date
        ("ZCPR-I08", "ZCPR05", -50, 100, [(1, 100, -40)]),
        ("ZCPR-I09", "ZCPR06", -3, 400, [(1, 400, 10)]),                    # due in 10 days
        ("ZCPR-I10", "ZCPR07", -20, 500, [(1, 500, -5)]),
        ("ZCPR-I11", "ZCPR09", -30, 700, [(1, 700, -10)]),
        ("ZCPR-I12", "ZCPR10", -30, 600, [(1, 600, -5)]),
        ("ZCPR-I13", "ZCPR11", -15, 250, [(1, 150, -5), (1, 100, -3)]),     # lines disagree
    ]
    for sales_id, customer, offset, amount, lines in invoices:
        cur.execute("""
            INSERT INTO sales (SALES_ID, CS_ID, SALES_DATE, TOTAL_PRICE, NET_SALES, SALES_TYPE, CASH_CS_NAME)
            VALUES (%s, %s, %s, %s, %s, 'CREDIT', '')
        """, (sales_id, customer, _d(offset) + " 10:00:00", amount, amount))
        for qty, price, due in lines:
            cur.execute("""
                INSERT INTO sales_details (SALES_ID, CS_ID, PRODUCT_ID, SALES_DATE, PAYMENT_DATE, UNIT_PRICE, QTY)
                VALUES (%s, %s, 'ZCPRP1', %s, %s, %s, %s)
            """, (sales_id, customer, _d(offset), None if due is None else _d(due), price, qty))

    collections = [
        ("ZCPR-C01", "ZCPR01", -20, "CASH", 400, "ZCPR-I01", ""),
        ("ZCPR-C02", "ZCPR02", -30, "CASH", 500, "ZCPR-I03", ""),
        ("ZCPR-C03", "ZCPR02", -25, "BEFTN", 300, "ZCPR-I03", ""),
        ("ZCPR-C04", "ZCPR03", -20, "CASH", 700, "ZCPR-I04", ""),
        ("ZCPR-C05", "ZCPR04", -40, "CASH", 900, None, ""),          # not linked to an invoice
        ("ZCPR-C06", "ZCPR05", -45, "CASH", 100, "ZCPR-I08", ""),
        ("ZCPR-C07", "ZCPR06", -1, "CASH", 100, None, ""),           # not linked to an invoice
        ("ZCPR-C08", "ZCPR07", -10, "CASH", 600, "ZCPR-I10", ""),
        ("ZCPR-C09", "ZCPR10", -6, "CHEQUE", 600, "ZCPR-I12", "H"),  # status not confirmed
    ]
    for coll_id, customer, offset, kind, amount, sales_id, cheque in collections:
        cur.execute("""
            INSERT INTO collection (COLLECTION_ID, COLLECTION_DATE, CS_ID, COLLECTION_TYPE, AMOUNT, SALES_ID, CHEQUE_STATUS, STATUS, APPROVAL)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'N', 'N')
        """, (coll_id, _d(offset), customer, kind, amount, sales_id, cheque))
        # The header table repeats the same money; the report must not add it again.
        cur.execute("""
            INSERT INTO collection_master (COLLECTION_ID, COLLECTION_DATE, CS_ID, AMOUNT, APPROVAL)
            VALUES (%s, %s, %s, %s, 'N')
        """, (coll_id, _d(offset), customer, amount))

    for ret_id, customer, offset, sales_id, amount in [
        ("ZCPR-R01", "ZCPR03", -18, "ZCPR-I04", 300),
        ("ZCPR-R02", "ZCPR03", -10, "ZCPR-I05", 50),
    ]:
        cur.execute("""
            INSERT INTO sales_return (SALES_RETURN_ID, RETURN_DATE, TOTAL_PRICE, CS_ID, APPROVAL)
            VALUES (%s, %s, %s, %s, 'N')
        """, (ret_id, _d(offset), amount, customer))
        cur.execute("""
            INSERT INTO sales_return_details
                (SALES_RETURN_ID, RETURN_DATE, CS_ID, PRODUCT_ID, QTY, UNIT_PRICE, TOTAL_PRICE, SALES_ID)
            VALUES (%s, %s, %s, 'ZCPRP1', 1, %s, %s, %s)
        """, (ret_id, _d(offset), customer, amount, amount, sales_id))

    cur.execute("""
        INSERT INTO journal_vouchar (JR_VOUCHAR_ID, JR_VOUCHAR_DATE, CSID, AMOUNT, JR_VOUCHAR_TYPE, APPROVAL)
        VALUES ('ZCPR-J01', %s, 'ZCPR09', 100, 'CR', 'N')
    """, (_d(-12),))


def setUpModule():
    SCHEMA.create(seed=_seed)


def tearDownModule():
    SCHEMA.drop()


def _invoice(**overrides) -> InvoiceFacts:
    base = dict(
        sales_id="INV1", sales_date=T - timedelta(days=30), amount_cents=100000, sales_type="CREDIT",
        line_count=1, dated_line_count=1, due_min=T - timedelta(days=10), due_max=T - timedelta(days=10),
    )
    base.update(overrides)
    return InvoiceFacts(**base)


def _ledger(invoices=(), **overrides) -> CustomerLedger:
    base = dict(customer_id="C1", name="Customer One", phone="01700000000", customer_type="DEALER",
                balance_raw=0.0, credit_limit_raw=0.0, invoices=tuple(invoices))
    base.update(overrides)
    return CustomerLedger(**base)


# ---------------------------------------------------------------------------
# Pure rules (no database)
# ---------------------------------------------------------------------------

class TestInvoiceRules(unittest.TestCase):
    def test_invoice_due_today_is_not_overdue_until_next_day(self):
        inv = _invoice(due_min=T, due_max=T)
        today = rules.evaluate_invoice(inv, allocation_reliable=True, today=T)
        self.assertFalse(today["is_overdue"])
        self.assertEqual(today["due_status"], "due_today")
        self.assertEqual(today["overdue_amount"], 0)
        tomorrow = rules.evaluate_invoice(inv, allocation_reliable=True, today=T + timedelta(days=1))
        self.assertTrue(tomorrow["is_overdue"])
        self.assertEqual(tomorrow["days_overdue"], 1)
        self.assertEqual(tomorrow["overdue_amount"], 1000.0)

    def test_missing_due_date_leaves_overdue_unknown(self):
        result = rules.evaluate_invoice(_invoice(dated_line_count=0, due_min=None, due_max=None), True, T)
        self.assertIsNone(result["is_overdue"])
        self.assertIsNone(result["overdue_amount"])
        self.assertEqual(result["due_date_display"], "Unknown")
        self.assertEqual(result["unpaid_amount"], 1000.0)
        self.assertIn("missing_due_date", result["reason_codes"])

    def test_conflicting_due_dates_are_unknown(self):
        partly_dated = rules.evaluate_invoice(_invoice(line_count=2, dated_line_count=1), True, T)
        differing = rules.evaluate_invoice(_invoice(line_count=2, dated_line_count=2, due_max=T), True, T)
        for result in (partly_dated, differing):
            self.assertIsNone(result["due_date"])
            self.assertEqual(result["due_date_status"], "conflict")
            self.assertIsNone(result["is_overdue"])
            self.assertIn("conflicting_due_date", result["reason_codes"])

    def test_full_payment_settles_invoice_after_due_date(self):
        result = rules.evaluate_invoice(_invoice(payment_cents=60000, payment_rows=2, return_cents=40000), True, T)
        self.assertEqual(result["payment_status"], "paid")
        self.assertEqual(result["unpaid_amount"], 0)
        self.assertFalse(result["is_overdue"])

    def test_partial_payment_leaves_remaining_amount_overdue(self):
        result = rules.evaluate_invoice(_invoice(payment_cents=40000, payment_rows=1), True, T)
        self.assertEqual(result["payment_status"], "partially_paid")
        self.assertEqual(result["unpaid_amount"], 600.0)
        self.assertEqual(result["overdue_amount"], 600.0)
        self.assertEqual(result["days_overdue"], 10)

    def test_overpayment_is_reported_as_credit_not_negative_unpaid(self):
        result = rules.evaluate_invoice(_invoice(payment_cents=120000, payment_rows=1), True, T)
        self.assertEqual(result["payment_status"], "overpaid")
        self.assertEqual(result["unpaid_amount"], 0)
        self.assertEqual(result["credit_amount"], 200.0)

    def test_unreliable_allocation_keeps_unpaid_unknown_never_zero(self):
        past_due = rules.evaluate_invoice(_invoice(), allocation_reliable=False, today=T)
        self.assertIsNone(past_due["unpaid_amount"])
        self.assertEqual(past_due["unpaid_display"], "Unknown")
        self.assertIsNone(past_due["is_overdue"])
        future_due = rules.evaluate_invoice(
            _invoice(due_min=T + timedelta(days=3), due_max=T + timedelta(days=3)), False, T)
        self.assertIsNone(future_due["unpaid_amount"])
        self.assertFalse(future_due["is_overdue"])  # not yet due, whatever was paid
        self.assertEqual(future_due["due_status"], "not_yet_due")

    def test_unconfirmed_cheque_and_missing_amount_are_unknown(self):
        cheque = rules.evaluate_invoice(_invoice(payment_cents=100000, payment_rows=1, unconfirmed_payment_rows=1), True, T)
        self.assertIsNone(cheque["unpaid_amount"])
        self.assertIn("unconfirmed_payments", cheque["reason_codes"])
        no_amount = rules.evaluate_invoice(_invoice(amount_cents=None), True, T)
        self.assertIsNone(no_amount["amount"])
        self.assertEqual(no_amount["amount_display"], "Unknown")
        self.assertIsNone(no_amount["unpaid_amount"])
        self.assertIn("missing_invoice_amount", no_amount["reason_codes"])

    def test_money_is_exact_in_cents(self):
        self.assertEqual(rules.to_cents(1234.56005859375), 123456)  # float32 storage noise
        self.assertIsNone(rules.to_cents(None))
        self.assertIsNone(rules.to_cents("not a number"))
        inv = _invoice(amount_cents=rules.to_cents(0.3), payment_cents=rules.to_cents(0.1) + rules.to_cents(0.2))
        self.assertEqual(rules.evaluate_invoice(inv, True, T)["payment_status"], "paid")
        self.assertEqual(rules.format_money(123456), "BDT 1,234.56")
        self.assertEqual(rules.format_money(100000), "BDT 1,000")
        self.assertEqual(rules.format_money(None), "Unknown")


class TestCustomerRules(unittest.TestCase):
    def test_unknown_balance_and_credit_limit_are_never_zero(self):
        row = rules.evaluate_customer(_ledger(balance_raw=900.0, credit_limit_raw=0.0), T)
        self.assertIsNone(row["outstanding_balance"])
        self.assertEqual(row["outstanding_balance_display"], "Unknown")
        self.assertIsNone(row["credit_limit"])
        self.assertEqual(row["credit_limit_display"], "Unknown")

    def test_balance_and_credit_limit_only_shown_once_meaning_is_confirmed(self):
        self.assertIsNone(rules.interpret_balance(900.0, rules.BALANCE_UNVERIFIED))
        self.assertEqual(rules.interpret_balance(900.0, rules.BALANCE_RECEIVABLE_POSITIVE), 90000)
        self.assertIsNone(rules.interpret_balance(None, rules.BALANCE_RECEIVABLE_POSITIVE))
        self.assertIsNone(rules.interpret_credit_limit(50000.0, rules.CREDIT_LIMIT_UNVERIFIED))
        self.assertEqual(rules.interpret_credit_limit(50000.0, rules.CREDIT_LIMIT_AMOUNT), 5000000)
        self.assertIsNone(rules.interpret_credit_limit(0.0, rules.CREDIT_LIMIT_AMOUNT))
        self.assertIsNone(rules.interpret_credit_limit(None, rules.CREDIT_LIMIT_AMOUNT))

    def test_unlinked_payments_make_customer_unknown(self):
        row = rules.evaluate_customer(_ledger([_invoice()], unlinked_payment_rows=1, unlinked_payment_cents=5000), T)
        self.assertFalse(row["allocation_reliable"])
        self.assertEqual(row["overdue_status"], "unknown")
        self.assertIsNone(row["unpaid_total"])
        self.assertIsNone(row["overdue_amount"])
        self.assertIn("unlinked_payments", row["reason_codes"])

    def test_partial_knowledge_reports_confirmed_minimum(self):
        known = _invoice(sales_id="A")
        no_due = _invoice(sales_id="B", dated_line_count=0, due_min=None, due_max=None)
        row = rules.evaluate_customer(_ledger([known, no_due]), T)
        self.assertEqual(row["overdue_status"], "overdue")
        self.assertIsNone(row["overdue_amount"])
        self.assertEqual(row["overdue_amount_confirmed"], 1000.0)
        self.assertEqual(row["overdue_display"], "At least BDT 1,000")
        self.assertTrue(row["days_overdue_is_minimum"])
        self.assertEqual(row["unpaid_total"], 2000.0)

    def test_customer_without_invoices(self):
        row = rules.evaluate_customer(_ledger(phone=None, name=None), T)
        self.assertEqual(row["overdue_status"], "no_invoices")
        self.assertEqual(row["invoice_count"], 0)
        self.assertEqual(row["customer_name"], "Unknown")
        self.assertEqual(row["phone_display"], "Unknown")

    def test_sorting_puts_unknown_last_in_both_directions(self):
        rows = [
            {"customer_id": "A", "customer_name": "A", "_overdue_sort": None},
            {"customer_id": "B", "customer_name": "B", "_overdue_sort": 5},
            {"customer_id": "C", "customer_name": "C", "_overdue_sort": 0},
        ]
        desc = rules.sort_customers(rows, "overdue_amount", "desc")
        asc = rules.sort_customers(rows, "overdue_amount", "asc")
        self.assertEqual([r["customer_id"] for r in desc], ["B", "C", "A"])
        self.assertEqual([r["customer_id"] for r in asc], ["C", "B", "A"])

    def test_filter_validation(self):
        clean = rules.normalize_report_filters({})
        self.assertEqual(clean["activity"], "with_invoices")
        self.assertEqual(clean["sort_by"], "overdue_amount")
        for bad in ({"sort_by": "risk_score"}, {"overdue_status": "late"}, {"sort_order": "up"},
                    {"activity": "x"}, {"page": 0}, {"page_size": 1000}, {"q": "x" * 101}):
            with self.assertRaises(rules.PaymentRiskInputError):
                rules.normalize_report_filters(bad)
        self.assertEqual(rules.validate_customer_id(" ZCPR01 "), "ZCPR01")
        for bad in ("", "bad id!", "X" * 11, None):
            with self.assertRaises(rules.PaymentRiskInputError):
                rules.validate_customer_id(bad)


# ---------------------------------------------------------------------------
# Database queries (temporary schema with synthetic rows)
# ---------------------------------------------------------------------------

class TestPaymentRiskQueries(unittest.TestCase):
    def setUp(self):
        cpr.clear_customer_payment_risk_cache()

    def test_report_cache_and_refresh(self):
        first = cpr.get_customer_payment_risk_report()
        failure = pymysql.err.OperationalError(2013, "Lost connection")
        with patch.object(cpr, "get_connection", side_effect=failure):
            cached = cpr.get_customer_payment_risk_report(q="zcpr01")  # served from the 60-second cache
            throttled = cpr.get_customer_payment_risk_report(refresh=True)  # refresh within 10 s reuses data
            with patch.object(cpr, "MIN_REFRESH_SECONDS", 0), self.assertRaises(pymysql.err.OperationalError):
                cpr.get_customer_payment_risk_report(refresh=True)
            with self.assertRaises(pymysql.err.OperationalError):
                cpr.get_customer_payment_risk_report(today=T + timedelta(days=1))  # new as-of date reloads
        self.assertEqual(first["summary"]["customer_count"], 10)
        self.assertEqual(throttled["summary"]["customer_count"], 10)
        self.assertEqual([r["customer_id"] for r in cached["customers"]], ["ZCPR01"])
        self.assertTrue(cached["loaded_at_display"])

    def _rows(self, **filters):
        report = cpr.get_customer_payment_risk_report(**filters)
        return report, {row["customer_id"]: row for row in report["customers"]}

    def test_temporary_schema_is_in_use_and_real_data_is_untouched(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE() AS db")
                self.assertEqual(cur.fetchone()["db"], TEST_DB)
                cur.execute(f"SELECT COUNT(*) AS n FROM `{SOURCE_DB}`.cs WHERE CS_ID LIKE 'ZCPR%%'")
                self.assertEqual(cur.fetchone()["n"], 0)
                cur.execute(f"SELECT COUNT(*) AS n FROM `{SOURCE_DB}`.sales WHERE SALES_ID LIKE 'ZCPR%%'")
                self.assertEqual(cur.fetchone()["n"], 0)

    def test_report_values_per_customer(self):
        report, rows = self._rows(page_size=100)
        self.assertEqual(set(rows), {f"ZCPR{n:02d}" for n in range(1, 12)} - {"ZCPR08"})

        overdue = rows["ZCPR01"]
        self.assertEqual(overdue["invoice_count"], 2)
        self.assertEqual(overdue["invoiced_total"], 1500.0)     # duplicate lines not double counted
        self.assertEqual(overdue["collected_total"], 400.0)     # collection_master not added again
        self.assertEqual(overdue["unpaid_total"], 1100.0)
        self.assertEqual(overdue["overdue_status"], "overdue")
        self.assertEqual(overdue["overdue_amount"], 600.0)      # invoice due today is excluded
        self.assertEqual(overdue["days_overdue"], 10)
        self.assertEqual(overdue["customer_name"], QUOTED_NAME)

        paid = rows["ZCPR02"]
        self.assertEqual((paid["invoiced_total"], paid["collected_total"], paid["unpaid_total"]), (800.0, 800.0, 0.0))
        self.assertEqual(paid["overdue_status"], "not_overdue")

        returned = rows["ZCPR03"]
        self.assertEqual(returned["returned_total"], 350.0)
        self.assertEqual(returned["unpaid_total"], 150.0)
        self.assertEqual(returned["overdue_amount"], 150.0)
        self.assertEqual(returned["days_overdue"], 2)

        unlinked = rows["ZCPR04"]
        self.assertIsNone(unlinked["unpaid_total"])
        self.assertIsNone(unlinked["overdue_amount"])
        self.assertEqual(unlinked["overdue_status"], "unknown")
        self.assertIsNone(unlinked["outstanding_balance"])
        self.assertEqual(unlinked["outstanding_balance_display"], "Unknown")

        self.assertEqual(rows["ZCPR05"]["overdue_status"], "unknown")
        self.assertEqual(rows["ZCPR05"]["unpaid_total"], 300.0)
        self.assertEqual(rows["ZCPR06"]["overdue_status"], "not_overdue")
        self.assertIsNone(rows["ZCPR06"]["unpaid_total"])
        self.assertEqual(rows["ZCPR07"]["unpaid_total"], 0.0)
        self.assertIn("other_ledger_entries", rows["ZCPR09"]["reason_codes"])
        self.assertIn("unconfirmed_payments", rows["ZCPR10"]["reason_codes"])
        self.assertIn("conflicting_due_date", rows["ZCPR11"]["reason_codes"])

    def test_summary_counts_cover_filtered_set_not_page(self):
        report, _ = self._rows(page_size=3, page=1)
        summary = report["summary"]
        self.assertEqual(summary["customer_count"], 10)
        self.assertEqual(summary["overdue_customers"], 2)
        self.assertEqual(summary["not_overdue_customers"], 3)
        self.assertEqual(summary["unknown_customers"], 5)
        self.assertEqual(summary["confirmed_overdue_amount"], 750.0)
        self.assertEqual(summary["confirmed_unpaid_amount"], 1800.0)
        self.assertEqual(summary["unpaid_unknown_customers"], 4)
        self.assertFalse(summary["overdue_amount_complete"])
        reasons = {item["code"]: item["count"] for item in summary["unknown_reasons"]}
        self.assertEqual(reasons["unlinked_payments"], 2)
        self.assertEqual(report["as_of"], T.isoformat())

    def test_sorting_and_pagination(self):
        report, _ = self._rows(sort_by="overdue_amount", sort_order="desc", page_size=100)
        expected_desc = ["ZCPR01", "ZCPR03", "ZCPR06", "ZCPR07", "ZCPR02",
                         "ZCPR10", "ZCPR11", "ZCPR09", "ZCPR05", "ZCPR04"]
        self.assertEqual([r["customer_id"] for r in report["customers"]], expected_desc)
        asc, _ = self._rows(sort_by="overdue_amount", sort_order="asc", page_size=100)
        self.assertEqual([r["customer_id"] for r in asc["customers"]][:5], ["ZCPR06", "ZCPR07", "ZCPR02", "ZCPR03", "ZCPR01"])
        self.assertEqual([r["customer_id"] for r in asc["customers"]][5:], expected_desc[5:])

        last, _ = self._rows(page=4, page_size=3)
        self.assertEqual(last["pagination"]["total_records"], 10)
        self.assertEqual(last["pagination"]["total_pages"], 4)
        self.assertEqual([r["customer_id"] for r in last["customers"]], ["ZCPR04"])
        self.assertFalse(last["pagination"]["has_next"])
        self.assertTrue(last["pagination"]["has_prev"])

    def test_search_and_filters(self):
        self.assertEqual(list(self._rows(q="o'brien")[1]), ["ZCPR01"])
        self.assertEqual(list(self._rows(q="zcpr03")[1]), ["ZCPR03"])
        self.assertEqual(list(self._rows(q="0171 000 0002")[1]), ["ZCPR02"])
        self.assertEqual(set(self._rows(customer_type="retailer")[1]), {"ZCPR02", "ZCPR07"})
        self.assertEqual(set(self._rows(overdue_status="overdue")[1]), {"ZCPR01", "ZCPR03"})
        self.assertEqual(len(self._rows(activity="all", page_size=100)[1]), 11)
        self.assertEqual(list(self._rows(activity="all", overdue_status="no_invoices")[1]), ["ZCPR08"])
        empty, rows = self._rows(q="nobody-matches-this")
        self.assertEqual(rows, {})
        self.assertEqual(empty["summary"]["customer_count"], 0)

    def test_detail_matches_list_and_lists_history(self):
        _, rows = self._rows(page_size=100)
        detail = cpr.get_customer_payment_risk_detail("ZCPR01")
        customer = detail["customer"]
        for key in ("invoiced_total", "collected_total", "unpaid_total", "overdue_amount", "days_overdue", "overdue_status"):
            self.assertEqual(customer[key], rows["ZCPR01"][key], key)
        invoices = detail["invoices"]
        self.assertEqual([i["sales_id"] for i in invoices], ["ZCPR-I02", "ZCPR-I01"])
        self.assertEqual(invoices[0]["due_status"], "due_today")
        self.assertFalse(invoices[0]["is_overdue"])
        self.assertEqual(invoices[1]["line_count"], 2)
        self.assertEqual(invoices[1]["amount"], 1000.0)
        self.assertEqual(invoices[1]["paid_amount"], 400.0)
        self.assertEqual(invoices[1]["days_overdue"], 10)
        self.assertEqual([p["collection_id"] for p in detail["payments"]], ["ZCPR-C01"])
        self.assertEqual(detail["payments"][0]["linked_invoice"], "ZCPR-I01")

        returns = cpr.get_customer_payment_risk_detail("ZCPR03")["returns"]
        self.assertEqual([r["return_id"] for r in returns], ["ZCPR-R02", "ZCPR-R01"])

        raw = cpr.get_customer_payment_risk_detail("ZCPR04")["customer"]
        self.assertEqual(raw["recorded_balance"], 900.0)
        self.assertIsNone(raw["outstanding_balance"])
        self.assertEqual(cpr.get_customer_payment_risk_detail("ZCPR08")["invoices"], [])

    def test_detail_unknown_customer(self):
        with self.assertRaises(LookupError):
            cpr.get_customer_payment_risk_detail("ZCPR99")

    def test_all_statements_are_read_only(self):
        statements = []
        real_get_connection = cpr.get_connection

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

        with patch.object(cpr, "get_connection", lambda: RecordingConnection(real_get_connection())):
            cpr.get_customer_payment_risk_report(activity="all")
            cpr.get_customer_payment_risk_detail("ZCPR03")
            cpr.get_customer_payment_risk_filter_options()
        self.assertTrue(statements)
        for sql in statements:
            self.assertTrue(sql.upper().startswith(("SELECT", "START TRANSACTION READ ONLY")), sql[:80])


# ---------------------------------------------------------------------------
# Permissions and API
# ---------------------------------------------------------------------------

def _client() -> TestClient:
    from main import app
    return TestClient(app, raise_server_exceptions=False)


class TestPaymentRiskPermissions(unittest.TestCase):
    def test_menu_grants(self):
        self.assertTrue(cpr.can_view_customer_payment_risk(ADMIN_USER))
        self.assertTrue(cpr.can_view_customer_payment_risk(VIEWER_BY_SLUG))
        self.assertTrue(cpr.can_view_customer_payment_risk(VIEWER_BY_ID))
        self.assertTrue(cpr.can_view_customer_payment_risk({"role": "viewer", "allowed_menus": "[37]"}))
        self.assertTrue(cpr.can_view_customer_payment_risk({"role": "viewer", "allowed_menus": "*"}))
        self.assertFalse(cpr.can_view_customer_payment_risk(NO_PERM_USER))
        self.assertFalse(cpr.can_view_customer_payment_risk({"role": "viewer", "allowed_menus": []}))
        self.assertFalse(cpr.can_view_customer_payment_risk(None))
        self.assertEqual(cpr.resolve_customer_payment_risk_menu_id(), 37)


class TestPaymentRiskApi(unittest.TestCase):
    PATHS = (
        "/api/customer-payment-risk/bootstrap",
        "/api/customer-payment-risk/customers",
        "/api/customer-payment-risk/customer?customer_id=ZCPR01",
    )

    @classmethod
    def setUpClass(cls):
        cls.client = _client()

    def setUp(self):
        cpr.clear_customer_payment_risk_cache()

    def _as(self, user):
        return patch("main.is_authenticated", return_value=True), patch("main._get_request_user", return_value=user)

    def test_unauthenticated_requests_are_rejected(self):
        with patch("main.is_authenticated", return_value=False):
            page = self.client.get("/customer-payment-risk", follow_redirects=False)
            responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers.get("location", ""))
        self.assertEqual([r.status_code for r in responses], [401, 401, 401])

    def test_users_without_the_menu_grant_are_rejected(self):
        auth, user = self._as(NO_PERM_USER)
        with auth, user:
            page = self.client.get("/customer-payment-risk")
            responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual(page.status_code, 403)
        self.assertEqual([r.status_code for r in responses], [403, 403, 403])
        self.assertNotIn("Zeta", "".join(r.text for r in responses))

    def test_permitted_users_get_report_and_detail(self):
        for viewer in (ADMIN_USER, VIEWER_BY_SLUG, VIEWER_BY_ID):
            auth, user = self._as(viewer)
            with auth, user:
                bootstrap = self.client.get("/api/customer-payment-risk/bootstrap")
                report = self.client.get("/api/customer-payment-risk/customers?overdue_status=overdue&sort_by=days_overdue")
                detail = self.client.get("/api/customer-payment-risk/customer?customer_id=ZCPR04")
            self.assertEqual(bootstrap.status_code, 200)
            self.assertEqual(bootstrap.json()["customer_types"], ["DEALER", "RETAILER"])
            self.assertEqual(bootstrap.json()["metadata"]["menu_id"], 37)
            self.assertEqual(report.status_code, 200)
            self.assertEqual([r["customer_id"] for r in report.json()["customers"]], ["ZCPR01", "ZCPR03"])
            self.assertEqual(detail.status_code, 200)
            body = detail.json()["customer"]
            self.assertIsNone(body["unpaid_total"])            # JSON null, not 0
            self.assertEqual(body["unpaid_display"], "Unknown")

    def test_invalid_input_and_missing_customer(self):
        auth, user = self._as(ADMIN_USER)
        with auth, user:
            bad_sort = self.client.get("/api/customer-payment-risk/customers?sort_by=risk_score")
            bad_status = self.client.get("/api/customer-payment-risk/customers?overdue_status=late")
            bad_page = self.client.get("/api/customer-payment-risk/customers?page=0")
            bad_id = self.client.get("/api/customer-payment-risk/customer?customer_id=bad%20id!")
            missing = self.client.get("/api/customer-payment-risk/customer?customer_id=ZCPR99")
        self.assertEqual(bad_sort.status_code, 422)
        self.assertIn("sort_by", bad_sort.json()["message"])
        self.assertEqual(bad_status.status_code, 422)
        self.assertEqual(bad_page.status_code, 422)
        self.assertEqual(bad_id.status_code, 422)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["message"], "Customer not found.")

    def test_internal_lookup_errors_are_not_reported_as_missing_customer(self):
        auth, user = self._as(ADMIN_USER)
        with auth, user, patch.object(cpr, "_load_ledgers", side_effect=KeyError("CS_ID")):
            response = self.client.get("/api/customer-payment-risk/customer?customer_id=ZCPR01")
        self.assertEqual(response.status_code, 500)

    def test_database_errors_do_not_leak_customer_data(self):
        secret = "Zeta O'Brien owes BDT 600"
        failure = pymysql.err.OperationalError(2013, f"Lost connection near '{secret}'")
        auth, user = self._as(VIEWER_BY_ID)
        with auth, user, patch.object(cpr, "get_connection", side_effect=failure):
            with self.assertLogs(level="DEBUG") as captured:
                responses = [self.client.get(path) for path in self.PATHS]
        self.assertEqual([r.status_code for r in responses], [500, 500, 500])
        for response in responses:
            self.assertNotIn("Zeta", response.text)
            self.assertNotIn("600", response.text)
            self.assertIn("temporarily unavailable", response.json()["message"])
        log_text = "\n".join(captured.output)
        self.assertNotIn("Zeta", log_text)
        self.assertNotIn("BDT 600", log_text)

    def _page(self):
        auth, user = self._as(ADMIN_USER)
        with auth, user:
            response = self.client.get("/customer-payment-risk")
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_page_renders_english_labels(self):
        html = self._page()
        for text in ("Customer Payment Risk", "Client Operations", "As of", "Unknown",
                     "does not predict", "Outstanding balance", "Credit limit", "Days overdue"):
            self.assertIn(text, html)
        self.assertIn('data-lucide="alert-triangle"', html)

    def test_page_script_is_valid_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        scripts = re.findall(r"<script>(.*?)</script>", self._page(), re.S)
        self.assertTrue(scripts, "page has no inline script")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "customer_payment_risk.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(scripts[-1])
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_script_escapes_html_and_uses_data_attributes(self):
        script = re.findall(r"<script>(.*?)</script>", self._page(), re.S)[-1]
        for entity in ("'&amp;'", "'&lt;'", "'&gt;'", "'&quot;'", "'&#39;'"):
            self.assertIn(entity, script)
        self.assertIn("data-customer-id", script)
        self.assertIn("data-page", script)
        self.assertNotRegex(script, r"onclick=\"[^\"]*\$\{")  # no inline handlers built from data
        self.assertNotIn("sendSms", script)


# ---------------------------------------------------------------------------
# Migration and regression
# ---------------------------------------------------------------------------

class TestPaymentRiskMigration(unittest.TestCase):
    def _menus(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, parent_id, title, url, icon, menu_order, is_active, is_disabled FROM sidebar_menus ORDER BY id")
                return {row["id"]: row for row in cur.fetchall()}

    def test_migration_normalizes_menu_37_and_preserves_other_menus(self):
        from db.migrations.migrate_customer_payment_risk import migrate_up, migration_status
        before = self._menus()
        self.assertTrue(migrate_up())
        self.assertTrue(migrate_up())  # idempotent
        after = self._menus()
        self.assertEqual(set(before), set(after))
        for menu_id, row in before.items():
            if menu_id != 37:
                self.assertEqual(after[menu_id], row)
        menu = migration_status()["menu"]
        self.assertEqual(menu["id"], 37)
        self.assertEqual(menu["parent_id"], 2)
        self.assertEqual(menu["title"], "Customer Payment Risk")
        self.assertEqual(menu["url"], "/customer-payment-risk")
        self.assertEqual(menu["icon"], "alert-triangle")
        self.assertEqual((menu["is_active"], menu["is_disabled"]), (1, 0))
        for menu_id in (32, 33, 35):
            self.assertIn(menu_id, after)

    def test_migration_inserts_menu_when_missing(self):
        from db.migrations.migrate_customer_payment_risk import migrate_up, migration_status
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sidebar_menus WHERE id = 37")
                original = cur.fetchone()
                cur.execute("DELETE FROM sidebar_menus WHERE id = 37")
            conn.commit()

        def restore():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM sidebar_menus WHERE LOWER(title) = 'customer payment risk'")
                    columns = ", ".join(f"`{key}`" for key in original)
                    marks = ", ".join(["%s"] * len(original))
                    cur.execute(f"INSERT INTO sidebar_menus ({columns}) VALUES ({marks})", tuple(original.values()))
                conn.commit()

        self.addCleanup(restore)
        self.assertTrue(migrate_up())
        menu = migration_status()["menu"]
        self.assertIsNotNone(menu)
        self.assertNotEqual(menu["id"], 37)
        self.assertEqual(menu["parent_id"], 2)
        self.assertEqual(menu["url"], "/customer-payment-risk")


class TestExistingReportsStillWork(unittest.TestCase):
    def test_sales_report_and_dormant_clients_endpoints(self):
        client = _client()
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main._report_user", return_value=ADMIN_USER):
            sales = client.get(f"/api/reports/sales?customer_id=ZCPR02&start_date={_d(-90)}&end_date={_d(0)}")
            dormant = client.get("/api/dormant-clients?q=ZCPR")
            old_stock_page = client.get("/old-stock-buyers")
        self.assertEqual(sales.status_code, 200, sales.text[:200])
        self.assertEqual(sales.json()["summary"]["invoice_lines"], 3)
        self.assertEqual(sales.json()["summary"]["revenue"], 800.0)
        self.assertEqual(dormant.status_code, 200, dormant.text[:200])
        self.assertEqual(old_stock_page.status_code, 200)


if __name__ == "__main__":
    unittest.main()
