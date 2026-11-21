"""Persistence for Report 10: Bangladesh Model Demand.

The application owns four tables:

- ``market_research_runs``: one row per Admin-triggered research run.
- ``market_research_observations``: evidence-backed model observations. Each row
  ties a model to the exact source URL, collection time, price/currency,
  availability, release/first-observed status, demand evidence and the local
  catalog comparison.
- ``market_research_sources``: per-run source reachability. A source that could
  not be reached is stored as ``failed`` instead of being reported as covered.
- ``market_research_usage``: the Tavily credit ledger used to enforce the
  50-credit per-run and 900-credit per-calendar-month limits.

Business tables (``product``, ``sales_details``, ``stock_non_serial`` and
``stock_serial``) are only read. Customer data is never stored or sent to the
search provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .connection import get_connection

logger = logging.getLogger("market_research")

TABLES_SQL = (
    """
    CREATE TABLE IF NOT EXISTS market_research_runs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        user_id INT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        stage VARCHAR(40) NOT NULL DEFAULT 'queued',
        progress TINYINT NOT NULL DEFAULT 0,
        provider VARCHAR(30) NOT NULL DEFAULT 'tavily',
        provider_status VARCHAR(30) NOT NULL DEFAULT 'unknown',
        provider_calls_ok INT NOT NULL DEFAULT 0,
        provider_calls_failed INT NOT NULL DEFAULT 0,
        sources_total INT NOT NULL DEFAULT 0,
        sources_available INT NOT NULL DEFAULT 0,
        sources_no_results INT NOT NULL DEFAULT 0,
        sources_failed INT NOT NULL DEFAULT 0,
        queries_attempted INT NOT NULL DEFAULT 0,
        credits_used INT NOT NULL DEFAULT 0,
        results_count INT NOT NULL DEFAULT 0,
        error VARCHAR(600) NULL,
        summary LONGTEXT NULL,
        started_at DATETIME NULL,
        completed_at DATETIME NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_market_research_run (run_id),
        KEY idx_market_research_status (status),
        KEY idx_market_research_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS market_research_observations (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        observation_key VARCHAR(64) NOT NULL,
        brand VARCHAR(120) NOT NULL DEFAULT 'Unknown',
        model VARCHAR(160) NOT NULL,
        variant VARCHAR(120) NULL,
        category VARCHAR(80) NOT NULL DEFAULT 'Other',
        source_name VARCHAR(120) NOT NULL,
        source_url VARCHAR(1000) NOT NULL,
        source_domain VARCHAR(200) NOT NULL,
        source_date DATE NULL,
        collected_at DATETIME NOT NULL,
        price_amount DECIMAL(14, 2) NULL,
        price_currency VARCHAR(10) NULL,
        availability VARCHAR(60) NULL,
        release_status VARCHAR(30) NOT NULL DEFAULT 'first_observed',
        release_date DATE NULL,
        release_evidence VARCHAR(600) NULL,
        first_observed_date DATE NOT NULL,
        demand_status VARCHAR(30) NOT NULL DEFAULT 'demand_unknown',
        demand_evidence VARCHAR(600) NULL,
        demand_source_url VARCHAR(1000) NULL,
        local_match_status VARCHAR(30) NOT NULL DEFAULT 'not_found',
        local_product_id VARCHAR(20) NULL,
        local_product_name VARCHAR(220) NULL,
        local_category VARCHAR(100) NULL,
        local_stock FLOAT NULL,
        local_units_sold FLOAT NULL,
        local_last_sold DATE NULL,
        local_compare_note VARCHAR(255) NULL,
        raw_title VARCHAR(600) NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_market_observation (run_id, observation_key),
        KEY idx_market_observation_run (run_id),
        KEY idx_market_observation_brand (brand),
        KEY idx_market_observation_category (category),
        KEY idx_market_observation_demand (demand_status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS market_research_sources (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        source_name VARCHAR(120) NOT NULL,
        source_type VARCHAR(30) NOT NULL,
        base_url VARCHAR(300) NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'not_checked',
        results_count INT NOT NULL DEFAULT 0,
        error VARCHAR(500) NULL,
        checked_at DATETIME NULL,
        UNIQUE KEY uq_market_source (run_id, source_name),
        KEY idx_market_source_run (run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS market_research_usage (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        period CHAR(7) NOT NULL,
        credits INT NOT NULL DEFAULT 0,
        status VARCHAR(20) NOT NULL DEFAULT 'reserved',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        KEY idx_market_usage_period (period),
        KEY idx_market_usage_run (run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
)

_OBSERVATION_COLUMNS = (
    "run_id",
    "observation_key",
    "brand",
    "model",
    "variant",
    "category",
    "source_name",
    "source_url",
    "source_domain",
    "source_date",
    "collected_at",
    "price_amount",
    "price_currency",
    "availability",
    "release_status",
    "release_date",
    "release_evidence",
    "first_observed_date",
    "demand_status",
    "demand_evidence",
    "demand_source_url",
    "local_match_status",
    "local_product_id",
    "local_product_name",
    "local_category",
    "local_stock",
    "local_units_sold",
    "local_last_sold",
    "local_compare_note",
    "raw_title",
)

_RUN_UPDATABLE_COLUMNS = {
    "status",
    "stage",
    "progress",
    "provider_status",
    "provider_calls_ok",
    "provider_calls_failed",
    "sources_total",
    "sources_available",
    "sources_no_results",
    "sources_failed",
    "queries_attempted",
    "credits_used",
    "results_count",
    "error",
    "summary",
    "started_at",
    "completed_at",
}

_TABLES_INITIALIZED = False


def observation_key(brand: str, model: str, category: str, source_domain: str) -> str:
    """Stable key used to deduplicate one model per source inside a run."""
    raw = "|".join(
        str(part or "").strip().lower()
        for part in (brand, model, category, source_domain)
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _connect(conn=None):
    return conn if conn is not None else get_connection()


def init_market_research_tables(conn=None) -> None:
    """Create the application-owned tables. Safe to call repeatedly."""
    global _TABLES_INITIALIZED
    if _TABLES_INITIALIZED and conn is None:
        return
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            for statement in TABLES_SQL:
                cur.execute(statement)
        if conn is None:
            c.commit()
        _TABLES_INITIALIZED = True
    finally:
        if conn is None:
            c.close()


def create_run(
    run_id: str,
    user_id: Optional[int],
    provider: str,
    sources_total: int,
    queries_total: int,
    conn=None,
) -> Dict[str, Any]:
    """Insert a queued run and return the stored row."""
    init_market_research_tables(conn)
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market_research_runs
                    (run_id, user_id, status, stage, progress, provider,
                     sources_total, summary)
                VALUES (%s, %s, 'pending', 'queued', 0, %s, %s, %s)
                """,
                (
                    run_id,
                    user_id,
                    provider,
                    sources_total,
                    json.dumps({"queries_planned": queries_total}),
                ),
            )
        if conn is None:
            c.commit()
        return get_run(run_id, conn=c) or {}
    finally:
        if conn is None:
            c.close()


def update_run(run_id: str, conn=None, **fields: Any) -> bool:
    """Update whitelisted run columns. ``summary`` mappings are JSON encoded."""
    updates = {key: value for key, value in fields.items() if key in _RUN_UPDATABLE_COLUMNS}
    if not updates:
        return False
    if "summary" in updates and isinstance(updates["summary"], (dict, list)):
        updates["summary"] = json.dumps(updates["summary"], ensure_ascii=False)
    assignments = ", ".join(f"{column} = %s" for column in updates)
    values = list(updates.values()) + [run_id]
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                f"UPDATE market_research_runs SET {assignments} WHERE run_id = %s",
                tuple(values),
            )
            changed = cur.rowcount
        if conn is None:
            c.commit()
        return bool(changed)
    finally:
        if conn is None:
            c.close()


def _decode_summary(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    result = dict(row)
    raw_summary = result.get("summary")
    if isinstance(raw_summary, str) and raw_summary.strip():
        try:
            result["summary"] = json.loads(raw_summary)
        except (TypeError, ValueError):
            result["summary"] = {"raw": raw_summary}
    elif raw_summary is None:
        result["summary"] = {}
    return result


def get_run(run_id: str, conn=None) -> Optional[Dict[str, Any]]:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM market_research_runs WHERE run_id = %s LIMIT 1",
                (run_id,),
            )
            return _decode_summary(cur.fetchone())
    finally:
        if conn is None:
            c.close()


def get_latest_run(conn=None) -> Optional[Dict[str, Any]]:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM market_research_runs ORDER BY id DESC LIMIT 1"
            )
            return _decode_summary(cur.fetchone())
    finally:
        if conn is None:
            c.close()


def get_active_run(conn=None) -> Optional[Dict[str, Any]]:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM market_research_runs
                WHERE status IN ('pending', 'running')
                ORDER BY id DESC LIMIT 1
                """
            )
            return _decode_summary(cur.fetchone())
    finally:
        if conn is None:
            c.close()


def get_previous_first_observed(
    brand: str, model: str, category: str, conn=None
) -> Optional[date]:
    """Earliest date this exact brand/model/category was previously observed."""
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(first_observed_date) AS first_seen
                FROM market_research_observations
                WHERE LOWER(brand) = %s AND LOWER(model) = %s AND LOWER(category) = %s
                """,
                (brand.strip().lower(), model.strip().lower(), category.strip().lower()),
            )
            row = cur.fetchone()
            value = (row or {}).get("first_seen")
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            if value:
                return date.fromisoformat(str(value)[:10])
            return None
    finally:
        if conn is None:
            c.close()


def upsert_source_status(
    run_id: str,
    source_name: str,
    source_type: str,
    base_url: str,
    status: str,
    results_count: int = 0,
    error: Optional[str] = None,
    checked_at: Optional[datetime] = None,
    conn=None,
) -> None:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market_research_sources
                    (run_id, source_name, source_type, base_url, status,
                     results_count, error, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    results_count = VALUES(results_count),
                    error = VALUES(error),
                    checked_at = VALUES(checked_at)
                """,
                (
                    run_id,
                    source_name,
                    source_type,
                    base_url,
                    status,
                    int(results_count or 0),
                    (error or "")[:500] or None,
                    checked_at if checked_at is not None else (
                        None if status == "not_checked" else datetime.now()
                    ),
                ),
            )
        if conn is None:
            c.commit()
    finally:
        if conn is None:
            c.close()


def get_source_statuses(run_id: str, conn=None) -> List[Dict[str, Any]]:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT source_name, source_type, base_url, status, results_count,
                       error, checked_at
                FROM market_research_sources
                WHERE run_id = %s
                ORDER BY source_name ASC
                """,
                (run_id,),
            )
            return list(cur.fetchall())
    finally:
        if conn is None:
            c.close()


def insert_observation(run_id: str, observation: Mapping[str, Any], conn=None) -> None:
    c = _connect(conn)
    try:
        values = []
        for column in _OBSERVATION_COLUMNS:
            value = run_id if column == "run_id" else observation.get(column)
            values.append(value)
        placeholders = ", ".join(["%s"] * len(_OBSERVATION_COLUMNS))
        update_columns = [
            column
            for column in _OBSERVATION_COLUMNS
            if column not in ("run_id", "observation_key")
        ]
        update_sql = ", ".join(
            f"{column} = VALUES({column})" for column in update_columns
        )
        with c.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO market_research_observations
                    ({', '.join(_OBSERVATION_COLUMNS)})
                VALUES ({placeholders})
                ON DUPLICATE KEY UPDATE {update_sql}
                """,
                tuple(values),
            )
        if conn is None:
            c.commit()
    finally:
        if conn is None:
            c.close()


def get_run_observations(run_id: str, conn=None) -> List[Dict[str, Any]]:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM market_research_observations
                WHERE run_id = %s
                ORDER BY demand_status = 'demand_unknown' ASC,
                         brand ASC, model ASC, source_name ASC
                """,
                (run_id,),
            )
            return list(cur.fetchall())
    finally:
        if conn is None:
            c.close()


def count_observations(run_id: str, conn=None) -> int:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM market_research_observations WHERE run_id = %s",
                (run_id,),
            )
            return int((cur.fetchone() or {}).get("cnt") or 0)
    finally:
        if conn is None:
            c.close()


def monthly_credit_total(period: str, conn=None) -> int:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM market_research_usage WHERE period = %s",
                (period,),
            )
            return int((cur.fetchone() or {}).get("total") or 0)
    finally:
        if conn is None:
            c.close()


def run_credit_total(run_id: str, conn=None) -> int:
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM market_research_usage WHERE run_id = %s",
                (run_id,),
            )
            return int((cur.fetchone() or {}).get("total") or 0)
    finally:
        if conn is None:
            c.close()


def reserve_credits(
    run_id: str,
    period: str,
    credits: int,
    monthly_limit: int,
    per_run_limit: int,
    conn=None,
) -> Dict[str, Any]:
    """Atomically reserve provider credits.

    Returns ``{"ok": bool, "reason": ..., "month_total": ..., "run_total": ...}``.
    A named MySQL lock avoids two concurrent jobs overshooting the monthly cap.
    """
    init_market_research_tables(conn)
    credits = max(1, int(credits))
    c = _connect(conn)
    lock_name = "market_research_credit_ledger"
    result: Dict[str, Any] = {
        "ok": False,
        "reason": "credit_lock_unavailable",
        "month_total": 0,
        "run_total": 0,
    }
    try:
        with c.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 5) AS acquired", (lock_name,))
            acquired = int((cur.fetchone() or {}).get("acquired") or 0)
            if acquired:
                try:
                    cur.execute(
                        "SELECT COALESCE(SUM(credits), 0) AS total FROM market_research_usage WHERE period = %s",
                        (period,),
                    )
                    month_total = int((cur.fetchone() or {}).get("total") or 0)
                    cur.execute(
                        "SELECT COALESCE(SUM(credits), 0) AS total FROM market_research_usage WHERE run_id = %s",
                        (run_id,),
                    )
                    run_total = int((cur.fetchone() or {}).get("total") or 0)

                    if run_total + credits > per_run_limit:
                        result = {"ok": False, "reason": "run_limit_reached",
                                  "month_total": month_total, "run_total": run_total}
                    elif month_total + credits > monthly_limit:
                        result = {"ok": False, "reason": "monthly_limit_reached",
                                  "month_total": month_total, "run_total": run_total}
                    else:
                        cur.execute(
                            """
                            INSERT INTO market_research_usage (run_id, period, credits, status)
                            VALUES (%s, %s, %s, 'reserved')
                            """,
                            (run_id, period, credits),
                        )
                        result = {"ok": True, "reason": "reserved",
                                  "month_total": month_total + credits,
                                  "run_total": run_total + credits}
                finally:
                    cur.execute("SELECT RELEASE_LOCK(%s) AS released", (lock_name,))
        if conn is None:
            c.commit()
        return result
    finally:
        if conn is None:
            c.close()


def mark_interrupted_runs(stale_after_minutes: Optional[int] = None, conn=None) -> int:
    """Mark abandoned pending/running runs as interrupted, never completed."""
    conditions = ["status IN ('pending', 'running')"]
    params: List[Any] = []
    if stale_after_minutes is not None:
        cutoff = datetime.now() - timedelta(minutes=stale_after_minutes)
        conditions.append("updated_at < %s")
        params.append(cutoff)
    sql = f"""
        UPDATE market_research_runs
        SET status = 'interrupted',
            stage = 'interrupted',
            completed_at = %s,
            error = 'Research job was interrupted before completion. Partial results, if any, are preserved.'
        WHERE {' AND '.join(conditions)}
    """
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(sql, tuple([datetime.now()] + params))
            changed = cur.rowcount
        if conn is None:
            c.commit()
        return int(changed or 0)
    finally:
        if conn is None:
            c.close()


def reset_tables_for_tests(conn=None) -> None:
    """Delete application-owned research rows. Used by tests only."""
    c = _connect(conn)
    try:
        with c.cursor() as cur:
            for table in (
                "market_research_usage",
                "market_research_sources",
                "market_research_observations",
                "market_research_runs",
            ):
                cur.execute(f"DELETE FROM {table}")
        if conn is None:
            c.commit()
    finally:
        if conn is None:
            c.close()
