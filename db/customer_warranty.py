"""Customer Warranty Service.

Report 08: Customer Warranty
Displays sold customer products with warranty status (Active, Expiring Soon, Expired, Not Started, Unknown),
search/filters, and provides Admin-only management for warranty records.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .connection import get_connection

logger = logging.getLogger("customer_warranty")

CUSTOMER_WARRANTY_MENU_ID = 31
CUSTOMER_WARRANTY_SLUGS = {
    "customer-warranty",
    "customer_warranty",
    "warranty-customers",
    "warranty_customers",
    "/menus/customer-warranty",
    "/customer-warranty",
}


def parse_date(value: Any) -> Optional[date]:
    """Safely parse input to a datetime.date object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", ""):
        return None
    # Handle YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
    s = s.split("T")[0].split(" ")[0]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date format '{value}'. Expected YYYY-MM-DD.")


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

    cleaned = re.sub(r"[\s\-\(\)\.]+", "", raw)
    if not cleaned or cleaned == "0":
        return ("", "", False)

    digits_no_cc = re.sub(r"^\+?88", "", cleaned)
    if re.match(r"^01[3-9]\d{8}$", digits_no_cc):
        return (raw, digits_no_cc, True)

    if re.match(r"^\+?\d{7,15}$", cleaned) and cleaned != "0":
        return (raw, cleaned, True)

    return (raw, "", False)


def compute_warranty_status(
    start_date: Optional[date],
    end_date: Optional[date],
    reference_date: Optional[date] = None,
) -> str:
    """Compute warranty status based on start/end dates and current reference date.

    Rules:
    - Active: warranty has started and ends more than 30 days later.
    - Expiring Soon: warranty has started and ends today or within the next 30 days.
    - Expired: the end date is before today.
    - Not Started: the start date is in the future.
    - Unknown: dates needed to determine the status are missing.
    - Validation: end date cannot be earlier than start date.
    """
    if reference_date is None:
        reference_date = date.today()

    if start_date is None or end_date is None:
        return "Unknown"

    if end_date < start_date:
        raise ValueError("Warranty end date cannot be earlier than start date.")

    if start_date > reference_date:
        return "Not Started"

    if end_date < reference_date:
        return "Expired"

    # Started and not expired: check 30-day boundary
    delta_days = (end_date - reference_date).days
    if delta_days <= 30:
        return "Expiring Soon"

    return "Active"


def can_view_customer_warranty(user: Mapping[str, Any] | None) -> bool:
    """Check if the user is authorized to view Customer Warranty."""
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
        slugs = {str(x).strip().lower() for x in allowed if isinstance(x, str)}
        if any(slug in slugs for slug in CUSTOMER_WARRANTY_SLUGS):
            return True

        try:
            allowed_ids = {int(x) for x in allowed if str(x).isdigit() or isinstance(x, int)}
            if CUSTOMER_WARRANTY_MENU_ID in allowed_ids:
                return True
        except (ValueError, TypeError):
            pass

    return False


def is_admin_user(user: Mapping[str, Any] | None) -> bool:
    """Check if user has Admin role."""
    if not user:
        return False
    return str(user.get("role", "")).strip().lower() == "admin"


def get_warranty_summary(
    reference_date: Optional[date] = None,
    conn=None,
) -> Dict[str, Any]:
    """Get aggregate summary counts across all warranty records.

    Card labels note: Counts represent warranty records, not unique customers.
    """
    if reference_date is None:
        reference_date = date.today()

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE 
                        WHEN warranty_start_date IS NOT NULL AND warranty_end_date IS NOT NULL
                             AND warranty_start_date <= %s AND warranty_end_date > DATE_ADD(%s, INTERVAL 30 DAY)
                        THEN 1 ELSE 0 END) AS active_cnt,
                    SUM(CASE 
                        WHEN warranty_start_date IS NOT NULL AND warranty_end_date IS NOT NULL
                             AND warranty_start_date <= %s AND warranty_end_date >= %s 
                             AND warranty_end_date <= DATE_ADD(%s, INTERVAL 30 DAY)
                        THEN 1 ELSE 0 END) AS expiring_soon_cnt,
                    SUM(CASE 
                        WHEN warranty_start_date IS NOT NULL AND warranty_end_date IS NOT NULL
                             AND warranty_end_date < %s
                        THEN 1 ELSE 0 END) AS expired_cnt,
                    SUM(CASE 
                        WHEN warranty_start_date IS NOT NULL AND warranty_end_date IS NOT NULL
                             AND warranty_start_date > %s
                        THEN 1 ELSE 0 END) AS not_started_cnt,
                    SUM(CASE 
                        WHEN warranty_start_date IS NULL OR warranty_end_date IS NULL
                        THEN 1 ELSE 0 END) AS unknown_cnt,
                    COUNT(*) as total_records
                FROM customer_warranty_record;
            """, (
                reference_date, reference_date,
                reference_date, reference_date, reference_date,
                reference_date,
                reference_date,
            ))
            row = cur.fetchone() or {}

            return {
                "active": int(row.get("active_cnt") or 0),
                "expiring_soon": int(row.get("expiring_soon_cnt") or 0),
                "expired": int(row.get("expired_cnt") or 0),
                "not_started": int(row.get("not_started_cnt") or 0),
                "unknown": int(row.get("unknown_cnt") or 0),
                "total_records": int(row.get("total_records") or 0),
                "reference_date": reference_date.strftime("%Y-%m-%d"),
                "counts_note": "Counts represent warranty records, not unique customers.",
            }
    finally:
        if close_conn and conn:
            conn.close()


def get_warranty_records(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = "warranty_end_date",
    sort_order: Optional[str] = "asc",
    page: int = 1,
    page_size: int = 25,
    reference_date: Optional[date] = None,
    conn=None,
) -> Dict[str, Any]:
    """Fetch paginated, filtered warranty records with summary stats."""
    if reference_date is None:
        reference_date = date.today()

    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 25)))
    offset = (page - 1) * page_size

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        summary = get_warranty_summary(reference_date=reference_date, conn=conn)

        # Base SELECT with computed warranty status
        base_query = """
            FROM customer_warranty_record cwr
            JOIN cs c ON cwr.customer_id = c.CS_ID
            JOIN product p ON cwr.product_id = p.PRODUCT_ID
            LEFT JOIN sales s ON cwr.sales_id = s.SALES_ID
            LEFT JOIN category cat ON p.CATEGORY_ID = cat.CATEGORY_ID
            WHERE 1=1
        """
        params: List[Any] = []

        # Search filter: customer name, customer id, phone, product name, invoice (sales_id), serial
        if search:
            s_clean = search.strip()
            if s_clean:
                search_term = f"%{s_clean}%"
                base_query += """
                    AND (
                        c.CS_NAME LIKE %s
                        OR c.CS_ID LIKE %s
                        OR c.MOBILE LIKE %s
                        OR c.PHONE LIKE %s
                        OR p.PRODUCT_NAME LIKE %s
                        OR p.PRODUCT_ID LIKE %s
                        OR cwr.sales_id LIKE %s
                        OR cwr.serial_number LIKE %s
                    )
                """
                params.extend([search_term] * 8)

        # Date range filter on warranty_end_date (or sales_date if warranty date is null)
        d_from = None
        d_to = None
        if date_from:
            try:
                d_from = parse_date(date_from)
                base_query += " AND cwr.warranty_end_date >= %s"
                params.append(d_from)
            except ValueError:
                pass

        if date_to:
            try:
                d_to = parse_date(date_to)
                base_query += " AND cwr.warranty_end_date <= %s"
                params.append(d_to)
            except ValueError:
                pass

        # Status filter
        if status_filter:
            norm_status = status_filter.strip().lower()
            if norm_status == "active":
                base_query += """
                    AND cwr.warranty_start_date IS NOT NULL 
                    AND cwr.warranty_end_date IS NOT NULL
                    AND cwr.warranty_start_date <= %s 
                    AND cwr.warranty_end_date > DATE_ADD(%s, INTERVAL 30 DAY)
                """
                params.extend([reference_date, reference_date])
            elif norm_status in ("expiring_soon", "expiring"):
                base_query += """
                    AND cwr.warranty_start_date IS NOT NULL 
                    AND cwr.warranty_end_date IS NOT NULL
                    AND cwr.warranty_start_date <= %s 
                    AND cwr.warranty_end_date >= %s 
                    AND cwr.warranty_end_date <= DATE_ADD(%s, INTERVAL 30 DAY)
                """
                params.extend([reference_date, reference_date, reference_date])
            elif norm_status == "expired":
                base_query += """
                    AND cwr.warranty_start_date IS NOT NULL 
                    AND cwr.warranty_end_date IS NOT NULL
                    AND cwr.warranty_end_date < %s
                """
                params.append(reference_date)
            elif norm_status in ("not_started", "pending"):
                base_query += """
                    AND cwr.warranty_start_date IS NOT NULL 
                    AND cwr.warranty_end_date IS NOT NULL
                    AND cwr.warranty_start_date > %s
                """
                params.append(reference_date)
            elif norm_status == "unknown":
                base_query += """
                    AND (cwr.warranty_start_date IS NULL OR cwr.warranty_end_date IS NULL)
                """

        # Total count for filtered query
        count_sql = f"SELECT COUNT(*) as filtered_cnt {base_query}"
        with conn.cursor() as cur:
            cur.execute(count_sql, tuple(params))
            c_row = cur.fetchone()
            filtered_total = int(c_row.get("filtered_cnt") or 0) if c_row else 0

        # Sorting
        sort_col_map = {
            "warranty_end_date": "cwr.warranty_end_date",
            "warranty_start_date": "cwr.warranty_start_date",
            "sales_date": "s.SALES_DATE",
            "customer_name": "c.CS_NAME",
            "product_name": "p.PRODUCT_NAME",
            "id": "cwr.id",
            "serial": "cwr.serial_number",
        }
        order_col = sort_col_map.get((sort_by or "").lower(), "cwr.warranty_end_date")
        direction = "DESC" if str(sort_order).lower() == "desc" else "ASC"

        # If sorting by warranty_end_date ASC, put NULLs last
        if order_col == "cwr.warranty_end_date" and direction == "ASC":
            order_clause = "ORDER BY (cwr.warranty_end_date IS NULL), cwr.warranty_end_date ASC, cwr.id DESC"
        elif order_col == "cwr.warranty_end_date" and direction == "DESC":
            order_clause = "ORDER BY cwr.warranty_end_date DESC, cwr.id DESC"
        else:
            order_clause = f"ORDER BY {order_col} {direction}, cwr.id DESC"

        select_sql = f"""
            SELECT
                cwr.id,
                cwr.customer_id,
                c.CS_NAME as customer_name,
                c.MOBILE as customer_mobile,
                c.PHONE as customer_phone,
                cwr.product_id,
                p.PRODUCT_NAME as product_name,
                COALESCE(cat.CATEGORY_NAME, 'General') as category_name,
                cwr.sales_id,
                s.SALES_DATE as sales_date,
                cwr.serial_number,
                cwr.warranty_start_date,
                cwr.warranty_end_date,
                cwr.warranty_source,
                cwr.notes,
                cwr.updated_by_username,
                cwr.created_at,
                cwr.updated_at,
                CASE
                    WHEN cwr.warranty_start_date IS NULL OR cwr.warranty_end_date IS NULL THEN 'Unknown'
                    WHEN cwr.warranty_start_date > %s THEN 'Not Started'
                    WHEN cwr.warranty_end_date < %s THEN 'Expired'
                    WHEN DATEDIFF(cwr.warranty_end_date, %s) <= 30 THEN 'Expiring Soon'
                    ELSE 'Active'
                END AS computed_status
            {base_query}
            {order_clause}
            LIMIT %s OFFSET %s
        """
        exec_params = [reference_date, reference_date, reference_date] + params + [page_size, offset]

        records = []
        with conn.cursor() as cur:
            cur.execute(select_sql, tuple(exec_params))
            rows = cur.fetchall()

        for r in rows:
            disp_phone, dial_phone, has_phone = validate_and_clean_phone(r.get("customer_mobile"))
            if not has_phone and r.get("customer_phone"):
                disp_phone, dial_phone, has_phone = validate_and_clean_phone(r.get("customer_phone"))

            st_d = r.get("warranty_start_date")
            en_d = r.get("warranty_end_date")
            sa_d = r.get("sales_date")
            cr_t = r.get("created_at")
            up_t = r.get("updated_at")

            st_str = st_d.strftime("%Y-%m-%d") if hasattr(st_d, "strftime") else (str(st_d) if st_d else None)
            en_str = en_d.strftime("%Y-%m-%d") if hasattr(en_d, "strftime") else (str(en_d) if en_d else None)
            sa_str = sa_d.strftime("%Y-%m-%d") if hasattr(sa_d, "strftime") else (str(sa_d) if sa_d else None)
            cr_str = cr_t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(cr_t, "strftime") else (str(cr_t) if cr_t else None)
            up_str = up_t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(up_t, "strftime") else (str(up_t) if up_t else None)

            days_remaining = None
            if en_d and hasattr(en_d, "year"):
                days_remaining = (en_d - reference_date).days

            status = r.get("computed_status") or "Unknown"

            records.append({
                "id": int(r["id"]),
                "customer_id": str(r["customer_id"]),
                "customer_name": str(r.get("customer_name") or "").strip(),
                "phone": disp_phone or None,
                "dial_phone": dial_phone or None,
                "has_valid_phone": has_phone,
                "product_id": str(r["product_id"]),
                "product_name": str(r.get("product_name") or "").strip(),
                "category": str(r.get("category_name") or "General"),
                "sales_id": str(r["sales_id"]),
                "sales_date": sa_str,
                "serial_number": str(r["serial_number"]).strip() if r.get("serial_number") else None,
                "warranty_start_date": st_str,
                "warranty_end_date": en_str,
                "warranty_status": status,
                "warranty_source": str(r.get("warranty_source") or "Admin entry"),
                "notes": str(r.get("notes") or "").strip() or None,
                "days_remaining": days_remaining,
                "updated_by_username": str(r.get("updated_by_username") or "admin"),
                "created_at": cr_str,
                "updated_at": up_str,
            })

        total_pages = (filtered_total + page_size - 1) // page_size if page_size > 0 else 1

        return {
            "success": True,
            "summary": summary,
            "filters": {
                "search": search or None,
                "status": status_filter or "all",
                "date_from": date_from or None,
                "date_to": date_to or None,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": filtered_total,
                "total_pages": max(1, total_pages),
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
            "records": records,
        }

    finally:
        if close_conn and conn:
            conn.close()


def get_warranty_record_detail(
    record_id: int,
    reference_date: Optional[date] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Fetch complete details of a single warranty record."""
    if reference_date is None:
        reference_date = date.today()

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    cwr.id,
                    cwr.customer_id,
                    c.CS_NAME as customer_name,
                    c.MOBILE as customer_mobile,
                    c.PHONE as customer_phone,
                    c.ADDRESS as customer_address,
                    c.EMAIL as customer_email,
                    c.CONTACT_PERSON as customer_contact,
                    cwr.product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(cat.CATEGORY_NAME, 'General') as category_name,
                    cwr.sales_id,
                    s.SALES_DATE as sales_date,
                    s.NET_SALES as invoice_total,
                    cwr.serial_number,
                    cwr.warranty_start_date,
                    cwr.warranty_end_date,
                    cwr.warranty_source,
                    cwr.notes,
                    cwr.updated_by_user_id,
                    cwr.updated_by_username,
                    cwr.created_at,
                    cwr.updated_at
                FROM customer_warranty_record cwr
                JOIN cs c ON cwr.customer_id = c.CS_ID
                JOIN product p ON cwr.product_id = p.PRODUCT_ID
                LEFT JOIN sales s ON cwr.sales_id = s.SALES_ID
                LEFT JOIN category cat ON p.CATEGORY_ID = cat.CATEGORY_ID
                WHERE cwr.id = %s
                LIMIT 1;
            """, (record_id,))
            r = cur.fetchone()
            if not r:
                return None

            disp_phone, dial_phone, has_phone = validate_and_clean_phone(r.get("customer_mobile"))
            if not has_phone and r.get("customer_phone"):
                disp_phone, dial_phone, has_phone = validate_and_clean_phone(r.get("customer_phone"))

            st_d = r.get("warranty_start_date")
            en_d = r.get("warranty_end_date")
            sa_d = r.get("sales_date")
            cr_t = r.get("created_at")
            up_t = r.get("updated_at")

            status = compute_warranty_status(st_d, en_d, reference_date)

            days_remaining = None
            if en_d and hasattr(en_d, "year"):
                days_remaining = (en_d - reference_date).days

            st_str = st_d.strftime("%Y-%m-%d") if hasattr(st_d, "strftime") else (str(st_d) if st_d else None)
            en_str = en_d.strftime("%Y-%m-%d") if hasattr(en_d, "strftime") else (str(en_d) if en_d else None)
            sa_str = sa_d.strftime("%Y-%m-%d") if hasattr(sa_d, "strftime") else (str(sa_d) if sa_d else None)
            cr_str = cr_t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(cr_t, "strftime") else (str(cr_t) if cr_t else None)
            up_str = up_t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(up_t, "strftime") else (str(up_t) if up_t else None)

            return {
                "id": int(r["id"]),
                "customer": {
                    "id": str(r["customer_id"]),
                    "name": str(r.get("customer_name") or "").strip(),
                    "phone": disp_phone or None,
                    "dial_phone": dial_phone or None,
                    "has_valid_phone": has_phone,
                    "address": str(r.get("customer_address") or "").strip() or None,
                    "email": str(r.get("customer_email") or "").strip() or None,
                    "contact_person": str(r.get("customer_contact") or "").strip() or None,
                },
                "product": {
                    "id": str(r["product_id"]),
                    "name": str(r.get("product_name") or "").strip(),
                    "category": str(r.get("category_name") or "General"),
                },
                "sale": {
                    "sales_id": str(r["sales_id"]),
                    "sales_date": sa_str,
                    "invoice_total": float(r.get("invoice_total") or 0.0),
                },
                "warranty": {
                    "serial_number": str(r["serial_number"]).strip() if r.get("serial_number") else None,
                    "start_date": st_str,
                    "end_date": en_str,
                    "status": status,
                    "source": str(r.get("warranty_source") or "Admin entry"),
                    "notes": str(r.get("notes") or "").strip() or None,
                    "days_remaining": days_remaining,
                },
                "audit": {
                    "updated_by_user_id": r.get("updated_by_user_id"),
                    "updated_by_username": str(r.get("updated_by_username") or "admin"),
                    "created_at": cr_str,
                    "updated_at": up_str,
                },
            }

    finally:
        if close_conn and conn:
            conn.close()


def create_warranty_record(
    data: Mapping[str, Any],
    user: Mapping[str, Any],
    conn=None,
) -> Dict[str, Any]:
    """Create a new warranty record. Admin only."""
    if not is_admin_user(user):
        raise PermissionError("Only Admin users are permitted to create warranty records.")

    cid = str(data.get("customer_id") or "").strip()
    if not cid:
        raise ValueError("Customer ID is required.")

    sid = str(data.get("sales_id") or "").strip()
    if not sid:
        raise ValueError("Sales / Invoice ID is required.")

    pid = str(data.get("product_id") or "").strip()
    if not pid:
        raise ValueError("Product ID is required.")

    serial = str(data.get("serial_number") or "").strip() or None
    source = str(data.get("warranty_source") or "Admin entry").strip() or "Admin entry"
    notes = str(data.get("notes") or "").strip() or None

    start_d = parse_date(data.get("warranty_start_date"))
    end_d = parse_date(data.get("warranty_end_date"))

    if start_d and end_d and end_d < start_d:
        raise ValueError("Warranty end date cannot be earlier than start date.")

    user_id = user.get("id")
    username = str(user.get("username") or "admin")

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # Verify customer exists
            cur.execute("SELECT CS_ID FROM cs WHERE CS_ID = %s LIMIT 1;", (cid,))
            if not cur.fetchone():
                return {
                    "success": False,
                    "status_code": 404,
                    "message": f"Customer '{cid}' does not exist in the database.",
                }

            # Verify product exists
            cur.execute("SELECT PRODUCT_ID FROM product WHERE PRODUCT_ID = %s LIMIT 1;", (pid,))
            if not cur.fetchone():
                return {
                    "success": False,
                    "status_code": 404,
                    "message": f"Product '{pid}' does not exist in the database.",
                }

            cur.execute("""
                INSERT INTO customer_warranty_record (
                    customer_id, sales_id, product_id, serial_number,
                    warranty_start_date, warranty_end_date, warranty_source,
                    notes, updated_by_user_id, updated_by_username
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (
                cid, sid, pid, serial,
                start_d, end_d, source,
                notes, user_id, username,
            ))
            new_id = cur.lastrowid
        conn.commit()

        # Audit event
        try:
            from .auth import audit_event
            audit_event(
                user_id=int(user_id or 1),
                action="WARRANTY_RECORD_CREATE",
                details=f"Created warranty record #{new_id} for invoice {sid}, customer {cid} by {username}",
            )
        except Exception:
            logger.warning("Could not record audit event for warranty creation")

        record = get_warranty_record_detail(new_id, conn=conn)
        return {
            "success": True,
            "status_code": 201,
            "message": "Customer warranty record created successfully.",
            "record_id": new_id,
            "data": record,
        }

    finally:
        if close_conn and conn:
            conn.close()


def update_warranty_record(
    record_id: int,
    data: Mapping[str, Any],
    user: Mapping[str, Any],
    conn=None,
) -> Dict[str, Any]:
    """Update an existing warranty record. Admin only."""
    if not is_admin_user(user):
        raise PermissionError("Only Admin users are permitted to update warranty records.")

    try:
        rid = int(record_id)
    except (ValueError, TypeError):
        raise ValueError("Invalid record ID.")

    start_d = parse_date(data.get("warranty_start_date"))
    end_d = parse_date(data.get("warranty_end_date"))

    if start_d and end_d and end_d < start_d:
        raise ValueError("Warranty end date cannot be earlier than start date.")

    serial = data.get("serial_number")
    if serial is not None:
        serial = str(serial).strip() or None

    source = data.get("warranty_source")
    if source is not None:
        source = str(source).strip() or "Admin entry"

    notes = data.get("notes")
    if notes is not None:
        notes = str(notes).strip() or None

    user_id = user.get("id")
    username = str(user.get("username") or "admin")

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, customer_id, sales_id FROM customer_warranty_record WHERE id = %s LIMIT 1;", (rid,))
            existing = cur.fetchone()
            if not existing:
                return {
                    "success": False,
                    "status_code": 404,
                    "message": f"Warranty record #{rid} not found.",
                }

            update_parts = []
            params: List[Any] = []

            # Only update fields that were provided or allow nulling dates
            if "warranty_start_date" in data:
                update_parts.append("warranty_start_date = %s")
                params.append(start_d)

            if "warranty_end_date" in data:
                update_parts.append("warranty_end_date = %s")
                params.append(end_d)

            if serial is not None or "serial_number" in data:
                update_parts.append("serial_number = %s")
                params.append(serial)

            if source is not None:
                update_parts.append("warranty_source = %s")
                params.append(source)

            if notes is not None or "notes" in data:
                update_parts.append("notes = %s")
                params.append(notes)

            update_parts.append("updated_by_user_id = %s")
            params.append(user_id)

            update_parts.append("updated_by_username = %s")
            params.append(username)

            params.append(rid)

            sql = f"""
                UPDATE customer_warranty_record
                SET {', '.join(update_parts)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """
            cur.execute(sql, tuple(params))
        conn.commit()

        # Audit event
        try:
            from .auth import audit_event
            audit_event(
                user_id=int(user_id or 1),
                action="WARRANTY_RECORD_UPDATE",
                details=f"Updated warranty record #{rid} (sales {existing.get('sales_id')}) by {username}",
            )
        except Exception:
            logger.warning("Could not record audit event for warranty update")

        detail = get_warranty_record_detail(rid, conn=conn)
        return {
            "success": True,
            "status_code": 200,
            "message": "Customer warranty record updated successfully.",
            "record_id": rid,
            "data": detail,
        }

    finally:
        if close_conn and conn:
            conn.close()
