"""Temporary MySQL schema for tests that must never touch real business data.

``IsolatedSchema.create()`` makes ``zz_<name>_test_<random>``, copies the
structure of the requested tables from the configured database with
``CREATE TABLE ... LIKE`` (no rows), optionally copies configuration rows
(the sidebar menus), points ``DB_NAME`` at the new schema and runs the seed
function there. ``drop()`` restores ``DB_NAME`` and drops the schema. Every
destructive statement is guarded by a strict name check.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Callable, Iterable, Optional
from unittest.mock import patch

from db.connection import get_connection

_SAFE_NAME = re.compile(r"zz_[a-z]+_test_[0-9a-f]{10}")


class IsolatedSchema:
    def __init__(self, prefix: str, tables: Iterable[str], copy_rows: Iterable[str] = ("sidebar_menus",)):
        self.source = os.getenv("DB_NAME", "print_db")
        self.name = f"{prefix}{uuid.uuid4().hex[:10]}"
        self.tables = tuple(tables)
        self.copy_rows = tuple(copy_rows)
        self._env_patch = None
        self._assert_safe()

    def _assert_safe(self) -> None:
        if not _SAFE_NAME.fullmatch(self.name) or self.name == self.source:
            raise RuntimeError("Refusing to use an unexpected database name for tests.")

    def create(self, seed: Optional[Callable] = None) -> None:
        try:
            self._create_structure()
            # A configured read-only user may have no grants on the temporary schema; the
            # code under test still runs its queries inside START TRANSACTION READ ONLY.
            self._env_patch = patch.dict(os.environ, {"DB_NAME": self.name, "DB_READONLY_USER": ""})
            self._env_patch.start()
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT DATABASE() AS db")
                    if cur.fetchone()["db"] != self.name:
                        raise RuntimeError("Test connection is not using the temporary schema.")
                    if seed:
                        seed(cur)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            try:
                self.drop()  # never leave a half-built schema behind
            except Exception:
                pass  # keep the original error visible
            raise

    def _create_structure(self) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE `{self.name}` CHARACTER SET utf8mb4")
                for table in self.tables:
                    cur.execute(f"CREATE TABLE `{self.name}`.`{table}` LIKE `{self.source}`.`{table}`")
                for table in self.copy_rows:
                    cur.execute(f"INSERT INTO `{self.name}`.`{table}` SELECT * FROM `{self.source}`.`{table}`")
            conn.commit()
        finally:
            conn.close()

    def drop(self) -> None:
        if self._env_patch is not None:
            self._env_patch.stop()
            self._env_patch = None
        self._assert_safe()
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{self.name}`")
            conn.commit()
        finally:
            conn.close()
