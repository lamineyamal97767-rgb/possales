"""Synthetic tests for Report 10: evidence-backed market discovery.

Every provider call in these tests is synthetic. No Tavily credits are spent.
Database-backed tests use unique run IDs and remove only their own rows.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
import market_research_service as mrs
from db import market_research as repo
from db.connection import get_connection

ADMIN_USER = {"id": 1, "username": "admin", "role": "Admin", "allowed_menus": "*"}
TEST_MODEL = "ZXQ9001"
TEST_BRAND = "Epson"
TEST_DOMAIN = "startech.com.bd"
TEST_URL = f"https://www.{TEST_DOMAIN}/epson-zxq9001-printer"
TEST_CONTENT = (
    f"{TEST_BRAND} {TEST_MODEL} is a best seller. "
    "Price BDT 22,500. In stock. Newly launched 12 January 2026."
)

SYNTHETIC_SOURCES = ({
    "name": "Star Tech",
    "type": "retailer",
    "base_url": "https://www.startech.com.bd",
    "search_domain": TEST_DOMAIN,
    "url_contains": TEST_DOMAIN,
},)


def _empty_ok():
    return {
        "ok": True,
        "results": [],
        "answer": None,
        "status_code": 200,
        "error": None,
        "error_code": None,
    }


def _one_result(model=TEST_MODEL, brand=TEST_BRAND, url=TEST_URL, content=TEST_CONTENT):
    return {
        "ok": True,
        "results": [{
            "title": f"{brand} {model} Printer",
            "url": url,
            "content": content,
        }],
        "answer": None,
        "status_code": 200,
        "error": None,
        "error_code": None,
    }


def _search_printer_only(query, max_results=5):
    if "printer model price" in query:
        return _one_result()
    return _empty_ok()


def _cleanup_run(run_id):
    if not run_id:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in (
                "market_research_usage",
                "market_research_sources",
                "market_research_observations",
                "market_research_runs",
            ):
                cur.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,))
        conn.commit()


def _create_run(run_id, sources_total=1, queries_total=2):
    repo.create_run(run_id, 1, "tavily", sources_total, queries_total)


class TestEvidenceParsing(unittest.TestCase):
    def test_demand_requires_explicit_source_evidence(self):
        status, evidence = mrs.detect_demand(
            f"{TEST_BRAND} {TEST_MODEL} Printer\nAvailable now at our store.",
            TEST_MODEL,
            TEST_BRAND,
        )
        self.assertEqual(status, "demand_unknown")
        self.assertIsNone(evidence)

        status, evidence = mrs.detect_demand(
            f"{TEST_BRAND} {TEST_MODEL} Printer\n{TEST_BRAND} {TEST_MODEL} is a best seller.",
            TEST_MODEL,
            TEST_BRAND,
        )
        self.assertEqual(status, "bestseller")
        self.assertIn("best seller", (evidence or "").lower())

    def test_new_release_requires_source_date_and_first_observed_otherwise(self):
        collected = datetime(2026, 1, 20, 10, 0, 0)
        released = mrs.parse_provider_results(
            [{
                "title": f"{TEST_BRAND} {TEST_MODEL} Printer",
                "url": TEST_URL,
                "content": f"{TEST_BRAND} {TEST_MODEL} launched on 12 January 2026.",
            }],
            SYNTHETIC_SOURCES[0],
            collected,
        )
        self.assertEqual(released[0]["release_status"], "new_release")
        self.assertEqual(released[0]["release_date"], date(2026, 1, 12))
        self.assertTrue(released[0]["release_evidence"])

        observed = mrs.parse_provider_results(
            [{
                "title": f"{TEST_BRAND} {TEST_MODEL} Printer",
                "url": TEST_URL,
                "content": f"{TEST_BRAND} {TEST_MODEL} was launched recently.",
                "published_date": "2026-01-12",
            }],
            SYNTHETIC_SOURCES[0],
            collected,
        )
        self.assertEqual(observed[0]["release_status"], "first_observed")
        self.assertIsNone(observed[0]["release_date"])
        self.assertEqual(observed[0]["first_observed_date"], collected.date())
        self.assertEqual(observed[0]["source_date"], date(2026, 1, 12))

    def test_generic_listing_and_off_topic_pages_are_rejected(self):
        collected = datetime(2026, 1, 20, 10, 0, 0)
        results = [
            {
                "title": "Printer Price in Bangladesh 2026 | Star Tech",
                "url": TEST_URL,
                "content": "Xiaomi PRO 9730 printer. Price BDT 11,700.",
            },
            {
                "title": "Toner Cartridge price in Bangladesh | Ryans",
                "url": f"https://www.{TEST_DOMAIN}/toner",
                "content": "Original TN2380 toner. Price BDT 750.",
            },
            {
                "title": "Tecno Spark 40 Pro Price in Bangladesh | Gadget & Gear",
                "url": f"https://www.{TEST_DOMAIN}/phone",
                "content": "Ink printer offers available.",
            },
            {
                "title": "Incase ICON Lite Backpack for Laptop Price in Bangladesh",
                "url": f"https://www.{TEST_DOMAIN}/bag",
                "content": "Price BDT 9,500.",
            },
        ]
        observations = mrs.parse_provider_results(results, SYNTHETIC_SOURCES[0], collected)
        self.assertEqual(observations, [])

    def test_legacy_rows_are_displayable_only_when_product_specific(self):
        generic = {"raw_title": "Printer Price in Bangladesh 2026 | Star Tech", "brand": "Xiaomi"}
        off_topic = {"raw_title": "Tecno Spark 40 Pro Price in Bangladesh", "brand": "Unknown"}
        product = {"raw_title": "HP LaserJet M211dw Printer", "brand": "HP"}
        self.assertFalse(mrs.is_displayable_observation(generic))
        self.assertFalse(mrs.is_displayable_observation(off_topic))
        self.assertTrue(mrs.is_displayable_observation(product))

    def test_title_model_is_required_and_known_brands_ignore_generic_names(self):
        self.assertEqual(
            mrs.detect_brand(
                "HP 105A Black Original Laser Toner Cartridge",
                ["HP", "ORIGINAL", "INK", "SMART"],
            ),
            "HP",
        )
        self.assertEqual(mrs.detect_brand("Original TN2380 Toner", ["ORIGINAL", "INK"]), "Unknown")
        self.assertEqual(mrs.classify_category("Canon PIXMA G1737 Ink Tank Printer"), "Printer")
        self.assertEqual(mrs.classify_category("Printer Ink Cartridge"), "Ink")
        self.assertEqual(mrs.classify_category("Toner Cartridge"), "Toner Cartridge")

    def test_prices_keep_source_currency_and_never_invent_bdt(self):
        self.assertEqual(mrs.parse_price("Price BDT 22,500"), (22500.0, "BDT"))
        self.assertEqual(mrs.parse_price("Only $199.99 today"), (199.99, "USD"))
        self.assertEqual(mrs.parse_price("Price on request"), (None, None))
        self.assertEqual(mrs.parse_price("22,500"), (None, None))

    def test_provider_results_ignore_unselected_domains(self):
        observations = mrs.parse_provider_results(
            [{
                "title": f"{TEST_BRAND} {TEST_MODEL} Printer",
                "url": "https://www.example.com/epson-zxq9001",
                "content": "BDT 22,500",
            }],
            SYNTHETIC_SOURCES[0],
            datetime(2026, 1, 20, 10, 0, 0),
        )
        self.assertEqual(observations, [])

    def test_queries_are_site_scoped_and_contain_no_customer_data(self):
        for source in mrs.BANGLADESH_MARKET_SOURCES:
            for query in mrs.build_queries(source):
                self.assertTrue(query.startswith(f"site:{source['search_domain']}"))
                self.assertNotIn("customer", query.lower())

    def test_credit_limits_are_enforced_as_configured(self):
        self.assertEqual(mrs.MAX_CREDITS_PER_RESEARCH, 50)
        self.assertEqual(mrs.MAX_CREDITS_PER_MONTH, 900)

    def test_provider_health_never_claims_ready_without_response(self):
        with patch("market_research_service.get_api_key", return_value=""):
            health = mrs._provider_health_from_run(None)
        self.assertEqual(health["status"], "not_configured")
        self.assertFalse(health["verified"])

        health = mrs._provider_health_from_run({
            "status": "completed", "provider_status": "failed",
            "provider_calls_ok": 0, "provider_calls_failed": 2,
        })
        self.assertEqual(health["status"], "failed")
        self.assertFalse(health["verified"])

        health = mrs._provider_health_from_run({
            "status": "completed", "provider_status": "ready",
            "provider_calls_ok": 2, "provider_calls_failed": 0,
        })
        self.assertEqual(health["status"], "ready")
        self.assertTrue(health["verified"])


class TestCatalogComparison(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            {
                "product_id": "ZP1",
                "product_name": f"EPSON ECOTANK {TEST_MODEL} PRINTER",
                "short_name": "",
                "brand_name": "Epson",
                "category_id": "C1",
                "category_name": "Printer",
                "stock_qty": 0,
                "units_sold": 0,
                "last_sold_date": None,
            },
            {
                "product_id": "ZP2",
                "product_name": f"EPSON {TEST_MODEL} INK BOTTLE CYAN",
                "short_name": "",
                "brand_name": "Epson",
                "category_id": "C2",
                "category_name": "Ink",
                "stock_qty": 4,
                "units_sold": 7,
                "last_sold_date": date(2026, 1, 5),
            },
            {
                "product_id": "ZP3",
                "product_name": "CANON G3430 PRINTER",
                "short_name": "",
                "brand_name": "",
                "category_id": "C1",
                "category_name": "Printer",
                "stock_qty": 2,
                "units_sold": 1,
                "last_sold_date": date(2026, 1, 6),
            },
        ]

    def test_exact_match_includes_product_with_no_sales(self):
        result = mrs.compare_against_catalog(
            {"brand": TEST_BRAND, "model": TEST_MODEL, "variant": ""},
            self.catalog,
        )
        self.assertEqual(result["local_match_status"], "already_in_catalog")
        self.assertEqual(result["local_product_id"], "ZP1")
        self.assertEqual(result["local_stock"], 0.0)
        self.assertEqual(result["local_units_sold"], 0.0)
        self.assertIsNone(result["local_last_sold"])

    def test_variant_mismatch_is_only_a_possible_match(self):
        result = mrs.compare_against_catalog(
            {"brand": TEST_BRAND, "model": TEST_MODEL, "variant": "Magenta"},
            self.catalog,
        )
        self.assertEqual(result["local_match_status"], "possible_match")
        self.assertTrue(result["local_compare_note"])

    def test_blank_catalog_brand_is_only_a_possible_match(self):
        result = mrs.compare_against_catalog(
            {"brand": "Unknown", "model": "G3430", "variant": ""},
            self.catalog,
        )
        self.assertEqual(result["local_match_status"], "possible_match")
        self.assertEqual(result["local_product_id"], "ZP3")

    def test_missing_model_is_not_found(self):
        result = mrs.compare_against_catalog(
            {"brand": TEST_BRAND, "model": "ZZTOP9999", "variant": ""},
            self.catalog,
        )
        self.assertEqual(result["local_match_status"], "not_found")
        self.assertIsNone(result["local_product_id"])

    def test_model_substring_is_not_an_exact_or_possible_match(self):
        catalog = [{
            "product_id": "ZP9",
            "product_name": f"EPSON ECOTANK {TEST_MODEL}2 PRINTER",
            "short_name": "",
            "brand_name": "Epson",
            "category_id": "C1",
            "category_name": "Printer",
            "stock_qty": 1,
            "units_sold": 1,
            "last_sold_date": None,
        }]
        result = mrs.compare_against_catalog(
            {"brand": TEST_BRAND, "model": TEST_MODEL, "variant": ""},
            catalog,
        )
        self.assertEqual(result["local_match_status"], "not_found")


class TestSyntheticResearchRuns(unittest.TestCase):
    def setUp(self):
        self.run_ids = []

    def tearDown(self):
        for run_id in self.run_ids:
            _cleanup_run(run_id)

    def _new_run_id(self, suffix):
        run_id = f"mr-test-{suffix}"
        self.run_ids.append(run_id)
        return run_id

    def test_completed_run_persists_source_backed_observation(self):
        run_id = self._new_run_id("persist-1")
        _create_run(run_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search_printer_only,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 1, 5, 9, 0, 0),
            )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["stage"], "finished")
        observations = repo.get_run_observations(run_id)
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation["model"], TEST_MODEL)
        self.assertEqual(observation["price_amount"], 22500.0)
        self.assertEqual(observation["price_currency"], "BDT")
        self.assertEqual(observation["demand_status"], "bestseller")
        self.assertEqual(observation["release_status"], "new_release")
        self.assertEqual(observation["release_date"], date(2026, 1, 12))
        self.assertEqual(observation["first_observed_date"], date(2099, 1, 5))
        self.assertEqual(observation["local_match_status"], "not_found")
        self.assertTrue(observation["source_url"].startswith("https://"))

    def test_duplicate_results_across_queries_are_counted_once(self):
        run_id = self._new_run_id("dedupe")

        def _always_result(query, max_results=5):
            return _one_result()

        _create_run(run_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            run = mrs.run_research_job(
                run_id,
                search_fn=_always_result,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 1, 8, 9, 0, 0),
            )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["results_count"], 1)
        self.assertEqual(run["summary"]["results_count"], 1)
        self.assertEqual(repo.count_observations(run_id), 1)

    def test_first_observed_date_is_preserved_across_runs(self):
        first_id = self._new_run_id("first-1")
        second_id = self._new_run_id("first-2")
        _create_run(first_id)
        _create_run(second_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            mrs.run_research_job(
                first_id,
                search_fn=_search_printer_only,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 2, 5, 9, 0, 0),
            )
            mrs.run_research_job(
                second_id,
                search_fn=_search_printer_only,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 3, 5, 9, 0, 0),
            )
        second = repo.get_run_observations(second_id)[0]
        self.assertEqual(second["first_observed_date"], date(2099, 2, 5))

    def test_catalog_failure_marks_comparison_unavailable(self):
        run_id = self._new_run_id("catalog-down")

        def _raise_catalog():
            raise RuntimeError("synthetic catalog outage")

        _create_run(run_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search_printer_only,
                catalog_fn=_raise_catalog,
                now_fn=lambda: datetime(2099, 4, 5, 9, 0, 0),
            )
        self.assertEqual(run["status"], "completed")
        self.assertFalse(run["summary"]["comparison_available"])
        observation = repo.get_run_observations(run_id)[0]
        self.assertEqual(observation["local_match_status"], "comparison_unavailable")

    def test_missing_api_key_fails_without_spending_credits(self):
        run_id = self._new_run_id("no-key")
        _create_run(run_id)
        with patch("market_research_service.get_api_key", return_value=""):
            run = mrs.run_research_job(run_id, now_fn=lambda: datetime(2099, 5, 5, 9, 0, 0))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stage"], "provider_not_configured")
        self.assertEqual(repo.run_credit_total(run_id), 0)
        statuses = repo.get_source_statuses(run_id)
        self.assertEqual(len(statuses), len(mrs.BANGLADESH_MARKET_SOURCES))
        self.assertTrue(all(row["status"] == "not_checked" for row in statuses))
        self.assertTrue(all(row["checked_at"] is None for row in statuses))

    def test_monthly_credit_limit_blocks_search(self):
        run_id = self._new_run_id("month-limit")
        filler_id = self._new_run_id("month-filler")
        period = "2099-06"
        current = repo.monthly_credit_total(period)
        with get_connection() as conn:
            with conn.cursor() as cur:
                if current < 900:
                    cur.execute(
                        "INSERT INTO market_research_usage (run_id, period, credits) VALUES (%s, %s, %s)",
                        (filler_id, period, 900 - current),
                    )
            conn.commit()
        calls = []

        def _search(query, max_results=5):
            calls.append(query)
            return _empty_ok()

        _create_run(run_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 6, 5, 9, 0, 0),
            )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stage"], "monthly_credit_limit")
        self.assertEqual(calls, [])
        self.assertEqual(repo.run_credit_total(run_id), 0)

    def test_per_run_credit_limit_stops_provider_calls(self):
        run_id = self._new_run_id("run-limit")
        calls = []

        def _search(query, max_results=5):
            calls.append(query)
            return _empty_ok()

        _create_run(run_id, sources_total=2, queries_total=4)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES * 2), \
                patch.object(mrs, "MAX_CREDITS_PER_RESEARCH", 3):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 7, 5, 9, 0, 0),
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["provider_status"], "run_limit_reached")
        self.assertEqual(run["credits_used"], 3)

    def test_all_provider_failures_are_persisted_as_failed(self):
        run_id = self._new_run_id("all-failed")

        def _search(query, max_results=5):
            return {
                "ok": False,
                "results": [],
                "status_code": None,
                "error_code": "network_error",
                "error": "synthetic network outage",
            }

        _create_run(run_id)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 8, 20, 9, 0, 0),
            )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stage"], "provider_failed")
        self.assertEqual(run["provider_status"], "failed")
        statuses = repo.get_source_statuses(run_id)
        self.assertEqual([row["status"] for row in statuses], ["failed"])

    def test_failed_source_is_recorded_failed_and_run_is_not_completed(self):
        run_id = self._new_run_id("source-failed")
        calls = []

        def _search(query, max_results=5):
            calls.append(query)
            if len(calls) <= 2:
                return _empty_ok()
            return {
                "ok": False,
                "results": [],
                "status_code": None,
                "error_code": "network_error",
                "error": "synthetic source unreachable",
            }

        _create_run(run_id, sources_total=2, queries_total=4)
        with patch.object(mrs, "BANGLADESH_MARKET_SOURCES", SYNTHETIC_SOURCES * 2):
            run = mrs.run_research_job(
                run_id,
                search_fn=_search,
                catalog_fn=lambda: [],
                now_fn=lambda: datetime(2099, 8, 5, 9, 0, 0),
            )
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["provider_status"], "degraded")
        statuses = repo.get_source_statuses(run_id)
        self.assertIn("failed", [row["status"] for row in statuses])


class TestMarketDiscoveryApiAndTemplate(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("main._get_request_user", return_value=ADMIN_USER)
    @patch("product_research_service.get_categories", return_value=[
        {"category_id": "ZCAT1", "category_name": "Printer"},
    ])
    def test_categories_endpoint_returns_ids(self, mock_categories, mock_user):
        res = self.client.get("/api/new-products/categories")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["categories"][0]["category_id"], "ZCAT1")

    @patch("main._get_request_user", return_value=ADMIN_USER)
    @patch("product_research_service.start_product_research",
           side_effect=mrs.DuplicateResearchJobError("mr-existing"))
    def test_duplicate_research_returns_409_with_existing_job(self, mock_start, mock_user):
        res = self.client.post("/api/new-products/research")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json().get("job_id"), "mr-existing")

    def test_template_has_no_hardcoded_live_or_market_claims(self):
        path = Path(__file__).resolve().parent / "templates" / "new_products.html"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("Live Provider Ready", content)
        self.assertNotIn("Provider Live", content)
        self.assertIn("Provider Configured · Not Verified", content)
        self.assertIn("Provider Verified", content)
        self.assertIn("/api/new-products/categories", content)
        self.assertIn("/api/new-products/research", content)
        self.assertIn("saved source evidence only.", content.lower())


if __name__ == "__main__":
    unittest.main()
