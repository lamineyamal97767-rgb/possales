"""Equivalent Models service for Report 11.

Equivalent Models answers one operational question:

    When a product is unavailable, which other products has an Admin reviewed
    as suitable replacements?

Important business rules
------------------------
- A relationship is directional (``source_product_id`` -> ``alternative_product_id``).
  The reverse recommendation is never created automatically.
- Similar names, shared categories or co-purchase patterns are never treated as
  equivalence. Every relationship is an explicit Admin record.
- A relationship is only presented as ``Admin Verified`` after an Admin calls
  the verify action, which sets ``verified_by`` and ``verified_at``.
- The current product schema has no dedicated model column. Search therefore
  matches the product name, product ID, description, category and brand.
  The API returns ``model=None`` and ``model_display="Unknown"`` rather than
  inventing a model number.
- Price is ``product.SALES_PRICE``. A missing or non-positive price is shown as
  ``Unknown``. Stock comes from ``stock_serial`` for serial products and from
  ``stock_non_serial`` for every other product; missing stock rows are
  ``Unknown``, never zero.
- Equivalent models do not establish printer/ink compatibility. The existing
  Ink Dependency module remains the source of truth for that relationship.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from pymysql.err import IntegrityError

from .connection import get_connection

logger = logging.getLogger("equivalent_models")

EQUIVALENT_MODELS_MENU_ID = 34
EQUIVALENT_MODELS_MENU_TITLE = "Equivalent Models"
EQUIVALENT_MODELS_SLUGS = {
    "equivalent-models",
    "equivalent_models",
    "equivalent models",
    "/equivalent-models",
    "/menus/equivalent-models",
}

STATUS_NEEDS_REVIEW = "needs_review"
STATUS_VERIFIED = "verified"
STATUS_INACTIVE = "inactive"
RELATIONSHIP_STATUSES = {
    STATUS_NEEDS_REVIEW,
    STATUS_VERIFIED,
    STATUS_INACTIVE,
}

VERIFICATION_FILTER_VALUES = {
    "all",
    "active",
    "verified",
    "needs_review",
    "inactive",
}
STOCK_FILTER_VALUES = {"all", "in_stock", "out_of_stock", "unknown"}
SORT_FIELDS = {"last_updated", "product_name", "price", "stock", "status"}
SORT_ORDERS = {"asc", "desc"}

MAX_REASON_LENGTH = 2000
MAX_SOURCE_LENGTH = 200
MAX_SEARCH_LENGTH = 200
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

DISCLAIMER = (
    "Recommendations are directional and only appear after an Admin records "
    "them. Similar names or shared categories are not treated as equivalence."
)

COMPATIBILITY_NOTICE = (
    "An equivalent model does not automatically establish printer and ink "
    "compatibility. Use the Ink Dependency module for verified compatibility."
)

MODEL_NOTE = (
    "The current product catalog has no dedicated model column. Search matches "
    "the product name, product ID, description, category and brand, and the "
    "model field is shown as Unknown when the database does not store one."
)

CALCULATION_BASIS = (
    "Price is product.SALES_PRICE; a missing or non-positive value is Unknown. "
    "Stock is the sum of stock_serial.STOCK_QTY for serial products or "
    "stock_non_serial.STOCK_QTY for non-serial products. Products without stock "
    "rows are Unknown, not zero."
)

_TABLE_INITIALIZED = False

_PRODUCT_SELECT = """
    SELECT
        p.PRODUCT_ID AS product_id,
        p.PRODUCT_NAME AS product_name,
        p.DESCRIPTION AS description,
        p.CATEGORY_ID AS category_id,
        p.BRAND_ID AS brand_id,
        p.SALES_PRICE AS sales_price,
        p.UNIT AS unit,
        p.WARRANTY AS warranty,
        p.COLOR AS color,
        p.SERIAL AS serial,
        p.BARCODE_ID AS barcode_id,
        p.ACTIVE AS active,
        c.CATEGORY_NAME AS category_name,
        b.BRAND_NAME AS brand_name,
        ns.stock_qty AS non_serial_stock,
        ns.stock_rows AS non_serial_stock_rows,
        ns.stock_observed_at AS non_serial_observed_at,
        ss.stock_qty AS serial_stock,
        ss.stock_rows AS serial_stock_rows,
        ss.stock_observed_at AS serial_observed_at
    FROM product p
    LEFT JOIN category c ON c.CATEGORY_ID = p.CATEGORY_ID
    LEFT JOIN brand b ON b.BRAND_ID = p.BRAND_ID
    LEFT JOIN (
        SELECT
            PRODUCT_ID,
            SUM(STOCK_QTY) AS stock_qty,
            COUNT(*) AS stock_rows,
            MAX(TIMESTAMP) AS stock_observed_at
        FROM stock_non_serial
        GROUP BY PRODUCT_ID
    ) ns ON ns.PRODUCT_ID = p.PRODUCT_ID
    LEFT JOIN (
        SELECT
            PRODUCT_ID,
            SUM(STOCK_QTY) AS stock_qty,
            COUNT(*) AS stock_rows,
            MAX(TIMESTAMP) AS stock_observed_at
        FROM stock_serial
        GROUP BY PRODUCT_ID
    ) ss ON ss.PRODUCT_ID = p.PRODUCT_ID
"""

_RELATION_SELECT = """
    SELECT
        id,
        source_product_id,
        alternative_product_id,
        status,
        reason,
        source,
        verified_by,
        verified_at,
        created_by,
        updated_by,
        created_at,
        updated_at
    FROM product_equivalent_models
"""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_equivalent_models_table() -> None:
    """Create the application-owned relationship table when it is missing."""
    global _TABLE_INITIALIZED
    if _TABLE_INITIALIZED:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS product_equivalent_models (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    source_product_id VARCHAR(10) NOT NULL,
                    alternative_product_id VARCHAR(10) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'needs_review',
                    reason TEXT NOT NULL,
                    source VARCHAR(200) NOT NULL DEFAULT 'Admin entry',
                    verified_by VARCHAR(50) NULL,
                    verified_at DATETIME NULL,
                    created_by VARCHAR(50) DEFAULT 'admin',
                    updated_by VARCHAR(50) DEFAULT 'admin',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_equivalent_directional (source_product_id, alternative_product_id),
                    KEY idx_equivalent_source_status (source_product_id, status),
                    KEY idx_equivalent_alternative_status (alternative_product_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    _TABLE_INITIALIZED = True


# ---------------------------------------------------------------------------
# Permissions and metadata
# ---------------------------------------------------------------------------

def resolve_equivalent_models_menu_id() -> Optional[int]:
    """Return the sidebar menu ID for Equivalent Models (title/url match)."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM sidebar_menus
                    WHERE LOWER(title) LIKE '%equivalent model%'
                       OR LOWER(url) LIKE '%equivalent-model%'
                    ORDER BY id
                    LIMIT 1
                """)
                row = cur.fetchone()
                return int(row["id"]) if row and row.get("id") else EQUIVALENT_MODELS_MENU_ID
    except Exception:
        return EQUIVALENT_MODELS_MENU_ID


def _normalize_allowed_menus(allowed: Any) -> List[Any]:
    if allowed is None:
        return []
    if isinstance(allowed, (list, tuple, set)):
        return list(allowed)

    text = str(allowed).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            return list(parsed) if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def is_admin_user(user: Mapping[str, Any] | None) -> bool:
    """Return True only for the Admin role."""
    if not user:
        return False
    return str(user.get("role", "")).strip().lower() == "admin"


def can_view_equivalent_models(user: Mapping[str, Any] | None) -> bool:
    """Allow Admin users or users granted the Equivalent Models menu."""
    if not user:
        return False
    if is_admin_user(user):
        return True

    allowed = user.get("allowed_menus")
    if allowed is None:
        return False
    if isinstance(allowed, str) and allowed.strip() == "*":
        return True

    values = _normalize_allowed_menus(allowed)
    if not values:
        return False

    slugs = {str(value).strip().lower() for value in values if isinstance(value, str)}
    if any(slug in slugs for slug in EQUIVALENT_MODELS_SLUGS):
        return True

    allowed_ids = set()
    for value in values:
        try:
            text = str(value).strip()
            if text.isdigit():
                allowed_ids.add(int(text))
        except (TypeError, ValueError):
            continue

    if allowed_ids and resolve_equivalent_models_menu_id() in allowed_ids:
        return True

    return False


def get_equivalent_models_metadata() -> Dict[str, Any]:
    """Return static labels and disclaimers used by the page and API."""
    return {
        "menu_id": resolve_equivalent_models_menu_id(),
        "disclaimer": DISCLAIMER,
        "compatibility_notice": COMPATIBILITY_NOTICE,
        "model_note": MODEL_NOTE,
        "calculation_basis": CALCULATION_BASIS,
        "status_options": [
            {"value": "active", "label": "All active relationships"},
            {"value": "verified", "label": "Admin Verified"},
            {"value": "needs_review", "label": "Needs Review"},
            {"value": "inactive", "label": "Inactive"},
            {"value": "all", "label": "All statuses"},
        ],
        "stock_options": [
            {"value": "all", "label": "All stock states"},
            {"value": "in_stock", "label": "In stock"},
            {"value": "out_of_stock", "label": "Out of stock"},
            {"value": "unknown", "label": "Unknown stock"},
        ],
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: Optional[float]) -> str:
    if value is None:
        return "Unknown"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _format_money(value: Optional[float]) -> str:
    if value is None:
        return "Unknown"
    if float(value).is_integer():
        return f"BDT {int(value):,}"
    return f"BDT {float(value):,.2f}"


def _iso_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _display_date(value: Any) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, (datetime, date)):
        return value.strftime("%d %b %Y")
    text = str(value).strip()
    if not text:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y")
    except (TypeError, ValueError):
        return text


def _normalize_page(page: Any, page_size: Any) -> Tuple[int, int]:
    try:
        clean_page = max(1, int(page))
    except (TypeError, ValueError):
        clean_page = 1
    try:
        clean_size = int(page_size)
    except (TypeError, ValueError):
        clean_size = DEFAULT_PAGE_SIZE
    clean_size = max(1, min(MAX_PAGE_SIZE, clean_size))
    return clean_page, clean_size


def _stock_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    serialized = str(row.get("serial") or "").strip().upper() == "Y"
    if serialized:
        rows = int(row.get("serial_stock_rows") or 0)
        qty = _to_float(row.get("serial_stock"))
        observed_at = row.get("serial_observed_at")
        source = "serial"
    else:
        rows = int(row.get("non_serial_stock_rows") or 0)
        qty = _to_float(row.get("non_serial_stock"))
        observed_at = row.get("non_serial_observed_at")
        source = "non_serial"

    if rows <= 0:
        return {
            "known": False,
            "quantity": None,
            "status": "unknown",
            "source": "unknown",
            "observed_at": None,
        }

    quantity = qty if qty is not None else 0.0
    return {
        "known": True,
        "quantity": quantity,
        "status": "in_stock" if quantity > 0 else "out_of_stock",
        "source": source,
        "observed_at": _iso_datetime(observed_at),
    }


def _price_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    raw = _to_float(row.get("sales_price"))
    if raw is None or raw <= 0:
        return {"known": False, "value": None}
    return {"known": True, "value": raw}


def _product_payload(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None

    stock = _stock_from_row(row)
    price = _price_from_row(row)
    serialized = str(row.get("serial") or "").strip().upper() == "Y"

    missing = []
    if not price["known"]:
        missing.append("price")
    if not stock["known"]:
        missing.append("stock")
    if not _to_text(row.get("brand_name")):
        missing.append("brand")
    if not _to_text(row.get("category_name")):
        missing.append("category")
    if not _to_text(row.get("unit")):
        missing.append("unit")
    if not _to_text(row.get("warranty")):
        missing.append("warranty")
    if not _to_text(row.get("color")):
        missing.append("color")
    if not _to_text(row.get("description")):
        missing.append("description")

    return {
        "product_id": _to_text(row.get("product_id")),
        "product_name": _to_text(row.get("product_name")) or "Unknown product",
        "description": _to_text(row.get("description")),
        "model": None,
        "model_display": "Unknown",
        "category_id": _to_text(row.get("category_id")),
        "category_name": _to_text(row.get("category_name")) or "Unknown",
        "brand_id": _to_text(row.get("brand_id")),
        "brand_name": _to_text(row.get("brand_name")) or "Unknown",
        "unit": _to_text(row.get("unit")),
        "warranty": _to_text(row.get("warranty")),
        "color": _to_text(row.get("color")),
        "serialized": serialized,
        "serial_display": "Yes" if serialized else "No",
        "barcode_id": _to_text(row.get("barcode_id")),
        "active": _to_text(row.get("active")),
        "price_known": price["known"],
        "price": price["value"],
        "price_display": _format_money(price["value"]),
        "stock_known": stock["known"],
        "stock": stock["quantity"],
        "stock_display": _format_number(stock["quantity"]),
        "stock_status": stock["status"],
        "stock_source": stock["source"],
        "stock_observed_at": stock["observed_at"],
        "missing_fields": missing,
    }


def _fetch_products_by_ids(ids: Iterable[Any], conn=None) -> Dict[str, Dict[str, Any]]:
    clean_ids = [str(value).strip() for value in ids if str(value or "").strip()]
    clean_ids = list(dict.fromkeys(clean_ids))
    if not clean_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(clean_ids))

    def _query(c):
        with c.cursor() as cur:
            cur.execute(
                _PRODUCT_SELECT + f" WHERE p.PRODUCT_ID IN ({placeholders})",
                tuple(clean_ids),
            )
            return cur.fetchall()

    if conn is not None:
        rows = _query(conn)
    else:
        with get_connection() as c:
            rows = _query(c)
    return {str(row.get("product_id")): row for row in rows}


def get_product(product_id: str) -> Optional[Dict[str, Any]]:
    """Return one catalog product with actual price, stock and attributes."""
    pid = str(product_id or "").strip()
    if not pid:
        return None
    rows = _fetch_products_by_ids([pid])
    row = rows.get(pid)
    return _product_payload(row) if row else None


def get_equivalent_model_categories() -> List[Dict[str, Any]]:
    """Return categories that contain at least one product."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.CATEGORY_ID AS category_id,
                    c.CATEGORY_NAME AS category_name,
                    COUNT(p.PRODUCT_ID) AS product_count
                FROM category c
                JOIN product p ON p.CATEGORY_ID = c.CATEGORY_ID
                WHERE c.CATEGORY_NAME IS NOT NULL AND c.CATEGORY_NAME <> ''
                GROUP BY c.CATEGORY_ID, c.CATEGORY_NAME
                ORDER BY c.CATEGORY_NAME ASC
            """)
            rows = cur.fetchall()
    return [
        {
            "category_id": _to_text(row.get("category_id")),
            "category_name": _to_text(row.get("category_name")) or "Unknown",
            "product_count": int(row.get("product_count") or 0),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Product search
# ---------------------------------------------------------------------------

def search_products(
    query: str = "",
    category_id: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """Search products by name, ID, description, category or brand text."""
    q = str(query or "").strip()[:MAX_SEARCH_LENGTH]
    category = str(category_id or "").strip()
    clean_page, clean_size = _normalize_page(page, page_size)
    offset = (clean_page - 1) * clean_size

    if not q:
        return {
            "query": "",
            "page": clean_page,
            "page_size": clean_size,
            "total_records": 0,
            "total_pages": 0,
            "products": [],
            "search_fields": ["product_name", "product_id", "description", "category_name", "brand_name"],
            "model_note": MODEL_NOTE,
        }

    like = f"%{q}%"
    where = """
        (
            p.PRODUCT_NAME LIKE %s
            OR p.PRODUCT_ID LIKE %s
            OR p.DESCRIPTION LIKE %s
            OR c.CATEGORY_NAME LIKE %s
            OR b.BRAND_NAME LIKE %s
        )
    """
    params: List[Any] = [like, like, like, like, like]
    if category:
        where += " AND p.CATEGORY_ID = %s"
        params.append(category)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM product p
                LEFT JOIN category c ON c.CATEGORY_ID = p.CATEGORY_ID
                LEFT JOIN brand b ON b.BRAND_ID = p.BRAND_ID
                WHERE {where}
                """,
                tuple(params),
            )
            total = int((cur.fetchone() or {}).get("total") or 0)

            cur.execute(
                _PRODUCT_SELECT
                + f"""
                WHERE {where}
                ORDER BY
                    CASE
                        WHEN p.PRODUCT_ID = %s THEN 0
                        WHEN p.PRODUCT_NAME LIKE %s THEN 1
                        ELSE 2
                    END,
                    p.PRODUCT_NAME ASC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [q, f"{q}%", clean_size, offset]),
            )
            rows = cur.fetchall()

    total_pages = (total + clean_size - 1) // clean_size if total else 0
    return {
        "query": q,
        "page": clean_page,
        "page_size": clean_size,
        "total_records": total,
        "total_pages": total_pages,
        "products": [_product_payload(row) for row in rows],
        "search_fields": ["product_name", "product_id", "description", "category_name", "brand_name"],
        "model_note": MODEL_NOTE,
    }


# ---------------------------------------------------------------------------
# Relationship serialization
# ---------------------------------------------------------------------------

def _effective_status(row: Mapping[str, Any]) -> str:
    raw = str(row.get("status") or "").strip().lower()
    if raw == STATUS_INACTIVE:
        return STATUS_INACTIVE
    if raw == STATUS_VERIFIED and row.get("verified_at") and _to_text(row.get("verified_by")):
        return STATUS_VERIFIED
    if raw in (STATUS_VERIFIED, STATUS_NEEDS_REVIEW):
        return STATUS_NEEDS_REVIEW
    return STATUS_NEEDS_REVIEW


def _status_label(status: str) -> str:
    if status == STATUS_VERIFIED:
        return "Admin Verified"
    if status == STATUS_INACTIVE:
        return "Inactive"
    return "Needs Review"


def _relationship_payload(
    row: Mapping[str, Any],
    alternative_product: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    effective = _effective_status(row)
    return {
        "id": int(row.get("id") or 0),
        "source_product_id": _to_text(row.get("source_product_id")),
        "alternative_product_id": _to_text(row.get("alternative_product_id")),
        "status": effective,
        "status_raw": _to_text(row.get("status")),
        "status_label": _status_label(effective),
        "is_admin_verified": effective == STATUS_VERIFIED,
        "is_active_relationship": effective in (STATUS_VERIFIED, STATUS_NEEDS_REVIEW),
        "reason": _to_text(row.get("reason")),
        "source": _to_text(row.get("source")),
        "created_by": _to_text(row.get("created_by")),
        "updated_by": _to_text(row.get("updated_by")),
        "verified_by": _to_text(row.get("verified_by")),
        "verified_at": _iso_datetime(row.get("verified_at")),
        "verified_at_display": _display_date(row.get("verified_at")),
        "created_at": _iso_datetime(row.get("created_at")),
        "updated_at": _iso_datetime(row.get("updated_at")),
        "last_updated_display": _display_date(row.get("updated_at") or row.get("created_at")),
        "alternative_product": _product_payload(alternative_product) if alternative_product else None,
    }


def _serialize_relationship_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    alternative_rows = _fetch_products_by_ids([row.get("alternative_product_id")])
    alternative_row = alternative_rows.get(str(row.get("alternative_product_id") or ""))
    return _relationship_payload(row, alternative_row)


def _fetch_relationship(relationship_id: int, conn=None) -> Optional[Dict[str, Any]]:
    def _query(c):
        with c.cursor() as cur:
            cur.execute(_RELATION_SELECT + " WHERE id = %s", (relationship_id,))
            return cur.fetchone()

    if conn is not None:
        return _query(conn)
    init_equivalent_models_table()
    with get_connection() as c:
        return _query(c)


def _normalize_filters(
    verification_status: str = "active",
    category_id: str = "",
    stock_status: str = "all",
    search: str = "",
    sort_by: str = "last_updated",
    sort_order: str = "desc",
) -> Dict[str, str]:
    status_value = str(verification_status or "active").strip().lower()
    if status_value not in VERIFICATION_FILTER_VALUES:
        raise ValueError(
            "Invalid verification status. Allowed values: "
            + ", ".join(sorted(VERIFICATION_FILTER_VALUES))
        )

    stock_value = str(stock_status or "all").strip().lower()
    if stock_value not in STOCK_FILTER_VALUES:
        raise ValueError(
            "Invalid stock status. Allowed values: "
            + ", ".join(sorted(STOCK_FILTER_VALUES))
        )

    sort_value = str(sort_by or "last_updated").strip().lower()
    if sort_value not in SORT_FIELDS:
        raise ValueError("Invalid sort field. Allowed values: " + ", ".join(sorted(SORT_FIELDS)))

    order_value = str(sort_order or "desc").strip().lower()
    if order_value not in SORT_ORDERS:
        raise ValueError("Invalid sort order. Use 'asc' or 'desc'.")

    return {
        "verification_status": status_value,
        "category_id": str(category_id or "").strip(),
        "stock_status": stock_value,
        "search": str(search or "").strip()[:MAX_SEARCH_LENGTH],
        "sort_by": sort_value,
        "sort_order": order_value,
    }


def _matches_status(relationship: Mapping[str, Any], filter_value: str) -> bool:
    effective = relationship["status"]
    if filter_value == "all":
        return True
    if filter_value == "active":
        return effective in (STATUS_VERIFIED, STATUS_NEEDS_REVIEW)
    return effective == filter_value


def _sort_key(relationship: Mapping[str, Any], sort_by: str):
    product = relationship.get("alternative_product") or {}
    status_rank = {
        STATUS_VERIFIED: 0,
        STATUS_NEEDS_REVIEW: 1,
        STATUS_INACTIVE: 2,
    }.get(relationship.get("status"), 3)

    if sort_by == "product_name":
        return str(product.get("product_name") or "").lower()
    if sort_by == "price":
        value = product.get("price")
        return (value is None, value if value is not None else 0)
    if sort_by == "stock":
        value = product.get("stock")
        return (value is None, value if value is not None else 0)
    if sort_by == "status":
        return status_rank
    return str(relationship.get("updated_at") or "")


def list_alternative_relationships(
    source_product_id: str,
    verification_status: str = "active",
    category_id: str = "",
    stock_status: str = "all",
    search: str = "",
    sort_by: str = "last_updated",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Optional[Dict[str, Any]]:
    """Return directional alternatives recorded for one source product."""
    init_equivalent_models_table()
    source_id = str(source_product_id or "").strip()
    source_product = get_product(source_id)
    if not source_product:
        return None

    filters = _normalize_filters(
        verification_status=verification_status,
        category_id=category_id,
        stock_status=stock_status,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    clean_page, clean_size = _normalize_page(page, page_size)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _RELATION_SELECT
                + " WHERE source_product_id = %s ORDER BY updated_at DESC, id DESC",
                (source_id,),
            )
            rows = cur.fetchall()

    alternative_ids = [row.get("alternative_product_id") for row in rows]
    product_rows = _fetch_products_by_ids(alternative_ids)

    relationships = []
    summary = {
        "total_relationships": len(rows),
        "verified": 0,
        "needs_review": 0,
        "inactive": 0,
        "active_total": 0,
        "filtered_total": 0,
    }

    search_text = filters["search"].lower()
    for row in rows:
        alt_row = product_rows.get(str(row.get("alternative_product_id") or ""))
        alt_payload = _product_payload(alt_row) if alt_row else None
        relationship = _relationship_payload(row, alt_row)

        effective = relationship["status"]
        if effective == STATUS_VERIFIED:
            summary["verified"] += 1
        elif effective == STATUS_NEEDS_REVIEW:
            summary["needs_review"] += 1
        else:
            summary["inactive"] += 1
        if effective in (STATUS_VERIFIED, STATUS_NEEDS_REVIEW):
            summary["active_total"] += 1

        if not _matches_status(relationship, filters["verification_status"]):
            continue
        if filters["category_id"]:
            if not alt_payload or alt_payload.get("category_id") != filters["category_id"]:
                continue
        if filters["stock_status"] != "all":
            if not alt_payload or alt_payload.get("stock_status") != filters["stock_status"]:
                continue
        if search_text:
            haystack = " ".join(
                str(value or "")
                for value in [
                    (alt_payload or {}).get("product_name"),
                    (alt_payload or {}).get("product_id"),
                    (alt_payload or {}).get("category_name"),
                    (alt_payload or {}).get("brand_name"),
                    relationship.get("reason"),
                    relationship.get("source"),
                    relationship.get("status_label"),
                ]
            ).lower()
            if search_text not in haystack:
                continue

        relationships.append(relationship)

    reverse = filters["sort_order"] == "desc"
    if filters["sort_by"] in ("price", "stock"):
        known = [
            item for item in relationships
            if (item.get("alternative_product") or {}).get(filters["sort_by"]) is not None
        ]
        unknown = [
            item for item in relationships
            if (item.get("alternative_product") or {}).get(filters["sort_by"]) is None
        ]
        known.sort(key=lambda item: _sort_key(item, filters["sort_by"]), reverse=reverse)
        relationships = known + unknown
    else:
        relationships.sort(key=lambda item: _sort_key(item, filters["sort_by"]), reverse=reverse)
    summary["filtered_total"] = len(relationships)

    total_pages = (len(relationships) + clean_size - 1) // clean_size if relationships else 0
    start = (clean_page - 1) * clean_size
    end = start + clean_size
    page_rows = relationships[start:end]

    return {
        "source_product": source_product,
        "filters": filters,
        "summary": summary,
        "alternatives": page_rows,
        "pagination": {
            "page": clean_page,
            "page_size": clean_size,
            "total_records": len(relationships),
            "total_pages": total_pages,
            "has_next": clean_page < total_pages,
            "has_prev": clean_page > 1,
        },
        "disclaimer": DISCLAIMER,
        "compatibility_notice": COMPATIBILITY_NOTICE,
        "calculation_basis": CALCULATION_BASIS,
    }


# ---------------------------------------------------------------------------
# Detail comparison
# ---------------------------------------------------------------------------

def _comparison_value(product: Optional[Mapping[str, Any]], field: str) -> Tuple[str, bool]:
    if not product:
        return "Unknown", False
    value = product.get(field)
    if value is None or value == "":
        return "Unknown", False
    if isinstance(value, bool):
        return ("Yes" if value else "No"), True
    if isinstance(value, (int, float)):
        text = _format_number(float(value))
        return text, True
    return str(value), True


def _build_comparison(
    source: Optional[Mapping[str, Any]],
    alternative: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    fields = [
        ("product_id", "Product ID"),
        ("product_name", "Product name"),
        ("model_display", "Model"),
        ("category_name", "Category"),
        ("brand_name", "Brand"),
        ("unit", "Unit"),
        ("warranty", "Warranty"),
        ("color", "Color"),
        ("serial_display", "Serialized"),
        ("barcode_id", "Barcode"),
        ("price_display", "Current price"),
        ("stock_display", "Current stock"),
        ("description", "Description"),
    ]
    rows = []
    for field, label in fields:
        source_text, source_known = _comparison_value(source, field)
        alternative_text, alternative_known = _comparison_value(alternative, field)
        same = None
        if source_known and alternative_known:
            same = source_text.strip().lower() == alternative_text.strip().lower()
        rows.append({
            "field": field,
            "label": label,
            "source": source_text,
            "alternative": alternative_text,
            "source_known": source_known,
            "alternative_known": alternative_known,
            "same": same,
        })
    return rows


def get_relationship_detail(relationship_id: int) -> Optional[Dict[str, Any]]:
    """Return one relationship plus a database-backed comparison."""
    init_equivalent_models_table()
    row = _fetch_relationship(relationship_id)
    if not row:
        return None

    products = _fetch_products_by_ids([
        row.get("source_product_id"),
        row.get("alternative_product_id"),
    ])
    source_row = products.get(str(row.get("source_product_id") or ""))
    alternative_row = products.get(str(row.get("alternative_product_id") or ""))
    source_payload = _product_payload(source_row)
    alternative_payload = _product_payload(alternative_row)
    relationship = _relationship_payload(row, alternative_row)

    missing_products = []
    if not source_payload:
        missing_products.append("source_product")
    if not alternative_payload:
        missing_products.append("alternative_product")

    return {
        "relationship": relationship,
        "source_product": source_payload,
        "alternative_product": alternative_payload,
        "comparison": _build_comparison(source_payload, alternative_payload),
        "missing_products": missing_products,
        "disclaimer": DISCLAIMER,
        "compatibility_notice": COMPATIBILITY_NOTICE,
        "calculation_basis": CALCULATION_BASIS,
    }


# ---------------------------------------------------------------------------
# Admin writes
# ---------------------------------------------------------------------------

def _clean_product_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} product ID is required.")
    if len(text) > 10:
        raise ValueError(f"{label} product ID is too long. Product IDs are up to 10 characters.")
    return text


def _clean_reason(value: Any, required: bool = True) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError("A reason is required. Record why the alternative is suitable.")
        return None
    if len(text) > MAX_REASON_LENGTH:
        raise ValueError(f"Reason exceeds {MAX_REASON_LENGTH} characters.")
    return text


def _clean_source(value: Any, required: bool = True) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        if required:
            return "Admin entry"
        return None
    if len(text) > MAX_SOURCE_LENGTH:
        raise ValueError(f"Source exceeds {MAX_SOURCE_LENGTH} characters.")
    return text


def _clean_username(value: Any) -> str:
    return (str(value or "").strip() or "admin")[:50]


def _write_audit(user_id: Optional[int], action: str, details: str, ip_address: str = "") -> None:
    try:
        from .auth import audit_event
        audit_event(user_id, action, details[:255], ip_address[:64])
    except Exception:
        logger.exception("Could not write Equivalent Models audit event")


def _validate_products(source_id: str, alternative_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source_product = get_product(source_id)
    if not source_product:
        raise ValueError(f"Source product '{source_id}' was not found.")
    alternative_product = get_product(alternative_id)
    if not alternative_product:
        raise ValueError(f"Alternative product '{alternative_id}' was not found.")
    if source_id == alternative_id:
        raise ValueError("A product cannot be listed as its own equivalent.")
    return source_product, alternative_product


def add_equivalent_relationship(
    source_product_id: str,
    alternative_product_id: str,
    reason: str,
    source: str = "Admin entry",
    username: str = "admin",
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> Dict[str, Any]:
    """Create one directional relationship in Needs Review status.

    Re-adding a deactivated direction reactivates the same row; it never creates
    a duplicate. Verification is always a separate Admin action.
    """
    init_equivalent_models_table()
    source_id = _clean_product_id(source_product_id, "Source")
    alternative_id = _clean_product_id(alternative_product_id, "Alternative")
    clean_reason = _clean_reason(reason, required=True)
    clean_source = _clean_source(source, required=True)
    user_name = _clean_username(username)

    _validate_products(source_id, alternative_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _RELATION_SELECT
                + " WHERE source_product_id = %s AND alternative_product_id = %s LIMIT 1",
                (source_id, alternative_id),
            )
            existing = cur.fetchone()

            if existing:
                effective = _effective_status(existing)
                if effective != STATUS_INACTIVE:
                    raise ValueError(
                        f"Duplicate directional relationship already exists "
                        f"({source_id} -> {alternative_id})."
                    )
                cur.execute("""
                    UPDATE product_equivalent_models
                    SET status = %s,
                        reason = %s,
                        source = %s,
                        verified_by = NULL,
                        verified_at = NULL,
                        updated_by = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (STATUS_NEEDS_REVIEW, clean_reason, clean_source, user_name, existing["id"]))
                relationship_id = int(existing["id"])
                conn.commit()
                _write_audit(
                    user_id,
                    "EQUIVALENT_MODEL_REACTIVATE",
                    f"id={relationship_id} source={source_id} alternative={alternative_id} by={user_name}",
                    ip_address,
                )
                return {"id": relationship_id, "reactivated": True, "status": STATUS_NEEDS_REVIEW}

            try:
                cur.execute("""
                    INSERT INTO product_equivalent_models (
                        source_product_id, alternative_product_id, status, reason,
                        source, created_by, updated_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    source_id,
                    alternative_id,
                    STATUS_NEEDS_REVIEW,
                    clean_reason,
                    clean_source,
                    user_name,
                    user_name,
                ))
                relationship_id = int(cur.lastrowid)
                conn.commit()
            except IntegrityError as exc:
                conn.rollback()
                raise ValueError(
                    f"Duplicate directional relationship already exists "
                    f"({source_id} -> {alternative_id})."
                ) from exc

    _write_audit(
        user_id,
        "EQUIVALENT_MODEL_CREATE",
        f"id={relationship_id} source={source_id} alternative={alternative_id} by={user_name}",
        ip_address,
    )
    return {"id": relationship_id, "reactivated": False, "status": STATUS_NEEDS_REVIEW}


def update_equivalent_relationship(
    relationship_id: int,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    username: str = "admin",
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> Optional[Dict[str, Any]]:
    """Update the evidence fields on an existing relationship."""
    init_equivalent_models_table()
    existing = _fetch_relationship(int(relationship_id))
    if not existing:
        return None

    if reason is None and source is None:
        raise ValueError("Provide a reason or source to update.")

    updates = ["updated_by = %s", "updated_at = NOW()"]
    params: List[Any] = [_clean_username(username)]

    if reason is not None:
        clean_reason = _clean_reason(reason, required=True)
        updates.append("reason = %s")
        params.append(clean_reason)

    if source is not None:
        clean_source = _clean_source(source, required=True)
        updates.append("source = %s")
        params.append(clean_source)

    params.append(int(relationship_id))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE product_equivalent_models SET {', '.join(updates)} WHERE id = %s",
                tuple(params),
            )
            conn.commit()

    updated = _fetch_relationship(int(relationship_id))
    _write_audit(
        user_id,
        "EQUIVALENT_MODEL_UPDATE",
        f"id={relationship_id} source={updated.get('source_product_id')} "
        f"alternative={updated.get('alternative_product_id')} by={_clean_username(username)}",
        ip_address,
    )
    return _serialize_relationship_row(updated)


def verify_equivalent_relationship(
    relationship_id: int,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    username: str = "admin",
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> Optional[Dict[str, Any]]:
    """Explicitly verify a relationship. A reason must exist as evidence."""
    init_equivalent_models_table()
    existing = _fetch_relationship(int(relationship_id))
    if not existing:
        return None

    if _effective_status(existing) == STATUS_INACTIVE:
        raise ValueError("Inactive relationships cannot be verified. Add the direction again first.")

    final_reason = _clean_reason(reason, required=False) if reason is not None else _to_text(existing.get("reason"))
    if not final_reason:
        raise ValueError("A reason is required before an Admin can verify this relationship.")

    user_name = _clean_username(username)
    updates = [
        "status = %s",
        "reason = %s",
        "verified_by = %s",
        "verified_at = NOW()",
        "updated_by = %s",
        "updated_at = NOW()",
    ]
    params: List[Any] = [STATUS_VERIFIED, final_reason, user_name, user_name]

    if source is not None:
        clean_source = _clean_source(source, required=True)
        updates.append("source = %s")
        params.append(clean_source)

    params.append(int(relationship_id))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE product_equivalent_models SET {', '.join(updates)} WHERE id = %s",
                tuple(params),
            )
            conn.commit()

    updated = _fetch_relationship(int(relationship_id))
    _write_audit(
        user_id,
        "EQUIVALENT_MODEL_VERIFY",
        f"id={relationship_id} source={updated.get('source_product_id')} "
        f"alternative={updated.get('alternative_product_id')} by={user_name}",
        ip_address,
    )
    return _serialize_relationship_row(updated)


def deactivate_equivalent_relationship(
    relationship_id: int,
    username: str = "admin",
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> Optional[Dict[str, Any]]:
    """Deactivate a relationship without deleting its evidence."""
    init_equivalent_models_table()
    existing = _fetch_relationship(int(relationship_id))
    if not existing:
        return None

    if _effective_status(existing) == STATUS_INACTIVE:
        return _serialize_relationship_row(existing)

    user_name = _clean_username(username)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE product_equivalent_models
                SET status = %s, updated_by = %s, updated_at = NOW()
                WHERE id = %s
            """, (STATUS_INACTIVE, user_name, int(relationship_id)))
            conn.commit()

    updated = _fetch_relationship(int(relationship_id))
    _write_audit(
        user_id,
        "EQUIVALENT_MODEL_DEACTIVATE",
        f"id={relationship_id} source={updated.get('source_product_id')} "
        f"alternative={updated.get('alternative_product_id')} by={user_name}",
        ip_address,
    )
    return _serialize_relationship_row(updated)
