"""Customer Business KYC (Know Your Customer) Service.

Report 06: Customer Business KYC
Displays customer directory, business profile, purchase summary (printers & ink),
and provides Admin-only management for business profile data.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .connection import get_connection

logger = logging.getLogger("customer_kyc")

# Menu ID for Customer KYC in sidebar_menus
CUSTOMER_KYC_MENU_ID = 30
CUSTOMER_KYC_SLUGS = {
    "customer-kyc",
    "customer_kyc",
    "customer kyc",
    "/menus/customer-kyc",
    "/customer-kyc",
}

# Standard suggested business types for filtering / selection
DEFAULT_BUSINESS_TYPES = [
    "Corporate & Commercial Office",
    "Printing Press & Publication",
    "Retail Stationery & Bookshop",
    "Government Organization / NGO",
    "Educational Institution",
    "IT & Tech Service Center",
    "Wholesale Distributor / Dealer",
    "Healthcare & Diagnostics",
    "Other Commercial Business",
]


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


def can_view_customer_kyc(user: Mapping[str, Any] | None) -> bool:
    """Check if the user is authorized to view Customer KYC."""
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
        if any(slug in slugs for slug in CUSTOMER_KYC_SLUGS):
            return True

        try:
            allowed_ids = {int(x) for x in allowed if str(x).isdigit() or isinstance(x, int)}
            if CUSTOMER_KYC_MENU_ID in allowed_ids:
                return True
        except (ValueError, TypeError):
            pass

    return False


def is_admin_user(user: Mapping[str, Any] | None) -> bool:
    """Check if user has Admin role."""
    if not user:
        return False
    return str(user.get("role", "")).strip().lower() == "admin"


def get_customer_kyc_directory(
    search: Optional[str] = None,
    business_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    conn=None,
) -> Dict[str, Any]:
    """Fetch paginated customer directory with KYC business profiles and sales totals."""
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 25)))
    offset = (page - 1) * page_size

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # 1. Fetch available business types for filter dropdown
            cur.execute("""
                SELECT DISTINCT business_type 
                FROM customer_kyc_profile 
                WHERE business_type IS NOT NULL AND business_type != ''
                ORDER BY business_type ASC
            """)
            db_types = [r["business_type"] for r in cur.fetchall()]
            all_types = sorted(list(set(DEFAULT_BUSINESS_TYPES + db_types)))

            # Build query filters
            where_clauses = ["c.CS_ID IS NOT NULL AND c.CS_ID != ''"]
            params: List[Any] = []

            if search:
                q_clean = f"%{search.strip()}%"
                where_clauses.append(
                    "(c.CS_ID LIKE %s OR c.CS_NAME LIKE %s OR c.MOBILE LIKE %s OR c.PHONE LIKE %s OR kyp.business_name LIKE %s)"
                )
                params.extend([q_clean, q_clean, q_clean, q_clean, q_clean])

            if business_type and business_type != "all":
                if business_type == "none":
                    where_clauses.append("(kyp.business_type IS NULL OR kyp.business_type = '')")
                else:
                    where_clauses.append("kyp.business_type = %s")
                    params.append(business_type.strip())

            where_str = " AND ".join(where_clauses)

            # 2. Global summary counts across the entire directory
            summary_sql = f"""
                SELECT 
                    COUNT(DISTINCT c.CS_ID) as total_customers,
                    COUNT(DISTINCT CASE WHEN kyp.id IS NOT NULL AND (kyp.business_name IS NOT NULL OR kyp.business_type IS NOT NULL) THEN c.CS_ID END) as profiled_count,
                    COUNT(DISTINCT CASE WHEN s_agg.order_count > 0 THEN c.CS_ID END) as with_sales_count
                FROM cs c
                LEFT JOIN customer_kyc_profile kyp ON c.CS_ID = kyp.customer_id
                LEFT JOIN (
                    SELECT CS_ID, COUNT(DISTINCT SALES_ID) as order_count
                    FROM sales
                    WHERE CS_ID IS NOT NULL AND CS_ID != ''
                    GROUP BY CS_ID
                ) s_agg ON c.CS_ID = s_agg.CS_ID
                WHERE {where_str}
            """
            cur.execute(summary_sql, tuple(params))
            stats = cur.fetchone() or {}
            total_items = int(stats.get("total_customers") or 0)
            profiled_count = int(stats.get("profiled_count") or 0)
            with_sales_count = int(stats.get("with_sales_count") or 0)

            # 3. Fetch paginated records with safe join
            # Sales aggregate subquery avoids cartesian join duplication
            query_sql = f"""
                SELECT 
                    c.CS_ID as customer_id,
                    c.CS_NAME as customer_name,
                    c.ADDRESS as address,
                    c.MOBILE as mobile,
                    c.PHONE as phone,
                    c.EMAIL as email,
                    c.CONTACT_PERSON as default_contact,
                    c.CUSTOMER_TYPE as default_type,
                    kyp.id as kyc_profile_id,
                    kyp.business_name,
                    kyp.business_type,
                    kyp.contact_person as kyc_contact_person,
                    kyp.notes as kyc_notes,
                    kyp.updated_by_username,
                    kyp.updated_at as kyc_updated_at,
                    COALESCE(s_agg.order_count, 0) as order_count,
                    COALESCE(s_agg.total_spend, 0) as total_spend,
                    s_agg.last_purchase_date
                FROM cs c
                LEFT JOIN customer_kyc_profile kyp ON c.CS_ID = kyp.customer_id
                LEFT JOIN (
                    SELECT 
                        CS_ID,
                        COUNT(DISTINCT SALES_ID) as order_count,
                        ROUND(SUM(NET_SALES), 2) as total_spend,
                        MAX(SALES_DATE) as last_purchase_date
                    FROM sales
                    WHERE CS_ID IS NOT NULL AND CS_ID != ''
                    GROUP BY CS_ID
                ) s_agg ON c.CS_ID = s_agg.CS_ID
                WHERE {where_str}
                ORDER BY c.CS_ID ASC
                LIMIT %s OFFSET %s
            """
            exec_params = list(params) + [page_size, offset]
            cur.execute(query_sql, tuple(exec_params))
            rows = cur.fetchall()

        customers = []
        for r in rows:
            cid = str(r["customer_id"]).strip()
            cname = str(r["customer_name"]).strip()
            mobile = r.get("mobile")
            phone = r.get("phone")
            disp_phone, dial_phone, has_phone = validate_and_clean_phone(mobile)
            if not has_phone and phone:
                disp_phone, dial_phone, has_phone = validate_and_clean_phone(phone)

            has_profile = bool(r.get("kyc_profile_id"))
            b_name = r.get("business_name")
            b_type = r.get("business_type")

            lp_date = r.get("last_purchase_date")
            lp_str = lp_date.strftime("%Y-%m-%d") if hasattr(lp_date, "strftime") else (str(lp_date) if lp_date else None)

            up_at = r.get("kyc_updated_at")
            up_at_str = up_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(up_at, "strftime") else (str(up_at) if up_at else None)

            customers.append({
                "customer_id": cid,
                "customer_name": cname,
                "phone": disp_phone,
                "dial_phone": dial_phone,
                "has_valid_phone": has_phone,
                "address": str(r.get("address") or "").strip(),
                "has_business_profile": has_profile,
                "business_name": b_name if b_name else None,
                "business_type": b_type if b_type else None,
                "contact_person": r.get("kyc_contact_person") or r.get("default_contact") or None,
                "order_count": int(r.get("order_count") or 0),
                "total_spend": float(r.get("total_spend") or 0.0),
                "last_purchase_date": lp_str,
                "updated_by_username": r.get("updated_by_username"),
                "updated_at": up_at_str,
            })

        total_pages = max(1, (total_items + page_size - 1) // page_size)
        pagination = {
            "page": page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        }

        summary = {
            "total_customers": total_items,
            "profiled_customers": profiled_count,
            "unprofiled_customers": max(0, total_items - profiled_count),
            "customers_with_sales": with_sales_count,
            "customers_without_sales": max(0, total_items - with_sales_count),
            "report_generated_at": datetime.now().isoformat(),
        }

        return {
            "success": True,
            "summary": summary,
            "business_types": all_types,
            "pagination": pagination,
            "filters": {
                "q": search or "",
                "business_type": business_type or "all",
            },
            "customers": customers,
        }

    finally:
        if close_conn and conn:
            conn.close()


def get_customer_kyc_detail(customer_id: str, conn=None) -> Optional[Dict[str, Any]]:
    """Retrieve full KYC profile, contact details, and purchase summary for a single customer."""
    cid = str(customer_id or "").strip()
    if not cid:
        return None

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # 1. Base customer information from cs
            cur.execute("""
                SELECT 
                    CS_ID as customer_id,
                    CS_NAME as customer_name,
                    ADDRESS as address,
                    MOBILE as mobile,
                    PHONE as phone,
                    EMAIL as email,
                    CONTACT_PERSON as default_contact,
                    CUSTOMER_TYPE as default_type
                FROM cs
                WHERE CS_ID = %s
                LIMIT 1
            """, (cid,))
            cs_row = cur.fetchone()
            if not cs_row:
                return None

            # 2. Business profile from customer_kyc_profile
            cur.execute("""
                SELECT 
                    id,
                    business_name,
                    business_type,
                    contact_person,
                    notes,
                    updated_by_username,
                    created_at,
                    updated_at
                FROM customer_kyc_profile
                WHERE customer_id = %s
                LIMIT 1
            """, (cid,))
            kyc_row = cur.fetchone()

            # 3. Sales totals from sales table
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT SALES_ID) as order_count,
                    COALESCE(SUM(NET_SALES), 0) as total_spend,
                    MAX(SALES_DATE) as last_purchase_date,
                    MIN(SALES_DATE) as first_purchase_date
                FROM sales
                WHERE CS_ID = %s AND SALES_DATE IS NOT NULL
            """, (cid,))
            sales_stats = cur.fetchone() or {}

            # 4. Top purchased printer & ink products from sales_details
            cur.execute("""
                SELECT 
                    sd.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'General') as category_name,
                    SUM(sd.QTY) as total_qty,
                    ROUND(SUM(sd.QTY * sd.UNIT_PRICE), 2) as total_amount,
                    MAX(sd.SALES_DATE) as last_bought_date
                FROM sales_details sd
                JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                WHERE sd.CS_ID = %s
                GROUP BY sd.PRODUCT_ID, p.PRODUCT_NAME, category_name
                ORDER BY total_qty DESC
                LIMIT 15
            """, (cid,))
            prod_rows = cur.fetchall()

        disp_phone, dial_phone, has_phone = validate_and_clean_phone(cs_row.get("mobile"))
        if not has_phone and cs_row.get("phone"):
            disp_phone, dial_phone, has_phone = validate_and_clean_phone(cs_row.get("phone"))

        lp_date = sales_stats.get("last_purchase_date")
        lp_str = lp_date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(lp_date, "strftime") else (str(lp_date) if lp_date else None)

        fp_date = sales_stats.get("first_purchase_date")
        fp_str = fp_date.strftime("%Y-%m-%d") if hasattr(fp_date, "strftime") else (str(fp_date) if fp_date else None)

        products = []
        for pr in prod_rows:
            p_lb = pr.get("last_bought_date")
            p_lb_str = p_lb.strftime("%Y-%m-%d") if hasattr(p_lb, "strftime") else (str(p_lb) if p_lb else None)
            products.append({
                "product_id": str(pr["product_id"]),
                "product_name": str(pr["product_name"]),
                "category": str(pr["category_name"]),
                "total_quantity": float(pr.get("total_qty") or 0),
                "total_amount": float(pr.get("total_amount") or 0.0),
                "last_purchased": p_lb_str,
            })

        has_profile = kyc_row is not None
        business_profile = None
        if has_profile:
            up_at = kyc_row.get("updated_at")
            cr_at = kyc_row.get("created_at")
            business_profile = {
                "business_name": kyc_row.get("business_name") or None,
                "business_type": kyc_row.get("business_type") or None,
                "contact_person": kyc_row.get("contact_person") or None,
                "notes": kyc_row.get("notes") or None,
                "updated_by_username": kyc_row.get("updated_by_username") or "admin",
                "created_at": cr_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(cr_at, "strftime") else str(cr_at),
                "updated_at": up_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(up_at, "strftime") else str(up_at),
            }

        return {
            "success": True,
            "customer_id": cid,
            "customer_name": str(cs_row.get("customer_name") or "").strip(),
            "contact_details": {
                "phone": disp_phone or None,
                "dial_phone": dial_phone or None,
                "has_valid_phone": has_phone,
                "address": str(cs_row.get("address") or "").strip() or None,
                "email": str(cs_row.get("email") or "").strip() or None,
                "default_contact_person": str(cs_row.get("default_contact") or "").strip() or None,
                "default_customer_type": str(cs_row.get("default_type") or "").strip() or None,
            },
            "has_business_profile": has_profile,
            "business_profile": business_profile,
            "purchase_summary": {
                "order_count": int(sales_stats.get("order_count") or 0),
                "total_spend": float(sales_stats.get("total_spend") or 0.0),
                "first_purchase_date": fp_str,
                "last_purchase_date": lp_str,
                "has_sales_history": int(sales_stats.get("order_count") or 0) > 0,
                "top_products": products,
            },
            "retrieved_at": datetime.now().isoformat(),
        }

    finally:
        if close_conn and conn:
            conn.close()


def save_customer_kyc_profile(
    customer_id: str,
    data: Mapping[str, Any],
    user: Mapping[str, Any],
    conn=None,
) -> Dict[str, Any]:
    """Create or update business KYC profile for an existing customer.

    Requires Admin privileges.
    """
    if not is_admin_user(user):
        raise PermissionError("Only Admin users are permitted to create or modify KYC business profiles.")

    cid = str(customer_id or "").strip()
    if not cid:
        raise ValueError("Customer ID is required.")

    b_name = str(data.get("business_name") or "").strip() or None
    b_type = str(data.get("business_type") or "").strip() or None
    c_person = str(data.get("contact_person") or "").strip() or None
    notes = str(data.get("notes") or "").strip() or None

    user_id = user.get("id")
    username = str(user.get("username") or "admin")

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        with conn.cursor() as cur:
            # 1. Verify customer exists in cs
            cur.execute("SELECT CS_ID FROM cs WHERE CS_ID = %s LIMIT 1", (cid,))
            if not cur.fetchone():
                return {
                    "success": False,
                    "status_code": 404,
                    "message": f"Customer '{cid}' does not exist in the database.",
                }

            # 2. Insert or update kyc profile
            cur.execute("""
                INSERT INTO customer_kyc_profile (
                    customer_id, business_name, business_type, contact_person, notes,
                    updated_by_user_id, updated_by_username
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    business_name = VALUES(business_name),
                    business_type = VALUES(business_type),
                    contact_person = VALUES(contact_person),
                    notes = VALUES(notes),
                    updated_by_user_id = VALUES(updated_by_user_id),
                    updated_by_username = VALUES(updated_by_username),
                    updated_at = CURRENT_TIMESTAMP
            """, (cid, b_name, b_type, c_person, notes, user_id, username))
        conn.commit()

        # Audit event record
        try:
            from .auth import audit_event
            audit_event(
                user_id=int(user_id or 1),
                action="KYC_PROFILE_UPDATE",
                details=f"Customer '{cid}' KYC profile updated by {username}",
            )
        except Exception:
            logger.warning("Could not record audit event for KYC update")

        # Fetch updated profile
        detail = get_customer_kyc_detail(cid, conn=conn)

        return {
            "success": True,
            "status_code": 200,
            "message": "Customer business profile saved successfully.",
            "data": detail,
        }

    finally:
        if close_conn and conn:
            conn.close()
