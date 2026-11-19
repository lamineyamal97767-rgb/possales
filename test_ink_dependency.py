"""
Unit & Integration Test Suite for Report 03: Printer <-> Ink Dependency.
Tests cover:
1. Bidirectional relationships (Printer -> Ink and Ink -> Printer).
2. Distinction between Admin Confirmed and Possible Match (Sales History).
3. Duplicate relationship prevention.
4. Invalid product handling (non-existent printer or ink).
5. Admin permission enforcement (401 unauthenticated, 403 non-admin, 201 admin).
6. Empty database / empty relations handling (no hallucinated compatibility).
7. Migration execution, status check, and rollback.
"""

import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app
import db.ink_dependency as ink_dep
from db.migrations.migrate_printer_ink import migrate_up, migrate_down, migration_status


class InkDependencyUnitTests(unittest.TestCase):
    def setUp(self):
        # Ensure migration table is up for test runs
        migrate_up()

    def test_01_migration_lifecycle(self):
        """Test migration status, rollback (down), and reapplying (up)."""
        status_before = migration_status()
        self.assertTrue(status_before["table_exists"], "Table should exist after migrate_up")

        # Rollback
        down_ok = migrate_down()
        self.assertTrue(down_ok, "migrate_down should succeed")
        status_down = migration_status()
        self.assertFalse(status_down["table_exists"], "Table should not exist after rollback")

        # Re-apply
        up_ok = migrate_up()
        self.assertTrue(up_ok, "migrate_up should succeed")
        status_up = migration_status()
        self.assertTrue(status_up["table_exists"], "Table should exist after re-applying")

    def test_02_product_validation(self):
        """Test validation of valid and invalid printer and ink IDs."""
        printers = ink_dep.get_printer_models_list()
        inks = ink_dep.get_ink_products_list()

        if printers:
            valid_p = ink_dep.validate_printer_id(printers[0]["product_id"])
            self.assertIsNotNone(valid_p, "Valid printer should be recognized")
            self.assertEqual(valid_p["product_id"], printers[0]["product_id"])

        if inks:
            valid_i = ink_dep.validate_ink_id(inks[0]["product_id"])
            self.assertIsNotNone(valid_i, "Valid ink should be recognized")
            self.assertEqual(valid_i["product_id"], inks[0]["product_id"])

        # Non-existent products
        self.assertIsNone(ink_dep.validate_printer_id("INVALID_PRINTER_999"))
        self.assertIsNone(ink_dep.validate_ink_id("INVALID_INK_999"))
        self.assertIsNone(ink_dep.validate_printer_id(""))
        self.assertIsNone(ink_dep.validate_ink_id(""))

    def test_03_invalid_product_exception_on_relation_add(self):
        """Adding a relation with invalid printer or ink ID must raise ValueError."""
        printers = ink_dep.get_printer_models_list()
        inks = ink_dep.get_ink_products_list()
        valid_p = printers[0]["product_id"] if printers else "PD00000068"
        valid_i = inks[0]["product_id"] if inks else "PD00000001"

        # Invalid printer
        with self.assertRaises(ValueError) as ctx:
            ink_dep.add_printer_ink_relation("NON_EXISTENT_P", valid_i)
        self.assertIn("Invalid printer", str(ctx.exception))

        # Invalid ink
        with self.assertRaises(ValueError) as ctx:
            ink_dep.add_printer_ink_relation(valid_p, "NON_EXISTENT_I")
        self.assertIn("Invalid ink", str(ctx.exception))

        # Empty inputs
        with self.assertRaises(ValueError):
            ink_dep.add_printer_ink_relation("", "")

    def test_04_crud_relation_and_duplicate_prevention(self):
        """Test adding a relation, preventing duplicates, updating, and deactivating."""
        printers = ink_dep.get_printer_models_list()
        inks = ink_dep.get_ink_products_list()
        if not printers or not inks:
            self.skipTest("No products in database to test relations")

        p_id = printers[0]["product_id"]
        i_id = inks[0]["product_id"]

        # Clean any preexisting test relation
        rel_list = ink_dep.list_printer_ink_relations(printer_id=p_id, ink_id=i_id, status=None)
        for r in rel_list:
            ink_dep.delete_printer_ink_relation(r["id"])

        # 1. Add relationship
        result = ink_dep.add_printer_ink_relation(
            printer_id=p_id,
            ink_id=i_id,
            source="OEM Specification Test",
            notes="Unit test relationship",
            username="test_admin",
        )
        self.assertTrue(result["id"] > 0)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["source"], "OEM Specification Test")
        rel_id = result["id"]

        # 2. Duplicate prevention: adding again while active must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            ink_dep.add_printer_ink_relation(p_id, i_id, source="Duplicate attempt")
        self.assertIn("Duplicate", str(ctx.exception))

        # 3. Update relation
        up_ok = ink_dep.update_printer_ink_relation(
            relation_id=rel_id,
            source="Updated Source",
            notes="Updated Notes",
            username="test_admin",
        )
        self.assertTrue(up_ok)

        # Verify update
        relations = ink_dep.list_printer_ink_relations(printer_id=p_id, ink_id=i_id, status="active")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["source"], "Updated Source")
        self.assertEqual(relations[0]["notes"], "Updated Notes")

        # 4. Deactivate relation
        deact_ok = ink_dep.deactivate_printer_ink_relation(rel_id, username="test_admin")
        self.assertTrue(deact_ok)

        # Verify it's no longer in active relations
        active_relations = ink_dep.list_printer_ink_relations(printer_id=p_id, ink_id=i_id, status="active")
        self.assertEqual(len(active_relations), 0)

        # 5. Reactivate: adding an inactive relation should reactivate it seamlessly
        reactivate_res = ink_dep.add_printer_ink_relation(
            p_id, i_id, source="Reactivated Source", notes="Reactivated", username="test_admin"
        )
        self.assertEqual(reactivate_res["id"], rel_id)
        self.assertTrue(reactivate_res.get("reactivated"))

        # Clean up
        ink_dep.delete_printer_ink_relation(rel_id)

    def test_05_bidirectional_matrices(self):
        """Test Printer -> Ink and Ink -> Printer matrices for confirmed relations."""
        printers = ink_dep.get_printer_models_list()
        inks = ink_dep.get_ink_products_list()
        if not printers or not inks:
            self.skipTest("No products in database to test matrices")

        p_id = printers[0]["product_id"]
        i_id = inks[0]["product_id"]

        # Add a confirmed relation
        rel = ink_dep.add_printer_ink_relation(p_id, i_id, source="Admin confirmed", notes="Test spec")
        rel_id = rel["id"]

        try:
            # 1. Printer -> Ink
            p_matrix = ink_dep.get_printer_ink_matrix(p_id)
            self.assertIn("confirmed_inks", p_matrix)
            self.assertIn("possible_inks", p_matrix)
            confirmed_ink_ids = [c["ink_id"] for c in p_matrix["confirmed_inks"]]
            self.assertIn(i_id, confirmed_ink_ids, "Ink should be in confirmed_inks")

            # Verify verified attributes
            conf_item = next(c for c in p_matrix["confirmed_inks"] if c["ink_id"] == i_id)
            self.assertTrue(conf_item["is_confirmed"])
            self.assertEqual(conf_item["match_type"], "Admin confirmed")

            # 2. Ink -> Printer
            i_matrix = ink_dep.get_ink_printer_matrix(i_id)
            self.assertIn("confirmed_printers", i_matrix)
            self.assertIn("possible_printers", i_matrix)
            confirmed_printer_ids = [p["printer_id"] for p in i_matrix["confirmed_printers"]]
            self.assertIn(p_id, confirmed_printer_ids, "Printer should be in confirmed_printers")

            conf_p_item = next(p for p in i_matrix["confirmed_printers"] if p["printer_id"] == p_id)
            self.assertTrue(conf_p_item["is_confirmed"])
            self.assertEqual(conf_p_item["match_type"], "Admin confirmed")

        finally:
            ink_dep.delete_printer_ink_relation(rel_id)

    def test_06_sales_history_is_never_labeled_confirmed(self):
        """
        Verify that items originating solely from customer purchase history
        are strictly labeled as 'Possible match' and is_confirmed=False.
        """
        printers = ink_dep.get_printer_models_list()
        if not printers:
            self.skipTest("No printers available")

        # Find a printer with possible inks
        for p in printers[:5]:
            matrix = ink_dep.get_printer_ink_matrix(p["product_id"])
            for poss in matrix.get("possible_inks", []):
                self.assertFalse(poss["is_confirmed"], "Possible match must not have is_confirmed=True")
                self.assertEqual(poss["match_type"], "Possible match")
                self.assertIn("not guarantee", poss.get("match_disclaimer", "").lower())

    def test_07_empty_relations_does_not_assume_compatibility(self):
        """When no confirmed relations exist, confirmed_inks / confirmed_printers must be empty."""
        printers = ink_dep.get_printer_models_list()
        if not printers:
            self.skipTest("No printers available")

        # Pick the last printer (less likely to have test relations)
        target_p = printers[-1]["product_id"]

        # Ensure no active confirmed relations exist for it
        for r in ink_dep.list_printer_ink_relations(printer_id=target_p, status="active"):
            ink_dep.delete_printer_ink_relation(r["id"])

        matrix = ink_dep.get_printer_ink_matrix(target_p)
        self.assertEqual(matrix["confirmed_inks"], [], "confirmed_inks must be empty when none configured")
        self.assertEqual(matrix["confirmed_count"], 0)


class InkDependencyAPIPermissionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        migrate_up()

    @patch("main.is_authenticated", return_value=False)
    @patch("main._get_request_user", return_value=None)
    def test_08_unauthenticated_api_requests(self, mock_user, mock_auth):
        """Unauthenticated requests to admin modification endpoints must be rejected with 401."""
        # Add relation without auth -> 401
        res = self.client.post("/api/ink-dependency/relations", json={"printer_id": "P1", "ink_id": "I1"})
        self.assertEqual(res.status_code, 401)

        # Update relation without auth -> 401
        res = self.client.put("/api/ink-dependency/relations/1", json={"status": "inactive"})
        self.assertEqual(res.status_code, 401)

        # Delete relation without auth -> 401
        res = self.client.delete("/api/ink-dependency/relations/1")
        self.assertEqual(res.status_code, 401)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user", return_value={"id": 2, "username": "regular_user", "role": "User"})
    def test_09_non_admin_forbidden_from_modifications(self, mock_user, mock_auth):
        """Users with role='User' (non-admin) must be rejected with 403 Forbidden."""
        res = self.client.post("/api/ink-dependency/relations", json={"printer_id": "P1", "ink_id": "I1"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("Admin privileges required", res.json().get("message", ""))

        res = self.client.put("/api/ink-dependency/relations/1", json={"status": "inactive"})
        self.assertEqual(res.status_code, 403)

        res = self.client.delete("/api/ink-dependency/relations/1")
        self.assertEqual(res.status_code, 403)

    @patch("main.is_authenticated", return_value=True)
    @patch("main._get_request_user", return_value={"id": 1, "username": "admin", "role": "Admin"})
    def test_10_admin_relation_lifecycle_via_api(self, mock_user, mock_auth):
        """Admin user can add, query, update, and deactivate relationships via API."""
        printers = ink_dep.get_printer_models_list()
        inks = ink_dep.get_ink_products_list()
        if not printers or not inks:
            self.skipTest("No products in database")

        p_id = printers[0]["product_id"]
        i_id = inks[0]["product_id"]

        # Clean up any existing test relation
        for r in ink_dep.list_printer_ink_relations(printer_id=p_id, ink_id=i_id, status=None):
            ink_dep.delete_printer_ink_relation(r["id"])

        # 1. Admin adds relationship -> 201 Created
        post_res = self.client.post(
            "/api/ink-dependency/relations",
            json={"printer_id": p_id, "ink_id": i_id, "source": "API Test", "notes": "From TestClient"},
        )
        self.assertEqual(post_res.status_code, 201)
        data = post_res.json()
        self.assertTrue(data.get("success"))
        rel_id = data["data"]["id"]

        # 2. Duplicate via API -> 409 Conflict
        dup_res = self.client.post(
            "/api/ink-dependency/relations",
            json={"printer_id": p_id, "ink_id": i_id},
        )
        self.assertEqual(dup_res.status_code, 409)

        # 3. Invalid product via API -> 422
        bad_res = self.client.post(
            "/api/ink-dependency/relations",
            json={"printer_id": "INVALID_P", "ink_id": i_id},
        )
        self.assertEqual(bad_res.status_code, 422)

        # 4. List relations via API -> 200
        list_res = self.client.get(f"/api/ink-dependency/relations?printer_id={p_id}")
        self.assertEqual(list_res.status_code, 200)
        self.assertTrue(any(r["id"] == rel_id for r in list_res.json().get("relations", [])))

        # 5. Update relation via API -> 200
        put_res = self.client.put(
            f"/api/ink-dependency/relations/{rel_id}",
            json={"source": "Updated via API", "notes": "New Notes"},
        )
        self.assertEqual(put_res.status_code, 200)

        # 6. Deactivate relation via API -> 200
        del_res = self.client.delete(f"/api/ink-dependency/relations/{rel_id}")
        self.assertEqual(del_res.status_code, 200)

        # Verify inactive
        active_list = ink_dep.list_printer_ink_relations(printer_id=p_id, ink_id=i_id, status="active")
        self.assertEqual(len(active_list), 0)

        # Permanent clean up
        ink_dep.delete_printer_ink_relation(rel_id)


if __name__ == "__main__":
    unittest.main()
