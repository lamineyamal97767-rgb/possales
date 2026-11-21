"""Product Research Service for New Products Intelligence.

Local product reporting stays in this module. Market discovery is delegated to
``market_research_service`` so every visible market claim is backed by a stored
source observation instead of a hard-coded sample.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from db.connection import get_connection
from config import load_dotenv
import market_research_service

load_dotenv()

logger = logging.getLogger(__name__)

DuplicateResearchJobError = market_research_service.DuplicateResearchJobError
ProviderState = market_research_service.get_provider_health


def _resolve_new_products_menu_id() -> Optional[int]:
    """Retrieve menu ID for New Products from sidebar_menus table."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM sidebar_menus
                    WHERE LOWER(title) LIKE '%new product%' OR url LIKE '%new-products%'
                    LIMIT 1
                """)
                row = cur.fetchone()
                return int(row["id"]) if row and row.get("id") else None
    except Exception:
        return 29  # Default ID in seed data


def can_view_new_products(user: Mapping[str, Any] | None) -> bool:
    """
    Check if user has permission to view the New Products module.
    Admins and users with 'new-products' or menu ID grant are permitted.
    """
    if not user:
        return False
    role = str(user.get("role", "")).strip().lower()
    if role == "admin":
        return True

    allowed = user.get("allowed_menus")
    if allowed is None:
        return False
    if allowed == "*":
        return True

    if isinstance(allowed, list):
        lower_slugs = {str(x).strip().lower() for x in allowed if isinstance(x, str)}
        if any(slug in lower_slugs for slug in ("new-products", "new_products", "new products")):
            return True

        menu_id = _resolve_new_products_menu_id()
        if menu_id is not None:
            try:
                allowed_ids = {int(x) for x in allowed if str(x).isdigit() or isinstance(x, int)}
                if menu_id in allowed_ids:
                    return True
            except (ValueError, TypeError):
                pass

    return False


def _get_latest_sales_date() -> date:
    """Find the latest sales date in sales_details, falling back to today."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(SALES_DATE) as max_date FROM sales_details WHERE SALES_DATE IS NOT NULL")
                row = cur.fetchone()
                if row and row.get("max_date"):
                    val = row["max_date"]
                    if isinstance(val, (datetime, date)):
                        return val if isinstance(val, date) else val.date()
                    return date.fromisoformat(str(val)[:10])
    except Exception as e:
        logger.warning(f"Could not determine latest sales date: {e}")
    return date.today()


def _market_note(snapshot: Mapping[str, Any]) -> str:
    provider = snapshot.get("provider") or {}
    if provider.get("message"):
        return str(provider["message"])
    if not snapshot.get("available"):
        return "No market research has been stored yet. Only an Admin can start research."
    run = snapshot.get("run") or {}
    status = str(run.get("status") or "unknown").lower()
    if status == "completed":
        return "Showing the latest completed evidence-backed market research run."
    if status == "partial":
        return "Showing partial results from the latest run. Some sources or provider calls failed."
    if status == "failed":
        return "The latest market research run failed. No failed search is shown as completed."
    if status == "interrupted":
        return "The latest market research run was interrupted before completion."
    return "Showing persisted market research results."


def get_product_report(filters: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Generate product report based on view type: 'local', 'market', or 'comparison'.
    If dates are omitted, defaults to 30 days ending at the latest sales date.
    """
    view = str(filters.get("view", "local")).strip().lower()
    if view not in ("local", "market", "comparison"):
        raise ValueError(f"Invalid view '{view}'. Must be 'local', 'market', or 'comparison'.")

    end_date_raw = filters.get("end_date")
    start_date_raw = filters.get("start_date")

    if end_date_raw:
        end_d = date.fromisoformat(str(end_date_raw).strip())
    else:
        end_d = _get_latest_sales_date()

    if start_date_raw:
        start_d = date.fromisoformat(str(start_date_raw).strip())
    else:
        start_d = end_d - timedelta(days=30)

    if start_d > end_d:
        raise ValueError("start_date cannot be after end_date.")

    category_id = filters.get("category_id")
    q = filters.get("q")
    brand = filters.get("brand")
    market_category = filters.get("market_category")

    with get_connection() as conn:
        with conn.cursor() as cur:
            conditions = ["sd.SALES_DATE BETWEEN %s AND %s"]
            params: List[Any] = [start_d.isoformat(), end_d.isoformat()]

            if category_id:
                conditions.append("p.CATEGORY_ID = %s")
                params.append(category_id.strip())

            if q:
                conditions.append("(p.PRODUCT_NAME LIKE %s OR p.PRODUCT_ID LIKE %s)")
                params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])

            where_sql = " AND ".join(conditions)

            sql = f"""
                SELECT 
                    p.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'General') as category_name,
                    p.SALES_PRICE as sales_price,
                    COALESCE(st.stock_qty, 0) as current_stock,
                    COALESCE(SUM(sd.QTY), 0) as period_units_sold,
                    ROUND(COALESCE(SUM(sd.QTY * sd.UNIT_PRICE), 0), 2) as period_revenue,
                    COUNT(DISTINCT sd.SALES_ID) as order_count,
                    MAX(sd.SALES_DATE) as last_sold_date
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                LEFT JOIN sales_details sd ON p.PRODUCT_ID = sd.PRODUCT_ID
                WHERE {where_sql}
                GROUP BY p.PRODUCT_ID, p.PRODUCT_NAME, category_name, p.SALES_PRICE, st.stock_qty
                ORDER BY period_units_sold DESC
                LIMIT 100
            """
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

    for r in rows:
        if isinstance(r.get("last_sold_date"), (date, datetime)):
            r["last_sold_date"] = r["last_sold_date"].strftime("%Y-%m-%d")
        r["period_units_sold"] = int(r.get("period_units_sold") or 0)
        r["current_stock"] = int(r.get("current_stock") or 0)
        r["period_revenue"] = float(r.get("period_revenue") or 0.0)
        r["sales_price"] = float(r.get("sales_price") or 0.0)

    market_research = market_research_service.get_market_snapshot(
        {
            "q": q,
            "brand": brand,
            "market_category": market_category,
        }
    )

    return {
        "view": view,
        "date_range": {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "is_default_range": not bool(filters.get("start_date") and filters.get("end_date")),
        },
        "filters": {
            "category_id": category_id,
            "q": q,
            "brand": brand,
            "market_category": market_category,
        },
        "total_products": len(rows),
        "products": rows,
        "market_insights": [],
        "market_research": market_research,
        "note": _market_note(market_research) if view in ("market", "comparison")
        else "Local inventory and sales performance view. Market evidence is stored separately.",
    }


def get_categories() -> List[Dict[str, str]]:
    """Return local catalog category IDs and names for the filter control."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT p.CATEGORY_ID AS category_id,
                       COALESCE(c.CATEGORY_NAME, 'Uncategorized') AS category_name
                FROM product p
                LEFT JOIN category c ON c.CATEGORY_ID = p.CATEGORY_ID
                WHERE p.CATEGORY_ID IS NOT NULL AND p.CATEGORY_ID <> ''
                ORDER BY category_name ASC, category_id ASC
            """)
            rows = cur.fetchall()
    return [
        {
            "category_id": str(row.get("category_id") or ""),
            "category_name": str(row.get("category_name") or "Uncategorized"),
        }
        for row in rows
        if row.get("category_id")
    ]


def tavily_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    """Backward-compatible wrapper around the evidence-based provider client."""
    return market_research_service.tavily_search(query, max_results=max_results)


def start_product_research(user_id: Any) -> str:
    """Queue an Admin research run. Raises DuplicateResearchJobError when busy."""
    market_research_service.initialize()
    return market_research_service.start_product_research_job(user_id)


def get_product_research_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve persisted progress and results for a research job."""
    return market_research_service.get_research_status(job_id)


def initialize_market_research() -> None:
    """Create application-owned tables and interrupt pre-restart jobs."""
    market_research_service.initialize()
