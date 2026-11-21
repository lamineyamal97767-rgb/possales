"""Tests for Report 09: Stock-out Sales Analysis.

Coverage:
- Period defaults, boundaries, previous-period calculation and validation
- Stock classification, missing/unknown stock, negative stock and sales changes
- Snapshot history coverage, detail dates and snapshot retry/no-backdate rules
- Search/category/status/review filters, sorting, summary and pagination
- Synthetic MySQL integration for serial/non-serial stock and duplicate headers
- Permission checks, page rendering, API errors, detail and safe Excel export
- Menu/migration preservation and existing report/alert regressions
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from main import app
from db.connection import get_connection
from db.stock_out_analysis import (
    build_daily_snapshot_rows,
    build_stock_out_rows,
    can_view_stock_out_analysis,
    capture_daily_stock_snapshot,
    classify_stock,
    compute_change_percent,
    get_stock_out_product_detail,
    get_stock_out_report,
    get_stock_out_report_export,
    normalize_stock_out_filters,
)


ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
VIEWER_USER = {"id": 5, "username": "viewer", "role": "viewer", "allowed_menus": "stock-out-analysis"}
NO_PERM_USER = {"id": 6, "username": "noperm", "role": "viewer", "allowed_menus": "1,2,3"}

CATEGORY_ID = "ZCATSO0001"
P1 = "ZSO0000001"
P2 = "ZSO0000002"
P3 = "ZSO0000003"
P4 = "ZSO0000004"
P5 = "ZSO0000005"
P6 = "ZSO0000006"
P7 = "ZSO0000007"

START = "2026-03-01"
END = "2026-03-05"


def _raw_row(**overrides):
    row = {
        "product_id": "P0001",
        "product_name": "Alpha Product",
        "category_id": "C001",
        "category_name": "Test Category",
        "serial": "N",
        "non_serial_qty": 1.0,
        "non_serial_rows": 1,
        "non_serial_observed_at": datetime(2026, 3, 1, 10, 0, 0),
        "serial_qty": None,
        "serial_rows": 0,
        "serial_observed_at": None,
        "selected_quantity": 5.0,
        "previous_quantity": 10.0,
        "selected_revenue": 100.0,
        "previous_revenue": 200.0,
        "snapshot_count": 0,
        "total_snapshot_count": 0,
        "first_snapshot_date": None,
        "last_snapshot_date": None,
    }
    row.update(overrides)
    return row


def _report_payload():
    return {
        "success": True,
        "filters": {
            "start_date": START,
            "end_date": END,
            "previous_start_date": "2026-02-24",
            "previous_end_date": "2026-02-28",
            "search": "",
            "category_id": "",
            "stock_status": "all",
            "review_status": "all",
            "sort_by": "restock_priority",
            "sort_order": "desc",
        },
        "period": {
            "start_date": START,
            "end_date": END,
            "period_days": 5,
            "previous_start_date": "2026-02-24",
            "previous_end_date": "2026-02-28",
        },
        "summary": {
            "total_products": 1,
            "out_of_stock_now": 1,
            "negative_stock": 0,
            "sales_decreased": 1,
            "needs_restock_review": 1,
            "history_unavailable": 1,
        },
        "options": {"categories": [{"category_id": "C001", "category_name": "Test Category"}]},
        "rows": [{
            "product_id": "P0001",
            "product_name": "Test Product",
            "category_id": "C001",
            "category_name": "Test Category",
            "current_stock": 0.0,
            "stock_status": "out_of_stock",
            "stock_status_label": "Out of stock",
            "stock_observed_at": "2026-03-01 10:00:00",
            "history_status": "unavailable",
            "history_status_label": "Unavailable",
            "review_label": "Needs restock review",
            "selected_quantity": 1.0,
            "previous_quantity": 2.0,
            "change_percent": -50.0,
        }],
        "pagination": {
            "page": 1,
            "page_size": 1,
            "total_records": 1,
            "total_pages": 1,
            "has_next": False,
            "has_prev": False,
        },
        "calculation_basis": "test basis",
    }


class TestStockOutFilters(unittest.TestCase):
    def test_default_period_is_latest_30_days(self):
        with patch("db.stock_out_analysis._today", return_value=date(2026, 11, 19)):
            filters = normalize_stock_out_filters({})
        self.assertEqual(filters["end_date"], date(2026, 11, 19))
        self.assertEqual(filters["start_date"], date(2026, 10, 21))
        self.assertEqual(filters["period_days"], 30)
        self.assertEqual(filters["previous_start_date"], date(2026, 9, 21))
        self.assertEqual(filters["previous_end_date"], date(2026, 10, 20))

    def test_end_date_defaults_start_to_previous_30_days(self):
        filters = normalize_stock_out_filters({"end_date": "2026-03-10"})
        self.assertEqual(filters["start_date"], date(2026, 2, 9))
        self.assertEqual(filters["period_days"], 30)
        self.assertEqual(filters["previous_end_date"], date(2026, 2, 8))

    def test_custom_period_and_previous_window(self):
        filters = normalize_stock_out_filters({"start_date": START, "end_date": END})
        self.assertEqual(filters["period_days"], 5)
        self.assertEqual(filters["previous_start_date"], date(2026, 2, 24))
        self.assertEqual(filters["previous_end_date"], date(2026, 2, 28))

    def test_maximum_366_day_range_is_allowed(self):
        filters = normalize_stock_out_filters({"start_date": "2025-01-01", "end_date": "2026-01-01"})
        self.assertEqual(filters["period_days"], 366)

    def test_date_validation(self):
        with self.assertRaises(ValueError):
            normalize_stock_out_filters({"start_date": "not-a-date"})
        with self.assertRaises(ValueError):
            normalize_stock_out_filters({"start_date": END, "end_date": START})
        with self.assertRaises(ValueError):
            normalize_stock_out_filters({"start_date": "2025-01-01", "end_date": "2026-01-02"})

    def test_filter_validation_and_normalization(self):
        filters = normalize_stock_out_filters({
            "start_date": START,
            "end_date": END,
            "search": "  toner  ",
            "category_id": "  C001 ",
            "stock_status": " OUT_OF_STOCK ",
            "review_status": " Needs_Restock ",
            "sort_by": " PRODUCT_NAME ",
            "sort_order": " ASC ",
        })
        self.assertEqual(filters["search"], "toner")
        self.assertEqual(filters["category_id"], "C001")
        self.assertEqual(filters["stock_status"], "out_of_stock")
        self.assertEqual(filters["review_status"], "needs_restock")
        self.assertEqual(filters["sort_by"], "product_name")
        self.assertEqual(filters["sort_order"], "asc")

        for key, value in (
            ("stock_status", "invalid"),
            ("review_status", "invalid"),
            ("sort_by", "invalid"),
            ("sort_order", "up"),
        ):
            with self.assertRaises(ValueError):
                normalize_stock_out_filters({"start_date": START, "end_date": END, key: value})


class TestStockOutCalculations(unittest.TestCase):
    def test_change_percent(self):
        self.assertEqual(compute_change_percent(3, 10), -70.0)
        self.assertEqual(compute_change_percent(10, 4), 150.0)
        self.assertIsNone(compute_change_percent(10, 0))
        self.assertIsNone(compute_change_percent(10, -5))

    def test_classify_stock(self):
        self.assertEqual(classify_stock(None), "unknown")
        self.assertEqual(classify_stock(-0.01), "negative_stock")
        self.assertEqual(classify_stock(0), "out_of_stock")
        self.assertEqual(classify_stock(0.01), "in_stock")

    def test_serial_product_uses_serial_stock_only(self):
        row = build_stock_out_rows([_raw_row(
            serial="Y",
            serial_rows=1,
            serial_qty=-2.5,
            serial_observed_at=datetime(2026, 3, 1, 11, 0, 0),
            non_serial_rows=1,
            non_serial_qty=999.0,
        )])[0]
        self.assertEqual(row["stock_source"], "serial")
        self.assertEqual(row["current_stock"], -2.5)
        self.assertEqual(row["stock_status"], "negative_stock")
        self.assertTrue(row["out_of_stock_now"])
        self.assertTrue(row["negative_stock"])
        self.assertTrue(row["needs_restock_review"])

    def test_non_serial_product_ignores_serial_stock(self):
        row = build_stock_out_rows([_raw_row(
            serial="N",
            non_serial_rows=1,
            non_serial_qty=0.0,
            serial_rows=1,
            serial_qty=999.0,
        )])[0]
        self.assertEqual(row["stock_source"], "non_serial")
        self.assertEqual(row["current_stock"], 0.0)
        self.assertEqual(row["stock_status"], "out_of_stock")
        self.assertTrue(row["out_of_stock_now"])
        self.assertFalse(row["negative_stock"])
        self.assertTrue(row["needs_restock_review"])

    def test_missing_stock_is_unknown_never_zero(self):
        for serial in ("Y", "N"):
            row = build_stock_out_rows([_raw_row(
                serial=serial,
                non_serial_rows=0,
                non_serial_qty=None,
                serial_rows=0,
                serial_qty=None,
            )])[0]
            self.assertIsNone(row["current_stock"])
            self.assertEqual(row["stock_status"], "unknown")
            self.assertFalse(row["out_of_stock_now"])
            self.assertTrue(any("Unknown" in reason for reason in row["review_reasons"]))

    def test_previous_zero_decrease_is_na_percent_but_raw_decrease(self):
        row = build_stock_out_rows([_raw_row(
            non_serial_qty=5.0,
            selected_quantity=-2.0,
            previous_quantity=0.0,
        )])[0]
        self.assertIsNone(row["change_percent"])
        self.assertTrue(row["sales_decreased"])
        self.assertEqual(row["review_label"], "Sales decreased")
        self.assertTrue(any("N/A" in reason for reason in row["review_reasons"]))

    def test_history_status_is_period_specific(self):
        unavailable_none = build_stock_out_rows([_raw_row()], period_days=5)[0]
        self.assertEqual(unavailable_none["history_status"], "unavailable")
        self.assertIn("No daily stock snapshots", unavailable_none["history_note"])

        unavailable_period = build_stock_out_rows([_raw_row(
            snapshot_count=0,
            total_snapshot_count=4,
        )], period_days=5)[0]
        self.assertEqual(unavailable_period["history_status"], "unavailable")
        self.assertIn("inside the selected period", unavailable_period["history_note"])

        partial = build_stock_out_rows([_raw_row(
            snapshot_count=2,
            total_snapshot_count=3,
        )], period_days=5)[0]
        self.assertEqual(partial["history_status"], "partial")
        self.assertTrue(partial["history_available"])
        self.assertIn("2 of 5", partial["history_note"])

        complete = build_stock_out_rows([_raw_row(
            snapshot_count=5,
            total_snapshot_count=8,
        )], period_days=5)[0]
        self.assertEqual(complete["history_status"], "complete")
        self.assertEqual(complete["history_status_label"], "Complete (5/5 days)")

    def test_current_stock_is_rounded(self):
        row = build_stock_out_rows([_raw_row(non_serial_qty=1.234567)])[0]
        self.assertEqual(row["current_stock"], 1.2346)


class TestStockOutFilteringSorting(unittest.TestCase):
    def setUp(self):
        self.rows = build_stock_out_rows([
            _raw_row(
                product_id="A1", product_name="Alpha", category_id="INK", category_name="Ink",
                non_serial_qty=-5.0, selected_quantity=1.0, previous_quantity=10.0,
                snapshot_count=1, total_snapshot_count=1,
            ),
            _raw_row(
                product_id="B2", product_name="Beta", category_id="PAPER", category_name="Paper",
                non_serial_qty=0.0, selected_quantity=0.0, previous_quantity=5.0,
            ),
            _raw_row(
                product_id="C3", product_name="Charles", category_id="INK", category_name="Ink",
                non_serial_qty=10.0, selected_quantity=10.0, previous_quantity=4.0,
                snapshot_count=2, total_snapshot_count=2,
            ),
            _raw_row(
                product_id="D4", product_name="Delta", category_id="PAPER", category_name="Paper",
                non_serial_qty=None, non_serial_rows=0,
                selected_quantity=3.0, previous_quantity=3.0,
            ),
        ], period_days=5)

    def _filter(self, **filters):
        from db.stock_out_analysis import _filter_stock_out_rows
        base = {
            "search": "", "category_id": "", "stock_status": "all",
            "review_status": "all",
        }
        base.update(filters)
        return _filter_stock_out_rows(self.rows, base)

    def test_search_case_insensitive_across_fields(self):
        self.assertEqual([r["product_id"] for r in self._filter(search="alpha")], ["A1"])
        self.assertEqual([r["product_id"] for r in self._filter(search="b2")], ["B2"])
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(search="ink")),
            ["A1", "C3"],
        )

    def test_category_and_stock_filters(self):
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(category_id="INK")),
            ["A1", "C3"],
        )
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(stock_status="out_of_stock")),
            ["A1", "B2"],
        )
        self.assertEqual([r["product_id"] for r in self._filter(stock_status="negative_stock")], ["A1"])
        self.assertEqual([r["product_id"] for r in self._filter(stock_status="unknown")], ["D4"])
        self.assertEqual([r["product_id"] for r in self._filter(stock_status="in_stock")], ["C3"])

    def test_review_filters(self):
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(review_status="needs_restock")),
            ["A1", "B2"],
        )
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(review_status="sales_decreased")),
            ["A1", "B2"],
        )
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(review_status="out_of_stock")),
            ["A1", "B2"],
        )
        self.assertEqual(
            sorted(r["product_id"] for r in self._filter(review_status="history_unavailable")),
            ["B2", "D4"],
        )

    def test_restock_priority_sorts_most_severe_decline_first(self):
        rows = build_stock_out_rows([
            _raw_row(product_id="N1", product_name="Needs Small", non_serial_qty=0.0,
                     selected_quantity=9.0, previous_quantity=10.0),
            _raw_row(product_id="N2", product_name="Needs Big", non_serial_qty=0.0,
                     selected_quantity=1.0, previous_quantity=10.0),
            _raw_row(product_id="O1", product_name="Out No Drop", non_serial_qty=0.0,
                     selected_quantity=10.0, previous_quantity=10.0),
            _raw_row(product_id="S1", product_name="Drop In Stock", non_serial_qty=5.0,
                     selected_quantity=5.0, previous_quantity=10.0),
            _raw_row(product_id="X1", product_name="No Flag", non_serial_qty=5.0,
                     selected_quantity=10.0, previous_quantity=10.0),
        ])
        from db.stock_out_analysis import _sort_stock_out_rows
        result = _sort_stock_out_rows(rows, "restock_priority", "desc")
        self.assertEqual(
            [row["product_id"] for row in result],
            ["N2", "N1", "O1", "S1", "X1"],
        )

    def test_sort_ties_are_ascending_by_name_in_descending_mode(self):
        rows = build_stock_out_rows([
            _raw_row(product_id="Z1", product_name="Zeta", non_serial_qty=0.0,
                     selected_quantity=1.0, previous_quantity=10.0),
            _raw_row(product_id="A1", product_name="Alpha", non_serial_qty=0.0,
                     selected_quantity=1.0, previous_quantity=10.0),
        ])
        from db.stock_out_analysis import _sort_stock_out_rows
        result = _sort_stock_out_rows(rows, "restock_priority", "desc")
        self.assertEqual([row["product_id"] for row in result], ["A1", "Z1"])

    def test_numeric_sort_keeps_unknown_values_last(self):
        from db.stock_out_analysis import _sort_stock_out_rows
        desc = _sort_stock_out_rows(self.rows, "current_stock", "desc")
        self.assertEqual([r["product_id"] for r in desc], ["C3", "B2", "A1", "D4"])
        asc = _sort_stock_out_rows(self.rows, "current_stock", "asc")
        self.assertEqual([r["product_id"] for r in asc], ["A1", "B2", "C3", "D4"])

    def test_summary_counts_filtered_rows(self):
        from db.stock_out_analysis import _summarize_stock_out_rows
        summary = _summarize_stock_out_rows(self.rows)
        self.assertEqual(summary["total_products"], 4)
        self.assertEqual(summary["out_of_stock_now"], 2)
        self.assertEqual(summary["negative_stock"], 1)
        self.assertEqual(summary["sales_decreased"], 2)
        self.assertEqual(summary["needs_restock_review"], 2)
        self.assertEqual(summary["history_unavailable"], 2)

    def test_pagination(self):
        from db.stock_out_analysis import _paginate
        page_two = _paginate(self.rows, page=2, page_size=3)
        self.assertEqual(len(page_two["rows"]), 1)
        self.assertEqual(page_two["pagination"]["total_records"], 4)
        self.assertEqual(page_two["pagination"]["total_pages"], 2)
        self.assertTrue(page_two["pagination"]["has_prev"])
        self.assertFalse(page_two["pagination"]["has_next"])


class TestStockOutSnapshotPure(unittest.TestCase):
    def test_build_daily_snapshot_rows(self):
        rows = build_daily_snapshot_rows([
            {"product_id": P1, "serial": "Y", "serial_rows": 1, "serial_qty": -3.0,
             "non_serial_rows": 1, "non_serial_qty": 999.0},
            {"product_id": P2, "serial": "N", "non_serial_rows": 1, "non_serial_qty": 0.0,
             "serial_rows": 0, "serial_qty": None},
            {"product_id": P3, "serial": "N", "non_serial_rows": 0, "non_serial_qty": None,
             "serial_rows": 0, "serial_qty": None},
        ], observation_date=date(2026, 4, 1), observed_at=datetime(2026, 4, 1, 23, 55, 0))
        by_id = {row["product_id"]: row for row in rows}
        self.assertEqual(by_id[P1]["stock_source"], "serial")
        self.assertEqual(by_id[P1]["stock_qty"], -3.0)
        self.assertEqual(by_id[P1]["stock_status"], "negative_stock")
        self.assertEqual(by_id[P2]["stock_qty"], 0.0)
        self.assertEqual(by_id[P2]["stock_status"], "out_of_stock")
        self.assertIsNone(by_id[P3]["stock_qty"])
        self.assertEqual(by_id[P3]["stock_status"], "unknown")
        self.assertEqual(by_id[P3]["stock_source"], "non_serial")

    @patch("db.stock_out_analysis._today", return_value=date(2026, 4, 1))
    def test_backdated_snapshot_is_rejected_without_database(self, _mock_today):
        with self.assertRaises(ValueError):
            capture_daily_stock_snapshot(observation_date=date(2026, 3, 31))


class TestStockOutDatabaseIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection()
        try:
            self._seed()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _seed(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO category (CATEGORY_ID, CATEGORY_NAME, INDATE) VALUES (%s, %s, NOW())",
                (CATEGORY_ID, "ZS Test Category"),
            )
            cur.executemany("""
                INSERT INTO product (PRODUCT_ID, PRODUCT_NAME, CATEGORY_ID, SERIAL, ACTIVE)
                VALUES (%s, %s, %s, %s, 'Y')
            """, [
                (P1, "ZS Test Serial Stockout", CATEGORY_ID, "Y"),
                (P2, "ZS Test Zero Stock", CATEGORY_ID, "N"),
                (P3, "ZS Test Positive Increase", CATEGORY_ID, "N"),
                (P4, "ZS Test Unknown Stock", CATEGORY_ID, "N"),
                (P5, "ZS Test Negative Stock", CATEGORY_ID, "N"),
                (P6, "ZS Test Return Only", CATEGORY_ID, "N"),
                (P7, "ZS Test Stock Only", CATEGORY_ID, "N"),
            ])
            cur.executemany("""
                INSERT INTO stock_non_serial
                    (PURCHASE_DATE, PRODUCT_ID, PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, %s)
            """, [
                ("2026-03-01", P1, 999.0, 999.0, datetime(2026, 3, 1, 9, 0, 0)),
                ("2026-03-01", P2, 0.0, 0.0, datetime(2026, 3, 1, 9, 0, 0)),
                ("2026-03-01", P3, 20.0, 20.0, datetime(2026, 3, 1, 9, 0, 0)),
                ("2026-03-01", P5, -5.0, -5.0, datetime(2026, 3, 1, 9, 0, 0)),
                ("2026-03-01", P6, 1.0, 1.0, datetime(2026, 3, 1, 9, 0, 0)),
                ("2026-03-01", P7, 7.0, 7.0, datetime(2026, 3, 1, 9, 0, 0)),
            ])
            cur.executemany("""
                INSERT INTO stock_serial
                    (PURCHASE_DATE, PRODUCT_ID, ORG_COST, AVG_COST,
                     PURCHASE_QTY, STOCK_QTY, TIMESTAMP)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, [
                ("2026-03-01", P1, 1.0, 1.0, -2.0, -2.0, datetime(2026, 3, 1, 10, 0, 0)),
                ("2026-03-01", P3, 1.0, 1.0, 999.0, 999.0, datetime(2026, 3, 1, 10, 0, 0)),
            ])
            cur.executemany("""
                INSERT INTO sales (SALES_ID, SALES_DATE, TOTAL_PRICE)
                VALUES (%s, %s, %s)
            """, [
                ("ZSOINV00000001", "2026-03-02 10:00:00", 100.0),
                ("ZSOINV00000001", "2026-03-02 10:00:00", 100.0),
                ("ZSOINV00000002", "2026-02-25 10:00:00", 500.0),
                ("ZSOINV00000003", "2026-02-26 10:00:00", 250.0),
                ("ZSOINV00000004", "2026-03-03 10:00:00", 500.0),
                ("ZSOINV00000005", "2026-02-27 10:00:00", 200.0),
                ("ZSOINV00000006", "2026-03-04 10:00:00", 150.0),
                ("ZSOINV00000007", "2026-02-28 10:00:00", 150.0),
                ("ZSOINV00000008", "2026-03-05 10:00:00", 50.0),
                ("ZSOINV00000009", "2026-02-28 10:00:00", 100.0),
                ("ZSOINV00000010", "2026-03-05 10:00:00", -10.0),
            ])
            cur.executemany("""
                INSERT INTO sales_details
                    (SALES_ID, PRODUCT_ID, SALES_DATE, UNIT_PRICE, QTY)
                VALUES (%s, %s, %s, %s, %s)
            """, [
                ("ZSOINV00000001", P1, "2026-03-02", 50.0, 2.0),
                ("ZSOINV00000002", P1, "2026-02-25", 50.0, 10.0),
                ("ZSOINV00000003", P2, "2026-02-26", 50.0, 5.0),
                ("ZSOINV00000004", P3, "2026-03-03", 50.0, 10.0),
                ("ZSOINV00000005", P3, "2026-02-27", 50.0, 4.0),
                ("ZSOINV00000006", P4, "2026-03-04", 50.0, 3.0),
                ("ZSOINV00000007", P4, "2026-02-28", 50.0, 3.0),
                ("ZSOINV00000008", P5, "2026-03-05", 50.0, 1.0),
                ("ZSOINV00000009", P5, "2026-02-28", 50.0, 2.0),
                ("ZSOINV00000010", P6, "2026-03-05", 10.0, -1.0),
            ])
            cur.executemany("""
                INSERT INTO stock_out_daily_snapshots
                    (product_id, observation_date, stock_qty, stock_status,
                     stock_source, observed_at, coverage_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, [
                (P1, "2026-03-01", -2.0, "negative_stock", "serial",
                 datetime(2026, 3, 1, 23, 55, 0), "Synthetic snapshot"),
                (P1, "2026-03-02", -2.0, "negative_stock", "serial",
                 datetime(2026, 3, 2, 23, 55, 0), "Synthetic snapshot"),
                (P1, "2026-03-03", -2.0, "negative_stock", "serial",
                 datetime(2026, 3, 3, 23, 55, 0), "Synthetic snapshot"),
                (P4, "2026-02-01", 5.0, "in_stock", "non_serial",
                 datetime(2026, 2, 1, 23, 55, 0), "Outside selected period"),
            ])

    def _filters(self, **overrides):
        filters = {"start_date": START, "end_date": END, "search": "ZS Test"}
        filters.update(overrides)
        return filters

    def _report(self, **overrides):
        return get_stock_out_report(
            self._filters(**overrides), page=1, page_size=100, conn=self.conn
        )

    def _rows_by_id(self, report):
        return {row["product_id"]: row for row in report["rows"]}

    def test_report_uses_sources_deduplicates_headers_and_calculates_flags(self):
        report = self._report()
        rows = self._rows_by_id(report)

        self.assertEqual(set(rows), {P1, P2, P3, P4, P5, P6, P7})
        self.assertEqual(rows[P1]["stock_source"], "serial")
        self.assertEqual(rows[P1]["current_stock"], -2.0)
        self.assertEqual(rows[P1]["selected_quantity"], 2.0)
        self.assertEqual(rows[P1]["previous_quantity"], 10.0)
        self.assertEqual(rows[P1]["change_percent"], -80.0)
        self.assertTrue(rows[P1]["needs_restock_review"])

        self.assertEqual(rows[P2]["current_stock"], 0.0)
        self.assertEqual(rows[P2]["stock_status"], "out_of_stock")
        self.assertEqual(rows[P2]["change_percent"], -100.0)
        self.assertTrue(rows[P2]["needs_restock_review"])

        self.assertEqual(rows[P3]["current_stock"], 20.0)
        self.assertEqual(rows[P3]["selected_quantity"], 10.0)
        self.assertEqual(rows[P3]["previous_quantity"], 4.0)
        self.assertFalse(rows[P3]["sales_decreased"])

        self.assertIsNone(rows[P4]["current_stock"])
        self.assertEqual(rows[P4]["stock_status"], "unknown")
        self.assertFalse(rows[P4]["needs_restock_review"])
        self.assertEqual(rows[P4]["snapshot_count"], 0)
        self.assertEqual(rows[P4]["total_snapshot_count"], 1)

        self.assertEqual(rows[P5]["current_stock"], -5.0)
        self.assertEqual(rows[P5]["stock_status"], "negative_stock")

        self.assertEqual(rows[P6]["selected_quantity"], -1.0)
        self.assertEqual(rows[P6]["previous_quantity"], 0.0)
        self.assertIsNone(rows[P6]["change_percent"])
        self.assertTrue(rows[P6]["sales_decreased"])

        self.assertEqual(rows[P7]["current_stock"], 7.0)
        self.assertEqual(rows[P7]["selected_quantity"], 0.0)
        self.assertEqual(rows[P7]["previous_quantity"], 0.0)

        self.assertEqual(rows[P1]["history_status"], "partial")
        self.assertEqual(rows[P1]["snapshot_count"], 3)
        self.assertEqual(rows[P1]["total_snapshot_count"], 3)
        self.assertEqual(rows[P4]["history_status"], "unavailable")

        self.assertEqual(report["summary"], {
            "total_products": 7,
            "out_of_stock_now": 3,
            "negative_stock": 2,
            "sales_decreased": 4,
            "needs_restock_review": 3,
            "history_unavailable": 6,
        })

    def test_report_filters_and_pagination(self):
        out_of_stock = self._report(stock_status="out_of_stock")
        self.assertEqual(
            sorted(row["product_id"] for row in out_of_stock["rows"]),
            [P1, P2, P5],
        )
        self.assertEqual(out_of_stock["summary"]["total_products"], 3)

        needs_restock = self._report(review_status="needs_restock")
        self.assertEqual(
            sorted(row["product_id"] for row in needs_restock["rows"]),
            [P1, P2, P5],
        )

        positive = self._report(search="Positive")
        self.assertEqual([row["product_id"] for row in positive["rows"]], [P3])

        category = self._report(category_id=CATEGORY_ID)
        self.assertEqual(category["summary"]["total_products"], 7)

        paged = get_stock_out_report(
            self._filters(), page=2, page_size=3, conn=self.conn
        )
        self.assertEqual(len(paged["rows"]), 3)
        self.assertEqual(paged["pagination"]["total_records"], 7)
        self.assertTrue(paged["pagination"]["has_prev"])
        self.assertTrue(paged["pagination"]["has_next"])

    def test_export_returns_all_filtered_rows(self):
        export = get_stock_out_report_export(self._filters(), conn=self.conn)
        self.assertEqual(len(export["rows"]), 7)
        self.assertEqual(export["pagination"]["total_records"], 7)
        self.assertEqual(export["pagination"]["page_size"], 7)

    def test_detail_fills_missing_days_and_shows_period_history(self):
        detail = get_stock_out_product_detail(
            P1, self._filters(), conn=self.conn
        )
        self.assertEqual(detail["history"]["snapshot_count"], 3)
        self.assertEqual(detail["history"]["total_snapshots"], 3)
        self.assertEqual(detail["history"]["period_days"], 5)
        self.assertEqual(detail["history"]["missing_days"], 2)
        self.assertEqual(len(detail["history"]["days"]), 5)
        self.assertEqual(len(detail["sales"]["trend"]), 5)
        days = {day["date"]: day for day in detail["history"]["days"]}
        self.assertEqual(days["2026-03-01"]["stock_qty"], -2.0)
        self.assertTrue(days["2026-03-04"]["missing"])
        self.assertIsNone(days["2026-03-04"]["stock_qty"])
        self.assertEqual(detail["sales"]["selected_quantity"], 2.0)
        self.assertEqual(detail["sales"]["previous_quantity"], 10.0)

        outside = get_stock_out_product_detail(
            P4, self._filters(), conn=self.conn
        )
        self.assertEqual(outside["history"]["snapshot_count"], 0)
        self.assertEqual(outside["history"]["total_snapshots"], 1)
        self.assertTrue(all(day["missing"] for day in outside["history"]["days"]))

    def test_detail_not_found_and_validation(self):
        self.assertIsNone(get_stock_out_product_detail(
            "ZNONEXIST", self._filters(), conn=self.conn
        ))
        with self.assertRaises(ValueError):
            get_stock_out_product_detail("", self._filters(), conn=self.conn)


class TestStockOutSnapshotDatabase(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    @patch("db.stock_out_analysis._today", return_value=date(2026, 4, 1))
    @patch("db.stock_out_analysis._fetch_current_stock_for_snapshot")
    def test_capture_is_retry_safe_and_preserves_first_observation(
        self, mock_fetch, _mock_today
    ):
        mock_fetch.return_value = [
            {"product_id": "ZSOSNAP001", "serial": "N", "non_serial_rows": 1,
             "non_serial_qty": 0.0, "serial_rows": 0, "serial_qty": None},
            {"product_id": "ZSOSNAP002", "serial": "Y", "serial_rows": 1,
             "serial_qty": -3.0, "non_serial_rows": 0, "non_serial_qty": None},
            {"product_id": "ZSOSNAP003", "serial": "N", "non_serial_rows": 0,
             "non_serial_qty": None, "serial_rows": 0, "serial_qty": None},
        ]

        first = capture_daily_stock_snapshot(conn=self.conn)
        self.assertEqual(first["products_processed"], 3)
        self.assertEqual(first["known_stock"], 2)
        self.assertEqual(first["unknown_stock"], 1)
        self.assertEqual(first["new_snapshots"], 3)

        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT product_id, stock_qty, stock_status, stock_source
                FROM stock_out_daily_snapshots
                WHERE observation_date = %s
                ORDER BY product_id
            """, (date(2026, 4, 1),))
            saved = cur.fetchall()
        self.assertEqual(len(saved), 3)
        self.assertEqual(saved[0]["stock_qty"], 0.0)
        self.assertEqual(saved[0]["stock_status"], "out_of_stock")
        self.assertEqual(saved[1]["stock_qty"], -3.0)
        self.assertEqual(saved[1]["stock_source"], "serial")
        self.assertIsNone(saved[2]["stock_qty"])
        self.assertEqual(saved[2]["stock_status"], "unknown")

        mock_fetch.return_value = [
            {"product_id": "ZSOSNAP001", "serial": "N", "non_serial_rows": 1,
             "non_serial_qty": 99.0, "serial_rows": 0, "serial_qty": None},
            {"product_id": "ZSOSNAP002", "serial": "Y", "serial_rows": 1,
             "serial_qty": 88.0, "non_serial_rows": 0, "non_serial_qty": None},
            {"product_id": "ZSOSNAP003", "serial": "N", "non_serial_rows": 1,
             "non_serial_qty": 77.0, "serial_rows": 0, "serial_qty": None},
        ]
        second = capture_daily_stock_snapshot(conn=self.conn)
        self.assertEqual(second["new_snapshots"], 0)
        self.assertEqual(second["snapshots_before"], 3)
        self.assertEqual(second["snapshots_after"], 3)

        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT product_id, stock_qty
                FROM stock_out_daily_snapshots
                WHERE observation_date = %s
                ORDER BY product_id
            """, (date(2026, 4, 1),))
            unchanged = cur.fetchall()
        self.assertEqual([row["stock_qty"] for row in unchanged], [0.0, -3.0, None])

        dry_run = capture_daily_stock_snapshot(conn=self.conn, dry_run=True)
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["new_snapshots"], 0)


class TestStockOutPermissions(unittest.TestCase):
    def test_admin_and_wildcard(self):
        self.assertTrue(can_view_stock_out_analysis(ADMIN_USER))
        self.assertTrue(can_view_stock_out_analysis({"role": "user", "allowed_menus": "*"}))

    def test_menu_id_grants(self):
        with patch("db.stock_out_analysis.resolve_stock_out_analysis_menu_id", return_value=33):
            self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": [33]}))
            self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": ["33"]}))
            self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": "14,33"}))
            self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": json.dumps([33])}))
            self.assertFalse(can_view_stock_out_analysis({"role": "User", "allowed_menus": "14,15"}))

    def test_slug_grants(self):
        self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": ["stock-out-analysis"]}))
        self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": ["/menus/stock-out-analysis"]}))
        self.assertTrue(can_view_stock_out_analysis({"role": "User", "allowed_menus": ["stock_out_analysis"]}))

    def test_no_access(self):
        self.assertFalse(can_view_stock_out_analysis(None))
        self.assertFalse(can_view_stock_out_analysis(NO_PERM_USER))
        self.assertFalse(can_view_stock_out_analysis({"role": "User", "allowed_menus": None}))
        self.assertFalse(can_view_stock_out_analysis({"role": "User", "allowed_menus": "["}))


class TestStockOutEndpoints(unittest.TestCase):
    client = TestClient(app, raise_server_exceptions=False)

    def test_unauthenticated_access(self):
        with patch("main.is_authenticated", return_value=False):
            page = self.client.get("/menus/stock-out-analysis", follow_redirects=False)
            report = self.client.get("/api/stock-out-analysis/report")
            detail = self.client.get("/api/stock-out-analysis/products/PD1")
            export = self.client.get("/api/stock-out-analysis/report.xlsx")
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers.get("location", ""))
        self.assertEqual(report.status_code, 401)
        self.assertEqual(detail.status_code, 401)
        self.assertEqual(export.status_code, 401)

    def test_permission_denied(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=NO_PERM_USER):
            page = self.client.get("/menus/stock-out-analysis")
            report = self.client.get("/api/stock-out-analysis/report")
            detail = self.client.get("/api/stock-out-analysis/products/PD1")
            export = self.client.get("/api/stock-out-analysis/report.xlsx")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(report.status_code, 403)
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(export.status_code, 403)

    def test_page_renders_for_permitted_viewer(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=VIEWER_USER):
            response = self.client.get("/menus/stock-out-analysis")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Stock-out Sales Analysis", response.text)
        self.assertIn("Needs Restock Review", response.text)
        self.assertIn("History Unavailable", response.text)
        self.assertIn("Unknown stock is not zero", response.text)

    def test_report_passes_filters_and_pagination(self):
        mock_report = unittest.mock.MagicMock(return_value=_report_payload())
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report", mock_report):
            response = self.client.get(
                "/api/stock-out-analysis/report"
                f"?start_date={START}&end_date={END}&search=toner"
                "&category_id=C001&stock_status=out_of_stock"
                "&review_status=needs_restock&sort_by=product_name"
                "&sort_order=asc&page=2&page_size=5"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["rows"][0]["product_id"], "P0001")
        filters = mock_report.call_args.args[0]
        self.assertEqual(filters["start_date"], START)
        self.assertEqual(filters["end_date"], END)
        self.assertEqual(filters["search"], "toner")
        self.assertEqual(filters["category_id"], "C001")
        self.assertEqual(filters["stock_status"], "out_of_stock")
        self.assertEqual(filters["review_status"], "needs_restock")
        self.assertEqual(mock_report.call_args.kwargs, {"page": 2, "page_size": 5})

    def test_report_validation_and_database_errors(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report", side_effect=ValueError("start_date invalid")):
            invalid = self.client.get("/api/stock-out-analysis/report")
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("start_date invalid", invalid.json()["message"])

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report", side_effect=RuntimeError("secret db detail")):
            failure = self.client.get("/api/stock-out-analysis/report")
        self.assertEqual(failure.status_code, 500)
        self.assertNotIn("secret db detail", failure.json()["message"])

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER):
            too_large = self.client.get("/api/stock-out-analysis/report?page_size=201")
        self.assertEqual(too_large.status_code, 422)

    def test_detail_success_not_found_and_errors(self):
        detail_payload = {
            "success": True,
            "product": {"product_id": "P0001", "product_name": "Test Product"},
            "stock": {"current_qty": 0.0},
            "sales": {"trend": []},
            "review": {"reasons": []},
            "history": {"days": []},
        }
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_product_detail", return_value=detail_payload):
            success = self.client.get(f"/api/stock-out-analysis/products/P0001?start_date={START}&end_date={END}")
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()["product"]["product_id"], "P0001")

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_product_detail", return_value=None):
            missing = self.client.get("/api/stock-out-analysis/products/NOPE")
        self.assertEqual(missing.status_code, 404)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_product_detail", side_effect=ValueError("bad date")):
            invalid = self.client.get("/api/stock-out-analysis/products/P1?start_date=nope")
        self.assertEqual(invalid.status_code, 422)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_product_detail", side_effect=RuntimeError("secret detail")):
            failure = self.client.get("/api/stock-out-analysis/products/P1")
        self.assertEqual(failure.status_code, 500)
        self.assertNotIn("secret detail", failure.json()["message"])

    def test_excel_export_is_safe_and_filtered(self):
        export_payload = {
            "success": True,
            "rows": [{
                "product_id": "P0001",
                "product_name": "=SUM(A1)",
                "category_name": "+cmd",
                "current_stock": None,
                "stock_status_label": "Unknown",
                "stock_observed_at": None,
                "selected_quantity": 1.0,
                "previous_quantity": 2.0,
                "change_percent": None,
                "history_status_label": "Unavailable",
                "review_label": "Stock unknown",
            }],
        }
        mock_export = unittest.mock.MagicMock(return_value=export_payload)
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report_export", mock_export):
            response = self.client.get(
                f"/api/stock-out-analysis/report.xlsx?start_date={START}&end_date={END}&search=toner"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("stock-out-analysis.xlsx", response.headers["content-disposition"])

        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(response.content))
        sheet = workbook["Stock-out Analysis"]
        self.assertEqual(sheet.cell(row=1, column=1).value, "Product ID")
        self.assertEqual(sheet.cell(row=2, column=2).value, "=SUM(A1)")
        self.assertEqual(sheet.cell(row=2, column=2).data_type, "s")
        self.assertEqual(sheet.cell(row=2, column=3).data_type, "s")
        self.assertEqual(sheet.cell(row=2, column=4).value, "Unknown")
        self.assertEqual(sheet.cell(row=2, column=9).value, "N/A")
        filters = mock_export.call_args.args[0]
        self.assertEqual(filters["search"], "toner")

    def test_export_validation_and_errors(self):
        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report_export", side_effect=ValueError("bad range")):
            invalid = self.client.get("/api/stock-out-analysis/report.xlsx")
        self.assertEqual(invalid.status_code, 422)

        with patch("main.is_authenticated", return_value=True), \
             patch("main._get_request_user", return_value=ADMIN_USER), \
             patch("main.database.get_stock_out_report_export", side_effect=RuntimeError("secret export")):
            failure = self.client.get("/api/stock-out-analysis/report.xlsx")
        self.assertEqual(failure.status_code, 500)
        self.assertNotIn("secret export", failure.json()["message"])


class TestStockOutMenusAndRegressions(unittest.TestCase):
    def test_migration_status_and_menu_preservation(self):
        from db.migrations.migrate_stock_out_analysis import check_status, migrate_up

        self.assertTrue(migrate_up())
        status = check_status()
        self.assertTrue(status["exists"])
        self.assertIsNotNone(status["menu"])
        self.assertEqual(status["menu"]["id"], 33)
        self.assertEqual(status["menu"]["parent_id"], 1)
        self.assertEqual(status["menu"]["url"], "/menus/stock-out-analysis")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM sidebar_menus WHERE parent_id IS NULL AND title = 'Product Intelligence'")
                self.assertEqual(cur.fetchone()["id"], 1)
                cur.execute("SELECT id, parent_id, url FROM sidebar_menus WHERE title = 'Customer Gifts'")
                gifts = cur.fetchone()
                self.assertEqual(gifts["id"], 32)
                self.assertEqual(gifts["parent_id"], 2)
                self.assertEqual(gifts["url"], "/menus/customer-gifts")

    def test_existing_report_permissions_still_work(self):
        from db import reports

        self.assertTrue(reports.can_view_reports({"role": "Admin", "allowed_menus": []}))
        with patch.object(reports, "_resolve_reports_menu_id", return_value=None):
            self.assertFalse(reports.can_view_reports({"role": "User", "allowed_menus": []}))
        with patch.object(
            reports,
            "_query",
            return_value=[{"sales_date": "2026-10-01", "invoice_lines": 1, "quantity": 2.0, "revenue": 100.0}],
        ):
            report = reports.get_sales_report({"start_date": "2026-10-01", "end_date": "2026-10-31"})
        self.assertIn("summary", report)
        self.assertIn("rows", report)
        self.assertEqual(report["summary"]["revenue"], 100.0)

    def test_existing_stock_alerts_still_run(self):
        from db.alerts import get_smart_alerts

        alerts = get_smart_alerts(low_stock_threshold=0)
        self.assertIsInstance(alerts, list)
        for alert in alerts:
            if alert.get("kind") == "low_stock":
                self.assertLessEqual(alert.get("current_stock", 0), 0)

    def test_database_facade_exports_stock_out_functions(self):
        import database

        for name in (
            "can_view_stock_out_analysis",
            "get_stock_out_report",
            "get_stock_out_report_export",
            "get_stock_out_product_detail",
            "capture_daily_stock_snapshot",
        ):
            self.assertTrue(callable(getattr(database, name)))


if __name__ == "__main__":
    unittest.main()
