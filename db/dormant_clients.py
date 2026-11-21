"""Dormant Clients analytics and cadence calculations.

Report 05: Dormant Clients
Identifies clients who used to purchase regularly but are now overdue
relative to their individual purchase interval.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .connection import get_connection

logger = logging.getLogger("dormant_clients")

# Menu ID for Dormant Clients in sidebar_menus
DORMANT_CLIENTS_MENU_ID = 17
DORMANT_CLIENTS_SLUGS = {
    "dormant-clients",
    "dormant_clients",
    "dormant clients",
    "/dormant-clients",
}


def validate_and_clean_phone(raw_phone: Any) -> Tuple[str, str, bool]:
    """Validate and clean customer phone number.

    Returns:
        (display_phone, dialable_phone, is_valid)
    """
    if raw_phone is None:
        return ("", "", False)
    raw = str(raw_phone).strip()
    if not raw or raw == "0":
        return ("", "", False)

    # Strip whitespace, hyphens, parens, dots
    cleaned = re.sub(r"[\s\-\(\)\.]+", "", raw)
    if not cleaned or cleaned == "0":
        return ("", "", False)

    # Strip country code prefix (+88 or 88)
    digits_no_cc = re.sub(r"^\+?88", "", cleaned)

    # Standard Bangladesh mobile: 11 digits starting with 01
    if re.match(r"^01[3-9]\d{8}$", digits_no_cc):
        return (raw, digits_no_cc, True)

    # Generic phone check: 7 to 15 digits
    if re.match(r"^\+?\d{7,15}$", cleaned) and cleaned != "0":
        return (raw, cleaned, True)

    return (raw, "", False)


def can_view_dormant_clients(user: Mapping[str, Any] | None) -> bool:
    """Check if the user is authorized to view the Dormant Clients module.

    Admins and users with 'dormant-clients' or menu ID 17 grant are permitted.
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
        # Check slugs
        slugs = {str(x).strip().lower() for x in allowed if isinstance(x, str)}
        if any(slug in slugs for slug in DORMANT_CLIENTS_SLUGS):
            return True

        # Check numeric ID
        try:
            allowed_ids = {int(x) for x in allowed if str(x).isdigit() or isinstance(x, int)}
            if DORMANT_CLIENTS_MENU_ID in allowed_ids:
                return True
        except (ValueError, TypeError):
            pass

    return False


def calculate_customer_cadence(
    purchase_dates: List[date],
    reference_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Calculate purchase cadence and dormancy status for a single customer.

    Rules:
    1. Only consider distinct purchase days <= reference_date (ignore future/null dates).
    2. Multiple invoices on the same calendar day count as 1 distinct purchase day.
    3. Take at most the latest 6 distinct purchase days.
    4. If distinct purchase days < 3 -> Insufficient data.
    5. If >= 3 distinct purchase days:
       Avg interval = (latest_date - earliest_used_date) / (k - 1)
       Dormancy threshold = Avg interval * 1.5
       Days elapsed since last purchase = (reference_date - latest_date).days
       If days elapsed > threshold -> Dormant (strict inequality)
       Otherwise -> Active
    6. Full precision during calculation; round for display.
    """
    if reference_date is None:
        reference_date = date.today()

    # Filter valid unique dates on or before reference_date
    valid_dates = sorted(list({d for d in purchase_dates if d and d <= reference_date}))
    distinct_count = len(valid_dates)

    if distinct_count == 0:
        return {
            "distinct_days_count": 0,
            "used_dates": [],
            "intervals": [],
            "last_purchase_date": None,
            "days_since_last": None,
            "avg_interval_days": None,
            "threshold_days": None,
            "days_overdue": None,
            "days_past_threshold": None,
            "status": "insufficient_data",
            "status_label": "Insufficient data",
        }

    # Latest up to 6 distinct days
    used_dates = valid_dates[-6:]
    last_date = used_dates[-1]
    days_since_last = (reference_date - last_date).days

    if len(used_dates) < 3:
        return {
            "distinct_days_count": distinct_count,
            "used_dates": [d.isoformat() for d in used_dates],
            "intervals": [],
            "last_purchase_date": last_date.isoformat(),
            "days_since_last": days_since_last,
            "avg_interval_days": None,
            "threshold_days": None,
            "days_overdue": None,
            "days_past_threshold": None,
            "status": "insufficient_data",
            "status_label": "Insufficient data",
        }

    # Build individual intervals between the used dates
    intervals = []
    for i in range(len(used_dates) - 1):
        diff = (used_dates[i + 1] - used_dates[i]).days
        intervals.append({
            "from_date": used_dates[i].isoformat(),
            "to_date": used_dates[i + 1].isoformat(),
            "days": diff,
        })

    # Mathematical identity: sum of consecutive intervals equals (last - first)
    total_interval_days = (used_dates[-1] - used_dates[0]).days
    interval_count = len(used_dates) - 1
    avg_interval = total_interval_days / interval_count
    threshold = avg_interval * 1.5

    # Strict inequality: days_since_last > threshold
    is_dormant = days_since_last > threshold
    status = "dormant" if is_dormant else "active"
    status_label = "Dormant" if is_dormant else "Active"

    # Days overdue: compared to normal cadence
    days_overdue = round(max(0.0, float(days_since_last - avg_interval)), 1)
    days_past_threshold = round(max(0.0, float(days_since_last - threshold)), 1)

    return {
        "distinct_days_count": distinct_count,
        "used_dates": [d.isoformat() for d in used_dates],
        "intervals": intervals,
        "last_purchase_date": last_date.isoformat(),
        "days_since_last": days_since_last,
        "avg_interval_days": round(avg_interval, 2),
        "avg_interval_raw": avg_interval,
        "threshold_days": round(threshold, 2),
        "days_overdue": days_overdue if is_dormant else 0.0,
        "days_past_threshold": days_past_threshold if is_dormant else 0.0,
        "status": status,
        "status_label": status_label,
    }


def get_dormant_clients_report(
    search: Optional[str] = None,
    status_filter: str = "all",
    sort_by: str = "days_overdue",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 25,
    reference_date: Optional[date] = None,
    conn=None,
) -> Dict[str, Any]:
    """Generate the full Dormant Clients report.

    Performs parameterized windowed queries across cs and sales,
    computes accurate cadence, and returns summary metrics with paginated client records.
    """
    if reference_date is None:
        reference_date = date.today()

    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 25)))
    status_filter = str(status_filter or "all").strip().lower()
    sort_by = str(sort_by or "days_overdue").strip().lower()
    sort_order = str(sort_order or "desc").strip().lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # 1. Fetch latest sales date from sales table
            cur.execute("""
                SELECT MAX(SALES_DATE) as latest_sales_date
                FROM sales
                WHERE SALES_DATE IS NOT NULL AND SALES_DATE <= %s
            """, (datetime.combine(reference_date, datetime.max.time()),))
            row = cur.fetchone() or {}
            raw_latest_sales = row.get("latest_sales_date")
            latest_sales_date_str = (
                raw_latest_sales.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(raw_latest_sales, "strftime")
                else (str(raw_latest_sales) if raw_latest_sales else None)
            )

            # Check if latest sales data is stale (> 14 days ago)
            is_sales_date_stale = False
            days_since_sales = None
            if raw_latest_sales:
                sales_day = raw_latest_sales.date() if hasattr(raw_latest_sales, "date") else None
                if sales_day:
                    days_since_sales = (reference_date - sales_day).days
                    if days_since_sales > 14:
                        is_sales_date_stale = True

            # 2. Fetch all valid registered customers from cs
            cur.execute("""
                SELECT CS_ID, CS_NAME, MOBILE, PHONE, ADDRESS
                FROM cs
                WHERE CS_ID IS NOT NULL AND CS_ID != ''
                ORDER BY CS_ID ASC
            """)
            cs_rows = cur.fetchall()

            # 3. Fetch windowed up-to-6 latest distinct purchase dates per customer in a single pass
            # Ignores null and future dates
            cur.execute("""
                SELECT CS_ID, purchase_date, rn FROM (
                    SELECT CS_ID, DATE(SALES_DATE) as purchase_date,
                           ROW_NUMBER() OVER (PARTITION BY CS_ID ORDER BY DATE(SALES_DATE) DESC) as rn
                    FROM sales
                    WHERE CS_ID IS NOT NULL AND CS_ID != ''
                      AND SALES_DATE IS NOT NULL
                      AND DATE(SALES_DATE) <= %s
                    GROUP BY CS_ID, DATE(SALES_DATE)
                ) ranked
                WHERE rn <= 6
                ORDER BY CS_ID ASC, purchase_date ASC
            """, (reference_date,))
            purchase_rows = cur.fetchall()

        # Map purchase dates by customer ID
        cust_dates: Dict[str, List[date]] = {}
        for r in purchase_rows:
            cid = str(r["CS_ID"]).strip()
            pdate = r["purchase_date"]
            if cid not in cust_dates:
                cust_dates[cid] = []
            cust_dates[cid].append(pdate)

        # 4. Evaluate cadence for each customer
        all_evaluated: List[Dict[str, Any]] = []
        dormant_count = 0
        insufficient_data_count = 0
        active_count = 0

        for cs in cs_rows:
            cid = str(cs.get("CS_ID") or "").strip()
            cname = str(cs.get("CS_NAME") or "").strip()
            mobile = cs.get("MOBILE")
            phone = cs.get("PHONE")
            address = str(cs.get("ADDRESS") or "").strip()

            # Choose best phone (prefer mobile, then phone)
            display_phone, dial_phone, has_valid_phone = validate_and_clean_phone(mobile)
            if not has_valid_phone and phone:
                display_phone, dial_phone, has_valid_phone = validate_and_clean_phone(phone)

            dates = cust_dates.get(cid, [])
            cadence = calculate_customer_cadence(dates, reference_date=reference_date)

            c_status = cadence["status"]
            if c_status == "dormant":
                dormant_count += 1
            elif c_status == "insufficient_data":
                insufficient_data_count += 1
            else:
                active_count += 1

            client_entry = {
                "customer_id": cid,
                "customer_name": cname,
                "phone": display_phone,
                "dial_phone": dial_phone,
                "has_valid_phone": has_valid_phone,
                "address": address,
                "last_purchase_date": cadence["last_purchase_date"],
                "distinct_purchase_days": cadence["distinct_days_count"],
                "avg_interval_days": cadence["avg_interval_days"],
                "threshold_days": cadence["threshold_days"],
                "days_since_last": cadence["days_since_last"],
                "days_overdue": cadence["days_overdue"],
                "days_past_threshold": cadence["days_past_threshold"],
                "status": cadence["status"],
                "status_label": cadence["status_label"],
            }
            all_evaluated.append(client_entry)

        # 5. Global Summary across the entire report (not just the current page)
        summary = {
            "total_customers": len(all_evaluated),
            "dormant_count": dormant_count,
            "insufficient_data_count": insufficient_data_count,
            "active_count": active_count,
            "latest_sales_date": latest_sales_date_str,
            "is_sales_date_stale": is_sales_date_stale,
            "days_since_latest_sale": days_since_sales,
            "reference_date": reference_date.isoformat(),
            "report_generated_at": datetime.now().isoformat(),
        }

        # 6. Apply search filter (name, phone, customer_id)
        filtered = all_evaluated
        if search:
            query = search.strip().lower()
            filtered = [
                c for c in filtered
                if query in c["customer_name"].lower()
                or query in c["customer_id"].lower()
                or (c["phone"] and query in c["phone"].lower())
                or (c["dial_phone"] and query in c["dial_phone"].lower())
            ]

        # 7. Apply status filter
        if status_filter in ("dormant", "insufficient_data", "active"):
            filtered = [c for c in filtered if c["status"] == status_filter]

        # 8. Sort records
        def sort_key(item: Dict[str, Any]):
            if sort_by == "days_overdue":
                val = item.get("days_overdue")
                return (0 if val is None else 1, val if val is not None else -999999)
            elif sort_by == "days_since_last":
                val = item.get("days_since_last")
                return (0 if val is None else 1, val if val is not None else -999999)
            elif sort_by == "customer_name":
                return (1, str(item.get("customer_name") or "").lower())
            elif sort_by == "last_purchase_date":
                val = item.get("last_purchase_date")
                return (0 if not val else 1, val or "")
            elif sort_by == "avg_interval":
                val = item.get("avg_interval_days")
                return (0 if val is None else 1, val if val is not None else -999999)
            else:
                val = item.get("days_overdue")
                return (0 if val is None else 1, val if val is not None else -999999)

        reverse = (sort_order == "desc")
        # For customer_name, default ascending order is natural unless requested
        if sort_by == "customer_name" and sort_order == "asc":
            reverse = False

        filtered.sort(key=sort_key, reverse=reverse)

        # 9. Paginate
        total_items = len(filtered)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = filtered[start_idx:end_idx]

        pagination = {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        }

        return {
            "success": True,
            "summary": summary,
            "pagination": pagination,
            "filters": {
                "q": search or "",
                "status": status_filter,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
            "clients": page_items,
        }

    finally:
        if close_conn and conn:
            conn.close()


def get_customer_purchase_history(
    customer_id: str,
    reference_date: Optional[date] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Fetch complete purchase history and cadence analysis for a single customer.

    Returns:
        dict with customer details, up-to-6 calculation dates with intervals,
        invoices list, and cadence metrics, or None if customer not found.
    """
    if reference_date is None:
        reference_date = date.today()

    cid = str(customer_id or "").strip()
    if not cid:
        return None

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # 1. Customer metadata from cs
            cur.execute("""
                SELECT CS_ID, CS_NAME, MOBILE, PHONE, ADDRESS, EMAIL, CONTACT_PERSON
                FROM cs
                WHERE CS_ID = %s
                LIMIT 1
            """, (cid,))
            customer = cur.fetchone()
            if not customer:
                return None

            # 2. Invoices from sales (most recent 50)
            cur.execute("""
                SELECT SALES_ID, SALES_DATE, NET_SALES, TOTAL_PRICE
                FROM sales
                WHERE CS_ID = %s
                  AND SALES_DATE IS NOT NULL
                  AND DATE(SALES_DATE) <= %s
                ORDER BY SALES_DATE DESC
                LIMIT 50
            """, (cid, reference_date))
            sales_rows = cur.fetchall()

            # 3. Overall latest sales date
            cur.execute("""
                SELECT MAX(SALES_DATE) as latest_sales_date
                FROM sales
                WHERE SALES_DATE IS NOT NULL AND SALES_DATE <= %s
            """, (datetime.combine(reference_date, datetime.max.time()),))
            s_row = cur.fetchone() or {}
            raw_latest_sales = s_row.get("latest_sales_date")
            latest_sales_date_str = (
                raw_latest_sales.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(raw_latest_sales, "strftime")
                else (str(raw_latest_sales) if raw_latest_sales else None)
            )

        # Extract distinct purchase dates from sales rows
        pdates = []
        invoices = []
        for s in sales_rows:
            s_date = s.get("SALES_DATE")
            d_obj = s_date.date() if hasattr(s_date, "date") else None
            if d_obj:
                pdates.append(d_obj)
            invoices.append({
                "sales_id": str(s.get("SALES_ID") or "").strip(),
                "sales_date": s_date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(s_date, "strftime") else str(s_date),
                "purchase_date": d_obj.isoformat() if d_obj else None,
                "net_sales": float(s.get("NET_SALES") or 0),
                "total_price": float(s.get("TOTAL_PRICE") or 0),
            })

        # Calculate cadence
        cadence = calculate_customer_cadence(pdates, reference_date=reference_date)

        # Phone cleanup
        display_phone, dial_phone, has_valid_phone = validate_and_clean_phone(customer.get("MOBILE"))
        if not has_valid_phone and customer.get("PHONE"):
            display_phone, dial_phone, has_valid_phone = validate_and_clean_phone(customer.get("PHONE"))

        return {
            "success": True,
            "customer": {
                "customer_id": cid,
                "customer_name": str(customer.get("CS_NAME") or "").strip(),
                "phone": display_phone,
                "dial_phone": dial_phone,
                "has_valid_phone": has_valid_phone,
                "address": str(customer.get("ADDRESS") or "").strip(),
                "email": str(customer.get("EMAIL") or "").strip(),
                "contact_person": str(customer.get("CONTACT_PERSON") or "").strip(),
            },
            "status": cadence["status"],
            "status_label": cadence["status_label"],
            "last_purchase_date": cadence["last_purchase_date"],
            "distinct_purchase_days_count": cadence["distinct_days_count"],
            "avg_interval_days": cadence["avg_interval_days"],
            "threshold_days": cadence["threshold_days"],
            "days_since_last": cadence["days_since_last"],
            "days_overdue": cadence["days_overdue"],
            "days_past_threshold": cadence["days_past_threshold"],
            "calculation_dates": cadence["used_dates"],
            "intervals": cadence["intervals"],
            "invoices": invoices,
            "latest_sales_date": latest_sales_date_str,
            "report_generated_at": datetime.now().isoformat(),
        }

    finally:
        if close_conn and conn:
            conn.close()
