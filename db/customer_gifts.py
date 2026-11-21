"""Customer Gifts service — Report 09: Customer Gifts.

Sales-based customer suggestions for gifts. Admin makes the final decision
and records gifts actually given. Customer and sales data are never modified
by this module; gift records live in the application-owned ``customer_gifts``
table linked to the customer in ``cs``.

Sales calculation basis (same as the rest of the application):
- Total purchase amount = ``SUM(sales.NET_SALES)`` over one row per invoice
  (the basis used by ``db/analytics.get_top_customers`` and
  ``db/customer_kyc.get_customer_kyc_detail``).
- Orders = ``COUNT(DISTINCT sales.SALES_ID)``.
- Purchase days = ``COUNT(DISTINCT DATE(sales.SALES_DATE))``.

``sales`` rows are collapsed to one row per ``(CS_ID, SALES_ID)`` invoice before
aggregation, and invoice lines in ``sales_details`` are never joined. Duplicate
invoice lines therefore cannot inflate order counts or spending.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from .connection import get_connection
from .customer_kyc import validate_and_clean_phone

logger = logging.getLogger("customer_gifts")

# Menu ID for Customer Gifts in sidebar_menus (resolved dynamically by title/url)
CUSTOMER_GIFTS_MENU_ID = 32
CUSTOMER_GIFTS_SLUGS = {
    "customer-gifts",
    "customer_gifts",
    "customer gifts",
    "/menus/customer-gifts",
    "/customer-gifts",
}
DEFAULT_NEW_CUSTOMER_DAYS = 90

CALCULATION_BASIS = (
    "Total purchase amount = SUM(sales.NET_SALES), orders = "
    "COUNT(DISTINCT sales.SALES_ID), and purchase days = "
    "COUNT(DISTINCT DATE(sales.SALES_DATE)) after collapsing sales rows to one "
    "row per (CS_ID, SALES_ID) invoice. This follows the Top Customers KPI in "
    "db/analytics.py and db/customer_kyc.get_customer_kyc_detail. Invoice lines "
    "(sales_details) are never joined, so duplicate invoice lines cannot inflate "
    "counts or spending."
)

_SORT_KEYS = {
    "total_purchase_amount": lambda r: r["total_purchase_amount"],
    "order_count": lambda r: r["order_count"],
    "distinct_purchase_days": lambda r: r["distinct_purchase_days"],
    "customer_name": lambda r: r["customer_name"].lower(),
    "first_purchase_date": lambda r: r["first_purchase_date"] or "",
    "last_purchase_date": lambda r: r["last_purchase_date"] or "",
}


def resolve_customer_gifts_menu_id() -> Optional[int]:
    """Return the sidebar menu ID for the Customer Gifts menu (dynamic by title/url)."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM sidebar_menus
                    WHERE LOWER(title) LIKE '%customer gifts%' OR LOWER(url) LIKE '%customer-gifts%'
                    ORDER BY id LIMIT 1
                """)
                row = cur.fetchone()
                return int(row["id"]) if row and row.get("id") else CUSTOMER_GIFTS_MENU_ID
    except Exception:
        return CUSTOMER_GIFTS_MENU_ID


def _normalize_allowed_menus(allowed: Any) -> List[Any]:
    """Normalize session menu grants (list, JSON array or CSV string) into a list."""
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


def can_view_customer_gifts(user: Mapping[str, Any] | None) -> bool:
    """Check if the user is authorized to view Customer Gifts (Admin or menu permission)."""
    if not user:
        return False

    role = str(user.get("role", "")).strip().lower()
    if role == "admin":
        return True

    allowed = user.get("allowed_menus")
    if allowed is None:
        return False
    if isinstance(allowed, str) and allowed.strip() == "*":
        return True

    values = _normalize_allowed_menus(allowed)
    if not values:
        return False

    slugs = {str(x).strip().lower() for x in values if isinstance(x, str)}
    if any(slug in slugs for slug in CUSTOMER_GIFTS_SLUGS):
        return True

    allowed_ids = set()
    for value in values:
        try:
            text = str(value).strip()
            if text.isdigit():
                allowed_ids.add(int(text))
        except (TypeError, ValueError):
            continue

    if allowed_ids and resolve_customer_gifts_menu_id() in allowed_ids:
        return True

    return False


def is_admin_user(user: Mapping[str, Any] | None) -> bool:
    """Check if the user has the Admin role."""
    if not user:
        return False
    return str(user.get("role", "")).strip().lower() == "admin"


def get_customer_sales_summary(
    customer_ids: Optional[List[str]] = None,
    conn=None,
) -> Dict[str, Dict[str, Any]]:
    """Fetch per-customer sales aggregates from the ``sales`` table.

    Returns a mapping of customer_id -> {customer_id, customer_name, phone,
    first_purchase_date, last_purchase_date, order_count,
    distinct_purchase_days, total_spend} using the project's existing sales
    calculation (see CALCULATION_BASIS).
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            where = ""
            params: List[Any] = []
            if customer_ids:
                ids = [str(i).strip() for i in customer_ids if str(i).strip()]
                if not ids:
                    return {}
                placeholders = ", ".join(["%s"] * len(ids))
                where = f"AND t.CS_ID IN ({placeholders})"
                params = ids

            cur.execute(f"""
                SELECT
                    t.CS_ID AS customer_id,
                    cs.CS_NAME AS customer_name,
                    COALESCE(cs.MOBILE, '') AS mobile,
                    COALESCE(cs.PHONE, '') AS phone,
                    MIN(t.SALE_DATE) AS first_purchase_date,
                    MAX(t.SALE_DATE) AS last_purchase_date,
                    COUNT(DISTINCT t.SALES_ID) AS order_count,
                    COUNT(DISTINCT t.SALE_DATE) AS distinct_purchase_days,
                    ROUND(SUM(t.NET_SALES), 2) AS total_spend
                FROM (
                    SELECT CS_ID, SALES_ID,
                           MIN(DATE(SALES_DATE)) AS SALE_DATE,
                           MAX(COALESCE(NET_SALES, 0)) AS NET_SALES
                    FROM sales
                    WHERE CS_ID IS NOT NULL AND CS_ID != ''
                      AND SALES_ID IS NOT NULL AND SALES_ID != ''
                    GROUP BY CS_ID, SALES_ID
                ) t
                JOIN cs ON cs.CS_ID = t.CS_ID
                WHERE 1=1 {where}
                GROUP BY t.CS_ID, cs.CS_NAME, cs.MOBILE, cs.PHONE
            """, params)
            rows = cur.fetchall()

        stats: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            cid = str(r["customer_id"]).strip()
            mobile = str(r.get("mobile") or "").strip()
            phone = str(r.get("phone") or "").strip()
            disp_phone = mobile if mobile else phone
            stats[cid] = {
                "customer_id": cid,
                "customer_name": str(r.get("customer_name") or "").strip(),
                "phone": disp_phone,
                "first_purchase_date": _as_date(r.get("first_purchase_date")),
                "last_purchase_date": _as_date(r.get("last_purchase_date")),
                "order_count": int(r.get("order_count") or 0),
                "distinct_purchase_days": int(r.get("distinct_purchase_days") or 0),
                "total_spend": float(r.get("total_spend") or 0.0),
            }
        return stats
    finally:
        if close_conn and conn:
            conn.close()


def _as_date(value: Any) -> Optional[date]:
    """Normalize a DB value to a plain ``date`` (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_date_param(value: Any, name: str) -> Optional[date]:
    """Parse an optional ISO date parameter; raises ValueError on bad input."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"Invalid {name} '{value}'. Must be in YYYY-MM-DD format.")


def _parse_float_param(value: Any, name: str) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name} '{value}'. Must be a number.")


def _parse_int_param(value: Any, name: str) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name} '{value}'. Must be an integer.")


def _suggestion_facts(row: Mapping[str, Any]) -> List[str]:
    """Build the fact list supporting a suggestion (no invented scores)."""
    facts = [f"First recorded purchase: {row.get('first_purchase_date') or 'No recorded purchase'}"]
    if row.get("last_purchase_date"):
        facts.append(f"Last recorded purchase: {row['last_purchase_date']}")
    facts.append(f"Orders placed: {row['order_count']}")
    facts.append(f"Purchased on {row['distinct_purchase_days']} distinct day(s)")
    facts.append(f"Total purchase amount: {float(row['total_purchase_amount'] or 0):.2f}")
    if row.get("phone"):
        facts.append(f"Phone: {row['phone']}")
    return facts


def _build_suggestion_row(
    customer_id: str,
    name: str,
    phone: str,
    first: date,
    last: Optional[date],
    order_count: int,
    distinct_days: int,
    total_spend: float,
) -> Dict[str, Any]:
    row = {
        "customer_id": customer_id,
        "customer_name": name,
        "phone": phone,
        "first_purchase_date": first.isoformat() if first else None,
        "last_purchase_date": last.isoformat() if last else None,
        "total_purchase_amount": round(float(total_spend or 0.0), 2),
        "order_count": int(order_count or 0),
        "distinct_purchase_days": int(distinct_days or 0),
    }
    row["facts"] = _suggestion_facts(row)
    return row


def _sorted_rows(rows: List[Dict[str, Any]], sort_by: str, sort_order: str) -> List[Dict[str, Any]]:
    sort_by = (sort_by or "total_purchase_amount").strip().lower() or "total_purchase_amount"
    if sort_by not in _SORT_KEYS:
        sort_by = "total_purchase_amount"
    reverse = (sort_order or "desc").strip().lower() != "asc"
    primary = _SORT_KEYS[sort_by]
    return sorted(rows, key=lambda r: (primary(r), r["order_count"], r["customer_id"]), reverse=reverse)


def build_customer_gift_suggestions(
    sales_stats: Mapping[str, Mapping[str, Any]],
    *,
    new_customer_days: int = DEFAULT_NEW_CUSTOMER_DAYS,
    reference_date: Optional[date] = None,
    search: Optional[str] = None,
    group: Optional[str] = None,
    first_purchase_from: Optional[date] = None,
    first_purchase_to: Optional[date] = None,
    min_total_amount: Optional[float] = None,
    min_order_count: Optional[int] = None,
    sort_by: str = "total_purchase_amount",
    sort_order: str = "desc",
) -> Dict[str, List[Dict[str, Any]]]:
    """Classify sales stats into New Customers and Repeat Customers suggestion groups.

    Pure function — works on any mapping of per-customer sales stats with the
    shape returned by :func:`get_customer_sales_summary`.

    Rules:
    - New Customers: first recorded purchase within the last ``new_customer_days`` days.
    - Repeat Customers: first recorded purchase earlier than that window AND
      purchased on at least two different days.
    - Customers without a recorded purchase are excluded.
    - Groups are disjoint: customers with an older first purchase are repeat
      candidates; customers with a recent first purchase are new.
    """
    today = reference_date or date.today()
    days = int(new_customer_days)
    if days < 1:
        raise ValueError("new_customer_days must be at least 1.")
    threshold = today - timedelta(days=days)

    q = str(search or "").strip().lower()
    group = (group or "").strip().lower() or None
    if group not in (None, "all", "new", "repeat"):
        raise ValueError(f"Invalid group '{group}'. Must be one of: all, new, repeat.")

    new_rows: List[Dict[str, Any]] = []
    repeat_rows: List[Dict[str, Any]] = []

    for cid in sorted(sales_stats or {}):
        stats = sales_stats[cid]
        first = stats.get("first_purchase_date")
        if first is None or first > today:
            # No recorded purchase (or only future-dated rows): not a suggestion.
            continue
        distinct_days = int(stats.get("distinct_purchase_days") or 0)
        total = float(stats.get("total_spend") or 0.0)
        orders = int(stats.get("order_count") or 0)
        name = str(stats.get("customer_name") or "").strip()
        phone = str(stats.get("phone") or "").strip()
        last = stats.get("last_purchase_date")

        is_new = first >= threshold
        is_repeat = (not is_new) and distinct_days >= 2
        if not is_new and not is_repeat:
            continue
        if group == "new" and not is_new:
            continue
        if group == "repeat" and not is_repeat:
            continue

        row = _build_suggestion_row(cid, name, phone, first, last, orders, distinct_days, total)

        if q and q not in (row["customer_id"].lower() + row["customer_name"].lower() + row["phone"].lower()):
            continue
        if first_purchase_from and first < first_purchase_from:
            continue
        if first_purchase_to and first > first_purchase_to:
            continue
        if min_total_amount is not None and row["total_purchase_amount"] < min_total_amount:
            continue
        if min_order_count is not None and row["order_count"] < min_order_count:
            continue

        if is_new:
            new_rows.append(row)
        else:
            repeat_rows.append(row)

    return {
        "new": _sorted_rows(new_rows, sort_by, sort_order),
        "repeat": _sorted_rows(repeat_rows, sort_by, sort_order),
    }


def _paginate(rows: List[Dict[str, Any]], page: int, page_size: int) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 25)))
    offset = (page - 1) * page_size
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {
        "rows": rows[offset:offset + page_size],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_records": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def _group_date_range(rows: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Date range of the figures for a group of suggestions (labelled on the page)."""
    if not rows:
        return {"first_purchase_from": None, "first_purchase_to": None}
    return {
        "first_purchase_from": min(r["first_purchase_date"] for r in rows),
        "first_purchase_to": max(r["first_purchase_date"] for r in rows),
    }


def get_customer_gifts_suggestions(
    search: Optional[str] = None,
    group: str = "all",
    first_purchase_from: Optional[str] = None,
    first_purchase_to: Optional[str] = None,
    min_total_amount: Optional[str] = None,
    min_order_count: Optional[int] = None,
    new_customer_days: int = DEFAULT_NEW_CUSTOMER_DAYS,
    sort_by: str = "total_purchase_amount",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 25,
    reference_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Return New Customers and Repeat Customers suggestion groups with filters."""
    fp_from = _parse_date_param(first_purchase_from, "first_purchase_from")
    fp_to = _parse_date_param(first_purchase_to, "first_purchase_to")
    if fp_from and fp_to and fp_from > fp_to:
        raise ValueError("first_purchase_from cannot be after first_purchase_to.")
    min_amount = _parse_float_param(min_total_amount, "min_total_amount")
    min_orders = _parse_int_param(min_order_count, "min_order_count")
    days = _parse_int_param(new_customer_days, "new_customer_days")
    if days is None:
        days = DEFAULT_NEW_CUSTOMER_DAYS
    if days < 1:
        raise ValueError("new_customer_days must be at least 1.")
    if (sort_by or "").strip().lower() not in _SORT_KEYS and (sort_by or "").strip():
        raise ValueError(f"Invalid sort_by '{sort_by}'. Must be one of {sorted(_SORT_KEYS)}.")

    stats = get_customer_sales_summary()
    groups = build_customer_gift_suggestions(
        stats,
        new_customer_days=days,
        reference_date=reference_date,
        search=search,
        group=group,
        first_purchase_from=fp_from,
        first_purchase_to=fp_to,
        min_total_amount=min_amount,
        min_order_count=min_orders,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    new_rows = groups["new"]
    repeat_rows = groups["repeat"]

    return {
        "success": True,
        "calculation_basis": CALCULATION_BASIS,
        "filters": {
            "search": str(search or "").strip(),
            "group": (group or "all").strip().lower(),
            "first_purchase_from": fp_from.isoformat() if fp_from else "",
            "first_purchase_to": fp_to.isoformat() if fp_to else "",
            "min_total_amount": min_amount,
            "min_order_count": min_orders,
            "new_customer_days": days,
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
        "date_range": {
            "new_customers": _group_date_range(new_rows),
            "repeat_customers": _group_date_range(repeat_rows),
        },
        "summary": {
            "new_customers_total": len(new_rows),
            "repeat_customers_total": len(repeat_rows),
            "total_suggestions": len(new_rows) + len(repeat_rows),
        },
        "new_customers": _paginate(new_rows, page, page_size),
        "repeat_customers": _paginate(repeat_rows, page, page_size),
    }


def _gift_record_row(r: Mapping[str, Any]) -> Dict[str, Any]:
    created = r.get("created_at")
    gift_date = r.get("gift_date")
    return {
        "id": int(r.get("id") or 0),
        "customer_id": str(r.get("customer_id") or "").strip(),
        "customer_name": str(r.get("customer_name") or "").strip(),
        "phone": str(r.get("phone") or "").strip(),
        "gift_name": str(r.get("gift_name") or "").strip(),
        "gift_date": gift_date.isoformat() if hasattr(gift_date, "isoformat") else (str(gift_date) if gift_date else None),
        "note": str(r.get("note") or "").strip(),
        "recorded_by_user_id": int(r.get("recorded_by_user_id") or 0),
        "recorded_by_username": str(r.get("recorded_by_username") or "").strip(),
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created, "strftime") else (str(created) if created else None),
    }


def get_customer_gift_details(customer_id: str, conn=None) -> Optional[Dict[str, Any]]:
    """Return customer details (purchase summary + facts) and previous gifts."""
    cid = str(customer_id or "").strip()
    if not cid:
        return None

    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT CS_ID, CS_NAME, ADDRESS, MOBILE, PHONE
                FROM cs
                WHERE CS_ID = %s
                LIMIT 1
            """, (cid,))
            cs_row = cur.fetchone()
            if not cs_row:
                return None

            cur.execute("""
                SELECT id, customer_id, gift_name, gift_date, note, recorded_by_user_id,
                       recorded_by_username, created_at
                FROM customer_gifts
                WHERE customer_id = %s
                ORDER BY created_at DESC, id DESC
            """, (cid,))
            gift_rows = cur.fetchall()

        summary = get_customer_sales_summary([cid], conn=conn)
        stats = summary.get(cid, {})
        first = stats.get("first_purchase_date")
        last = stats.get("last_purchase_date")
        order_count = int(stats.get("order_count") or 0)
        distinct_days = int(stats.get("distinct_purchase_days") or 0)
        total_spend = float(stats.get("total_spend") or 0.0)

        disp_phone = str(stats.get("phone") or "").strip()
        if not disp_phone:
            disp_phone, _, _ = validate_and_clean_phone(cs_row.get("MOBILE"))
            if not disp_phone:
                disp_phone, _, _ = validate_and_clean_phone(cs_row.get("PHONE"))

        facts_row = {
            "customer_id": cid,
            "customer_name": str(cs_row.get("CS_NAME") or "").strip(),
            "phone": disp_phone,
            "first_purchase_date": first.isoformat() if first else None,
            "last_purchase_date": last.isoformat() if last else None,
            "order_count": order_count,
            "distinct_purchase_days": distinct_days,
            "total_purchase_amount": total_spend,
        }
        gifts = [_gift_record_row(r) for r in gift_rows]

        return {
            "success": True,
            "customer": {
                "customer_id": cid,
                "customer_name": str(cs_row.get("CS_NAME") or "").strip(),
                "phone": disp_phone,
                "address": str(cs_row.get("ADDRESS") or "").strip(),
                "purchase_summary": {
                    "first_purchase_date": facts_row["first_purchase_date"],
                    "last_purchase_date": facts_row["last_purchase_date"],
                    "total_purchase_amount": round(total_spend, 2),
                    "order_count": order_count,
                    "distinct_purchase_days": distinct_days,
                    "has_sales_history": order_count > 0,
                },
                "purchase_facts": _suggestion_facts(facts_row),
            },
            "gifts": gifts,
            "gift_count": len(gifts),
        }
    finally:
        if close_conn and conn:
            conn.close()


def get_customer_gifts_list(
    search: Optional[str] = None,
    customer_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
) -> Dict[str, Any]:
    """List recorded customer gifts for the Gift History tab (paginated, searchable)."""
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 25)))
    offset = (page - 1) * page_size

    with get_connection() as conn:
        with conn.cursor() as cur:
            where = ["1=1"]
            params: List[Any] = []

            cid = str(customer_id or "").strip()
            if cid:
                where.append("g.customer_id = %s")
                params.append(cid)

            q = str(search or "").strip()
            if q:
                needle = "%" + q + "%"
                where.append(
                    "(c.CS_NAME LIKE %s OR g.customer_id LIKE %s "
                    "OR COALESCE(c.MOBILE, '') LIKE %s OR COALESCE(c.PHONE, '') LIKE %s "
                    "OR g.gift_name LIKE %s OR COALESCE(g.note, '') LIKE %s)"
                )
                params.extend([needle] * 6)

            where_sql = " AND ".join(where)

            cur.execute(f"""
                SELECT g.id, g.customer_id, c.CS_NAME AS customer_name,
                       COALESCE(NULLIF(TRIM(c.MOBILE), ''), TRIM(c.PHONE), '') AS phone,
                       g.gift_name, g.gift_date, g.note, g.recorded_by_user_id,
                       g.recorded_by_username, g.created_at
                FROM customer_gifts g
                LEFT JOIN cs c ON c.CS_ID = g.customer_id
                WHERE {where_sql}
                ORDER BY g.created_at DESC, g.id DESC
                LIMIT %s OFFSET %s
            """, params + [page_size, offset])
            rows = cur.fetchall()

            cur.execute(f"""
                SELECT COUNT(*) AS cnt
                FROM customer_gifts g
                LEFT JOIN cs c ON c.CS_ID = g.customer_id
                WHERE {where_sql}
            """, params)
            row = cur.fetchone()
            total = int(row["cnt"] or 0) if row else 0

            gifts = [_gift_record_row(r) for r in rows]
            total_pages = max(1, (total + page_size - 1) // page_size) if total else 1

            return {
                "success": True,
                "gifts": gifts,
                "gift_count": len(gifts),
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_records": total,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                },
            }


def record_customer_gift(
    customer_id: str,
    gift_name: str,
    gift_date: Optional[str] = None,
    note: Optional[str] = None,
    user: Optional[Mapping[str, Any]] = None,
    conn=None,
) -> Dict[str, Any]:
    """Admin-only: record a gift given to a customer.

    Validates the customer exists, requires a non-empty gift name, parses the
    gift date (defaults to today), records the Admin user and creation time.
    """
    if not is_admin_user(user):
        raise PermissionError("Only Admin users are permitted to record customer gifts.")

    cid = str(customer_id or "").strip()
    if not cid:
        raise ValueError("Customer ID is required.")

    name = str(gift_name or "").strip()
    if not name:
        raise ValueError("Gift name is required.")
    if len(name) > 200:
        raise ValueError("Gift name exceeds 200 characters limit.")

    if gift_date not in (None, ""):
        try:
            g_date = date.fromisoformat(str(gift_date).strip())
        except ValueError:
            raise ValueError("Gift date must be in YYYY-MM-DD format.")
    else:
        g_date = date.today()

    note_text = str(note or "").strip() or None
    if note_text and len(note_text) > 500:
        raise ValueError("Note exceeds 500 characters limit.")

    user_id = user.get("id") if user else None
    username = str(user.get("username") or "admin") if user else "admin"

    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT CS_ID FROM cs WHERE CS_ID = %s LIMIT 1", (cid,))
            if not cur.fetchone():
                raise ValueError(f"Customer '{cid}' does not exist.")

            cur.execute("""
                INSERT INTO customer_gifts (customer_id, gift_name, gift_date, note,
                                             recorded_by_user_id, recorded_by_username)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (cid, name, g_date, note_text, int(user_id) if user_id else 1, username))
            gift_id = cur.lastrowid
        conn.commit()

        try:
            from .auth import audit_event
            audit_event(
                int(user_id) if user_id else 1,
                "CUSTOMER_GIFT_RECORD",
                f"Gift #{gift_id} '{name}' recorded for customer '{cid}' by {username}",
            )
        except Exception:
            logger.warning("Could not record audit event for customer gift")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, customer_id, gift_name, gift_date, note, recorded_by_user_id,
                       recorded_by_username, created_at
                FROM customer_gifts WHERE id = %s
            """, (gift_id,))
            gift = _gift_record_row(cur.fetchone() or {})

        return {
            "success": True,
            "status_code": 201,
            "message": "Customer gift recorded successfully.",
            "gift": gift,
        }
    finally:
        if close_conn and conn:
            conn.close()
