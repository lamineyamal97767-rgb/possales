from typing import Dict, Any, List, Optional
from datetime import date, datetime
from decimal import Decimal
import time
from .connection import get_connection

_OVERVIEW_CACHE: Dict[str, Any] = {"data": None, "ts": 0}
_PRINTERS_CACHE: Dict[str, Any] = {"data": None, "ts": 0}
_INKS_CACHE: Dict[str, Any] = {"data": None, "ts": 0}
_REFILL_CACHE: Dict[str, Any] = {"data": None, "ts": 0}
_TTL = 60

_TABLE_INITIALIZED = False


def init_ink_compatibility_table():
    """Ensure application-owned printer_ink_compatibility table exists."""
    global _TABLE_INITIALIZED
    if _TABLE_INITIALIZED:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS printer_ink_compatibility (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    printer_id VARCHAR(10) NOT NULL,
                    ink_id VARCHAR(10) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    source VARCHAR(100) NOT NULL DEFAULT 'Admin confirmed',
                    notes TEXT NULL,
                    created_by VARCHAR(50) DEFAULT 'admin',
                    updated_by VARCHAR(50) DEFAULT 'admin',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_printer_ink (printer_id, ink_id),
                    KEY idx_pic_printer_status (printer_id, status),
                    KEY idx_pic_ink_status (ink_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
        conn.commit()
    _TABLE_INITIALIZED = True


def clear_ink_cache():
    for c in (_OVERVIEW_CACHE, _PRINTERS_CACHE, _INKS_CACHE, _REFILL_CACHE):
        c["data"] = None


def format_date(dt) -> str:
    if not dt:
        return "-"
    if isinstance(dt, (date, datetime)):
        return dt.strftime("%d %b %Y")
    return str(dt)


def extract_color(name: str) -> Dict[str, str]:
    """Detect ink/toner color from product name."""
    n = name.upper()
    if "CYAN" in n or " (C)" in n:
        return {"color": "Cyan", "badge": "bg-sky-50 text-sky-700 border-sky-300", "dot": "#0ea5e9"}
    elif "MAGENTA" in n or " (M)" in n:
        return {"color": "Magenta", "badge": "bg-pink-50 text-pink-700 border-pink-300", "dot": "#ec4899"}
    elif "YELLOW" in n or " (Y)" in n:
        return {"color": "Yellow", "badge": "bg-amber-50 text-amber-700 border-amber-300", "dot": "#eab308"}
    elif "BLACK" in n or "(BK)" in n or " BK" in n or "BH7" in n or "810" in n:
        return {"color": "Black", "badge": "bg-slate-900 text-white border-slate-700", "dot": "#0f172a"}
    elif "COLOR" in n or "CH7" in n or "811" in n or "TRI-COLOR" in n:
        return {"color": "Color (CMY)", "badge": "bg-purple-50 text-purple-700 border-purple-300", "dot": "#a855f7"}
    elif "790" in n or "003" in n or "664" in n or "057" in n or "BOTTLE" in n:
        return {"color": "Ink Bottle", "badge": "bg-blue-50 text-blue-700 border-blue-300", "dot": "#3b82f6"}
    else:
        return {"color": "Black (Mono)", "badge": "bg-slate-100 text-slate-700 border-slate-300", "dot": "#475569"}


def clean_record(d: Any) -> Any:
    """Ensure all values in a record are standard JSON-serializable types."""
    if isinstance(d, dict):
        for k, v in list(d.items()):
            if isinstance(v, (date, datetime)):
                d[k] = v.strftime("%Y-%m-%d")
            elif isinstance(v, Decimal):
                d[k] = float(v)
            elif isinstance(v, (dict, list)):
                d[k] = clean_record(v)
        return d
    elif isinstance(d, list):
        return [clean_record(i) for i in d]
    return d


# -----------------------------------------------------------------------------
# Validation Helpers
# -----------------------------------------------------------------------------

def validate_printer_id(printer_id: str) -> Optional[Dict[str, Any]]:
    """Validate that a product exists in database and is classified as a printer."""
    if not printer_id:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'PRINTER') as category_name,
                    p.CATEGORY_ID as category_id
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                WHERE p.PRODUCT_ID = %s
                  AND (p.CATEGORY_ID = 'CAT006' OR p.PRODUCT_NAME LIKE '%%PRINTER%%')
                LIMIT 1
            """, (printer_id.strip(),))
            row = cur.fetchone()
            return clean_record(row) if row else None


def validate_ink_id(ink_id: str) -> Optional[Dict[str, Any]]:
    """Validate that a product exists in database and is classified as ink/toner consumable."""
    if not ink_id:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'Consumable') as category_name,
                    p.CATEGORY_ID as category_id
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                WHERE p.PRODUCT_ID = %s
                  AND p.CATEGORY_ID != 'CAT006'
                  AND p.PRODUCT_NAME NOT LIKE '%%PRINTER%%'
                  AND p.PRODUCT_NAME NOT LIKE '%%SCANNER%%'
                  AND p.PRODUCT_NAME NOT LIKE '%%PHOTOCOPIER%%'
                  AND (
                    p.PRODUCT_NAME LIKE '%%TONER%%' 
                    OR p.PRODUCT_NAME LIKE '%%INK%%' 
                    OR p.PRODUCT_NAME LIKE '%%CARTRIDGE%%' 
                    OR p.PRODUCT_NAME LIKE '%%RIBBON%%'
                    OR p.PRODUCT_NAME LIKE '%%BOTTLE%%'
                    OR p.PRODUCT_NAME LIKE '%%POWDER%%'
                    OR p.PRODUCT_NAME LIKE '%%CHIP%%'
                    OR p.PRODUCT_NAME LIKE '%%DRUM%%'
                    OR p.PRODUCT_NAME LIKE '%%BOX%%'
                  )
                LIMIT 1
            """, (ink_id.strip(),))
            row = cur.fetchone()
            return clean_record(row) if row else None


# -----------------------------------------------------------------------------
# Admin Relationship Management CRUD
# -----------------------------------------------------------------------------

def add_printer_ink_relation(
    printer_id: str,
    ink_id: str,
    source: str = "Admin confirmed",
    notes: str = "",
    username: str = "admin",
) -> Dict[str, Any]:
    """
    Admin-only: Add a confirmed dependency between a printer and an ink/toner.
    Prevents duplicates and validates that both products are valid.
    """
    init_ink_compatibility_table()
    p_id = (printer_id or "").strip()
    i_id = (ink_id or "").strip()
    src = (source or "Admin confirmed").strip()
    nts = (notes or "").strip()
    usr = (username or "admin").strip()

    if not p_id or not i_id:
        raise ValueError("Printer ID and Ink/Toner ID are required.")

    # 1. Validate printer
    printer = validate_printer_id(p_id)
    if not printer:
        raise ValueError(f"Invalid printer ID '{p_id}'. Must be a recognized printer product.")

    # 2. Validate ink
    ink = validate_ink_id(i_id)
    if not ink:
        raise ValueError(f"Invalid ink/toner ID '{i_id}'. Must be a recognized ink, toner, or consumable product.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 3. Check for existing relation
            cur.execute("""
                SELECT id, printer_id, ink_id, status, source, notes
                FROM printer_ink_compatibility
                WHERE printer_id = %s AND ink_id = %s
                LIMIT 1
            """, (p_id, i_id))
            existing = cur.fetchone()

            if existing:
                if existing.get("status") == "active":
                    raise ValueError(f"Duplicate relationship: Printer '{p_id}' and Ink '{i_id}' are already linked as active.")
                else:
                    # Reactivate existing inactive relation
                    cur.execute("""
                        UPDATE printer_ink_compatibility
                        SET status = 'active', source = %s, notes = %s, updated_by = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (src, nts, usr, existing["id"]))
                    conn.commit()
                    clear_ink_cache()
                    return {
                        "id": existing["id"],
                        "printer_id": p_id,
                        "ink_id": i_id,
                        "status": "active",
                        "source": src,
                        "notes": nts,
                        "reactivated": True,
                        "printer_name": printer["product_name"],
                        "ink_name": ink["product_name"],
                    }

            # 4. Insert new relationship
            cur.execute("""
                INSERT INTO printer_ink_compatibility (
                    printer_id, ink_id, status, source, notes, created_by, updated_by
                ) VALUES (%s, %s, 'active', %s, %s, %s, %s)
            """, (p_id, i_id, src, nts, usr, usr))
            new_id = cur.lastrowid
            conn.commit()
            clear_ink_cache()
            return {
                "id": new_id,
                "printer_id": p_id,
                "ink_id": i_id,
                "status": "active",
                "source": src,
                "notes": nts,
                "reactivated": False,
                "printer_name": printer["product_name"],
                "ink_name": ink["product_name"],
            }


def update_printer_ink_relation(
    relation_id: int,
    status: Optional[str] = None,
    source: Optional[str] = None,
    notes: Optional[str] = None,
    username: str = "admin",
) -> bool:
    """Admin-only: Update relation status ('active'/'inactive'), source, or notes."""
    init_ink_compatibility_table()
    usr = (username or "admin").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM printer_ink_compatibility WHERE id = %s", (relation_id,))
            if not cur.fetchone():
                return False

            updates = ["updated_by = %s", "updated_at = NOW()"]
            params: List[Any] = [usr]

            if status is not None:
                st = status.strip().lower()
                if st not in ("active", "inactive"):
                    raise ValueError("Status must be 'active' or 'inactive'.")
                updates.append("status = %s")
                params.append(st)

            if source is not None:
                updates.append("source = %s")
                params.append(source.strip())

            if notes is not None:
                updates.append("notes = %s")
                params.append(notes.strip())

            params.append(relation_id)
            sql = f"UPDATE printer_ink_compatibility SET {', '.join(updates)} WHERE id = %s"
            cur.execute(sql, tuple(params))
            conn.commit()
            clear_ink_cache()
            return True


def deactivate_printer_ink_relation(relation_id: int, username: str = "admin") -> bool:
    """Admin-only: Deactivate a confirmed relationship."""
    return update_printer_ink_relation(relation_id, status="inactive", username=username)


def delete_printer_ink_relation(relation_id: int) -> bool:
    """Admin-only: Permanently delete a relationship record."""
    init_ink_compatibility_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM printer_ink_compatibility WHERE id = %s", (relation_id,))
            affected = cur.rowcount > 0
            conn.commit()
            clear_ink_cache()
            return affected


def list_printer_ink_relations(
    printer_id: Optional[str] = None,
    ink_id: Optional[str] = None,
    status: Optional[str] = "active",
) -> List[Dict[str, Any]]:
    """List confirmed relationships with joined product titles."""
    init_ink_compatibility_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            conditions = []
            params: List[Any] = []
            if printer_id:
                conditions.append("c.printer_id = %s")
                params.append(printer_id.strip())
            if ink_id:
                conditions.append("c.ink_id = %s")
                params.append(ink_id.strip())
            if status:
                conditions.append("c.status = %s")
                params.append(status.strip().lower())

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                SELECT 
                    c.id, c.printer_id, c.ink_id, c.status, c.source, c.notes,
                    c.created_by, c.updated_by, c.created_at, c.updated_at,
                    p1.PRODUCT_NAME as printer_name,
                    p2.PRODUCT_NAME as ink_name,
                    COALESCE(st1.stock_qty, 0) as printer_stock,
                    COALESCE(st2.stock_qty, 0) as ink_stock
                FROM printer_ink_compatibility c
                LEFT JOIN product p1 ON c.printer_id = p1.PRODUCT_ID
                LEFT JOIN product p2 ON c.ink_id = p2.PRODUCT_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial GROUP BY PRODUCT_ID
                ) st1 ON c.printer_id = st1.PRODUCT_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial GROUP BY PRODUCT_ID
                ) st2 ON c.ink_id = st2.PRODUCT_ID
                {where_clause}
                ORDER BY c.updated_at DESC
            """
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            for r in rows:
                clean_record(r)
                r["printer_stock"] = int(r.get("printer_stock") or 0)
                r["ink_stock"] = int(r.get("ink_stock") or 0)
            return rows


# -----------------------------------------------------------------------------
# Overview KPIs
# -----------------------------------------------------------------------------

def get_ink_dependency_overview() -> Dict[str, Any]:
    """Overview KPIs for Printer & Ink Synergy module."""
    init_ink_compatibility_table()
    now = time.time()
    if _OVERVIEW_CACHE["data"] and (now - _OVERVIEW_CACHE["ts"]) < _TTL:
        return _OVERVIEW_CACHE["data"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Total printer models
            cur.execute("""
                SELECT COUNT(DISTINCT PRODUCT_ID) as total_printers
                FROM product
                WHERE CATEGORY_ID = 'CAT006' OR PRODUCT_NAME LIKE '%PRINTER%'
            """)
            printers_count = cur.fetchone().get("total_printers") or 0

            # 2. Total ink / toner / consumable items
            cur.execute("""
                SELECT COUNT(DISTINCT PRODUCT_ID) as total_consumables
                FROM product
                WHERE PRODUCT_NAME LIKE '%TONER%' 
                   OR PRODUCT_NAME LIKE '%INK%' 
                   OR PRODUCT_NAME LIKE '%CARTRIDGE%' 
                   OR PRODUCT_NAME LIKE '%RIBBON%'
            """)
            consumables_count = cur.fetchone().get("total_consumables") or 0

            # 3. Synergy rate: % of printer buyers who also purchase consumables
            cur.execute("""
                SELECT COUNT(DISTINCT sd.CS_ID) as total_printer_buyers
                FROM sales_details sd
                JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
                WHERE (p.CATEGORY_ID = 'CAT006' OR p.PRODUCT_NAME LIKE '%PRINTER%')
                  AND sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
            """)
            pb_row = cur.fetchone() or {}
            total_pb = pb_row.get("total_printer_buyers") or 0
            cur.execute("""
                SELECT COUNT(DISTINCT sd.CS_ID) as joint_buyers
                FROM sales_details sd
                JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
                WHERE (p.PRODUCT_NAME LIKE '%TONER%' OR p.PRODUCT_NAME LIKE '%INK%' OR p.PRODUCT_NAME LIKE '%CARTRIDGE%' OR p.PRODUCT_NAME LIKE '%RIBBON%')
                  AND sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
                  AND sd.CS_ID IN (
                    SELECT DISTINCT sd2.CS_ID FROM sales_details sd2
                    JOIN product p2 ON sd2.PRODUCT_ID = p2.PRODUCT_ID
                    WHERE (p2.CATEGORY_ID = 'CAT006' OR p2.PRODUCT_NAME LIKE '%PRINTER%')
                  )
            """)
            syn = cur.fetchone() or {}
            joint_b = syn.get("joint_buyers") or 0
            if not total_pb:
                total_pb = 1
            synergy_rate = round((joint_b / total_pb * 100), 1) if total_pb > 0 else 0

            # 4. Total consumable volume sold
            cur.execute("""
                SELECT 
                    ROUND(SUM(sd.QTY), 0) as total_units,
                    ROUND(SUM(sd.QTY * sd.UNIT_PRICE), 2) as total_revenue
                FROM sales_details sd
                JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
                WHERE p.PRODUCT_NAME LIKE '%TONER%' 
                   OR p.PRODUCT_NAME LIKE '%INK%' 
                   OR p.PRODUCT_NAME LIKE '%CARTRIDGE%' 
                   OR p.PRODUCT_NAME LIKE '%RIBBON%'
            """)
            ink_sales = cur.fetchone() or {}

            # 5. Total Admin confirmed relationships
            cur.execute("""
                SELECT COUNT(*) as total_confirmed
                FROM printer_ink_compatibility
                WHERE status = 'active'
            """)
            conf_row = cur.fetchone() or {}
            total_confirmed = conf_row.get("total_confirmed") or 0

            data = {
                "total_printers": int(printers_count),
                "total_consumables": int(consumables_count),
                "synergy_rate": float(synergy_rate),
                "total_ink_units_sold": int(ink_sales.get("total_units") or 0),
                "total_ink_revenue": float(ink_sales.get("total_revenue") or 0),
                "total_confirmed_relations": int(total_confirmed),
            }
            _OVERVIEW_CACHE.update(data=data, ts=now)
            return data


def get_printer_models_list() -> List[Dict[str, Any]]:
    """List of all printers with active sales and owner metrics."""
    now = time.time()
    if _PRINTERS_CACHE["data"] and (now - _PRINTERS_CACHE["ts"]) < _TTL:
        return _PRINTERS_CACHE["data"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'PRINTER') as category_name,
                    COALESCE(st.stock_qty, 0) as current_stock,
                    COALESCE(p.SALES_PRICE, 0) as sales_price,
                    COUNT(DISTINCT sd.SALES_ID) as sales_count,
                    COALESCE(SUM(sd.QTY), 0) as total_units_sold,
                    COUNT(DISTINCT sd.CS_ID) as unique_owners
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty 
                    FROM stock_non_serial 
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                LEFT JOIN sales_details sd ON p.PRODUCT_ID = sd.PRODUCT_ID
                WHERE p.CATEGORY_ID = 'CAT006' OR p.PRODUCT_NAME LIKE '%PRINTER%'
                GROUP BY p.PRODUCT_ID, p.PRODUCT_NAME, category_name, st.stock_qty, p.SALES_PRICE
                ORDER BY total_units_sold DESC, sales_count DESC
            """)
            rows = cur.fetchall()
            for r in rows:
                clean_record(r)
                r["total_units_sold"] = int(r["total_units_sold"] or 0)
                r["sales_count"] = int(r["sales_count"] or 0)
                r["unique_owners"] = int(r["unique_owners"] or 0)
                r["current_stock"] = int(r["current_stock"] or 0)
                r["sales_price"] = float(r["sales_price"] or 0)
                name_upper = r["product_name"].upper()
                if "EPSON" in name_upper:
                    r["brand"] = "Epson"
                elif "CANON" in name_upper:
                    r["brand"] = "Canon"
                elif "HP" in name_upper:
                    r["brand"] = "HP"
                elif "BROTHER" in name_upper:
                    r["brand"] = "Brother"
                elif "PANTUM" in name_upper:
                    r["brand"] = "Pantum"
                else:
                    r["brand"] = "Other"
            _PRINTERS_CACHE.update(data=rows, ts=now)
            return rows


def get_ink_products_list() -> List[Dict[str, Any]]:
    """List of all consumable toners and inks with sales performance."""
    now = time.time()
    if _INKS_CACHE["data"] and (now - _INKS_CACHE["ts"]) < _TTL:
        return _INKS_CACHE["data"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID as product_id,
                    p.PRODUCT_NAME as product_name,
                    COALESCE(c.CATEGORY_NAME, 'Consumable') as category_name,
                    COALESCE(st.stock_qty, 0) as current_stock,
                    COALESCE(p.SALES_PRICE, 0) as sales_price,
                    COUNT(DISTINCT sd.SALES_ID) as orders_count,
                    COALESCE(SUM(sd.QTY), 0) as total_units_sold,
                    ROUND(COALESCE(SUM(sd.QTY * sd.UNIT_PRICE), 0), 2) as total_revenue,
                    COUNT(DISTINCT sd.CS_ID) as unique_buyers
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty 
                    FROM stock_non_serial 
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                LEFT JOIN sales_details sd ON p.PRODUCT_ID = sd.PRODUCT_ID
                WHERE p.PRODUCT_NAME LIKE '%TONER%' 
                   OR p.PRODUCT_NAME LIKE '%INK%' 
                   OR p.PRODUCT_NAME LIKE '%CARTRIDGE%' 
                   OR p.PRODUCT_NAME LIKE '%RIBBON%'
                GROUP BY p.PRODUCT_ID, p.PRODUCT_NAME, category_name, st.stock_qty, p.SALES_PRICE
                ORDER BY total_units_sold DESC
            """)
            rows = cur.fetchall()
            for r in rows:
                clean_record(r)
                r["total_units_sold"] = int(r["total_units_sold"] or 0)
                r["orders_count"] = int(r["orders_count"] or 0)
                r["unique_buyers"] = int(r["unique_buyers"] or 0)
                r["current_stock"] = int(r["current_stock"] or 0)
                r["sales_price"] = float(r["sales_price"] or 0)
                r["total_revenue"] = float(r["total_revenue"] or 0)
                name_upper = r["product_name"].upper()
                if "TONER" in name_upper:
                    r["ink_type"] = "Toner Cartridge"
                elif "RIBBON" in name_upper:
                    r["ink_type"] = "Ribbon"
                elif "CARTRIDGE" in name_upper:
                    r["ink_type"] = "Ink Cartridge"
                elif "BOTTLE" in name_upper or "ECOTANK" in name_upper:
                    r["ink_type"] = "Ink Bottle"
                else:
                    r["ink_type"] = "Ink/Powder"
            _INKS_CACHE.update(data=rows, ts=now)
            return rows


# -----------------------------------------------------------------------------
# Dependency Matrices: Admin Confirmed + Possible Match (Co-purchase)
# -----------------------------------------------------------------------------

def get_printer_ink_matrix(printer_id: str) -> Dict[str, Any]:
    """
    Report 03 (Part A): Printer to Ink Dependency.
    Returns:
    - printer: Basic model info & owner metrics.
    - confirmed_inks: Inks verified compatible by Admin (Admin confirmed).
    - possible_inks: Inks bought by owners of this printer in sales history (Possible match).
      NOTE: Co-purchase does not guarantee hardware compatibility.
    - consumables: Combined list (confirmed first) for backward compatibility.
    """
    init_ink_compatibility_table()
    p_id = (printer_id or "").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Printer info
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID, p.PRODUCT_NAME, COALESCE(c.CATEGORY_NAME, 'PRINTER') as CATEGORY_NAME,
                    COALESCE(st.stock_qty, 0) as current_stock, p.SALES_PRICE
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty 
                    FROM stock_non_serial 
                    WHERE PRODUCT_ID = %s
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                WHERE p.PRODUCT_ID = %s
            """, (p_id, p_id))
            printer = cur.fetchone()
            if not printer:
                return {}
            clean_record(printer)

            # 2. Customers who bought this printer
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT sd.CS_ID) as total_buyers,
                    COALESCE(SUM(sd.QTY), 0) as printer_units_sold
                FROM sales_details sd
                WHERE sd.PRODUCT_ID = %s AND sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
            """, (p_id,))
            p_stats = cur.fetchone() or {}

            # 3. Admin Confirmed Inks
            cur.execute("""
                SELECT 
                    c.id as relation_id,
                    c.printer_id,
                    c.ink_id,
                    c.status,
                    c.source,
                    c.notes,
                    c.created_by,
                    c.updated_by,
                    c.created_at,
                    c.updated_at,
                    p.PRODUCT_NAME as ink_name,
                    COALESCE(cat.CATEGORY_NAME, 'Consumable') as category_name,
                    COALESCE(st.stock_qty, 0) as warehouse_stock,
                    COALESCE(p.SALES_PRICE, 0) as sales_price
                FROM printer_ink_compatibility c
                JOIN product p ON c.ink_id = p.PRODUCT_ID
                LEFT JOIN category cat ON p.CATEGORY_ID = cat.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                WHERE c.printer_id = %s AND c.status = 'active'
                ORDER BY c.created_at DESC
            """, (p_id,))
            confirmed_raw = cur.fetchall()
            confirmed_inks = []
            confirmed_ids = set()

            for cr in confirmed_raw:
                clean_record(cr)
                confirmed_ids.add(cr["ink_id"])
                col_info = extract_color(cr["ink_name"])
                confirmed_inks.append({
                    "relation_id": cr.get("relation_id"),
                    "ink_id": cr["ink_id"],
                    "ink_name": cr["ink_name"],
                    "category_name": cr.get("category_name", "Consumable"),
                    "warehouse_stock": int(cr.get("warehouse_stock") or 0),
                    "sales_price": float(cr.get("sales_price") or 0),
                    "color": col_info["color"],
                    "color_badge": col_info["badge"],
                    "color_dot": col_info["dot"],
                    "source": cr.get("source") or "Admin confirmed",
                    "notes": cr.get("notes") or "",
                    "updated_at": format_date(cr.get("updated_at") or cr.get("created_at")),
                    "status": "Admin confirmed",
                    "match_type": "Admin confirmed",
                    "is_confirmed": True,
                    "confidence_label": "Admin Confirmed (Verified)",
                    "est_cycle_days": 45,
                    "restock_recommendation": 10,
                    "total_units_bought": 0,
                    "total_spent": 0.0,
                    "refill_orders_count": 0,
                    "repeat_customers_count": 0,
                })

            # 4. Possible Matches: Consumables bought by owners of this printer from sales history
            cur.execute("""
                WITH buyers AS (
                    SELECT DISTINCT CS_ID FROM sales_details
                    WHERE PRODUCT_ID = %s AND CS_ID IS NOT NULL AND CS_ID != ''
                )
                SELECT
                    p2.PRODUCT_ID as ink_id,
                    p2.PRODUCT_NAME as ink_name,
                    COALESCE(c2.CATEGORY_NAME, 'Consumable') as category_name,
                    COALESCE(st2.stock_qty, 0) as warehouse_stock,
                    ROUND(AVG(s2.UNIT_PRICE), 2) as avg_price,
                    COALESCE(SUM(s2.QTY), 0) as total_units_bought,
                    ROUND(SUM(s2.QTY * s2.UNIT_PRICE), 2) as total_spent,
                    COUNT(DISTINCT s2.SALES_ID) as refill_orders_count,
                    COUNT(DISTINCT s2.CS_ID) as repeat_customers_count,
                    MAX(s2.SALES_DATE) as last_refill_date
                FROM sales_details s2
                JOIN buyers b ON s2.CS_ID = b.CS_ID
                JOIN product p2 ON s2.PRODUCT_ID = p2.PRODUCT_ID
                LEFT JOIN category c2 ON p2.CATEGORY_ID = c2.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial
                    GROUP BY PRODUCT_ID
                ) st2 ON p2.PRODUCT_ID = st2.PRODUCT_ID
                WHERE s2.PRODUCT_ID != %s
                  AND p2.CATEGORY_ID != 'CAT006'
                  AND p2.PRODUCT_NAME NOT LIKE '%%PRINTER%%'
                  AND p2.PRODUCT_NAME NOT LIKE '%%SCANNER%%'
                  AND p2.PRODUCT_NAME NOT LIKE '%%IMAGECLASS%%'
                  AND p2.PRODUCT_NAME NOT LIKE '%%PHOTOCOPIER%%'
                  AND (
                    p2.PRODUCT_NAME LIKE '%%TONER%%'
                    OR p2.PRODUCT_NAME LIKE '%%INK%%'
                    OR p2.PRODUCT_NAME LIKE '%%CARTRIDGE%%'
                    OR p2.PRODUCT_NAME LIKE '%%RIBBON%%'
                    OR p2.PRODUCT_NAME LIKE '%%BOTTLE%%'
                    OR p2.PRODUCT_NAME LIKE '%%POWDER%%'
                    OR p2.PRODUCT_NAME LIKE '%%CHIP%%'
                    OR p2.PRODUCT_NAME LIKE '%%DRUM%%'
                    OR p2.PRODUCT_NAME LIKE '%%BOX%%'
                  )
                GROUP BY p2.PRODUCT_ID, p2.PRODUCT_NAME, category_name, st2.stock_qty
                ORDER BY total_units_bought DESC
                LIMIT 50
            """, (p_id, p_id))
            consumables_raw = cur.fetchall()

            possible_inks = []
            for c in consumables_raw:
                clean_record(c)
                ink_id = c["ink_id"]
                # If already confirmed, update stats in confirmed item if applicable, but keep distinct
                col_info = extract_color(c["ink_name"])
                refill_orders = int(c.get("refill_orders_count") or 0)
                cycle_days = max(15, min(90, round(365 / refill_orders))) if refill_orders > 1 else 45
                restock_rec = max(10, round(int(c.get("total_units_bought") or 0) * 0.25))

                item = {
                    "ink_id": ink_id,
                    "ink_name": c["ink_name"],
                    "category_name": c.get("category_name", "Consumable"),
                    "warehouse_stock": int(c.get("warehouse_stock") or 0),
                    "avg_price": float(c.get("avg_price") or 0),
                    "sales_price": float(c.get("avg_price") or 0),
                    "total_units_bought": int(c.get("total_units_bought") or 0),
                    "total_spent": float(c.get("total_spent") or 0),
                    "refill_orders_count": refill_orders,
                    "repeat_customers_count": int(c.get("repeat_customers_count") or 0),
                    "last_refill_formatted": format_date(c.get("last_refill_date")),
                    "color": col_info["color"],
                    "color_badge": col_info["badge"],
                    "color_dot": col_info["dot"],
                    "est_cycle_days": cycle_days,
                    "restock_recommendation": restock_rec,
                    "status": "Possible match",
                    "match_type": "Possible match",
                    "is_confirmed": False,
                    "confidence_label": "Possible Match (Sales Co-purchase)",
                    "match_disclaimer": "Purchased by customers who bought this printer. Co-purchase history does not guarantee hardware compatibility.",
                }

                if ink_id in confirmed_ids:
                    # Enrich confirmed record with sales data
                    for conf in confirmed_inks:
                        if conf["ink_id"] == ink_id:
                            conf["total_units_bought"] = item["total_units_bought"]
                            conf["total_spent"] = item["total_spent"]
                            conf["refill_orders_count"] = item["refill_orders_count"]
                            conf["repeat_customers_count"] = item["repeat_customers_count"]
                            conf["est_cycle_days"] = item["est_cycle_days"]
                            conf["restock_recommendation"] = item["restock_recommendation"]
                else:
                    possible_inks.append(item)

            total_ink_spend = sum(c.get("total_spent", 0) for c in (confirmed_inks + possible_inks))
            total_ink_units = sum(c.get("total_units_bought", 0) for c in (confirmed_inks + possible_inks))

            # Backward compatibility: consumables includes all items with match_type clearly stated
            all_consumables = confirmed_inks + possible_inks

            return {
                "printer": {
                    "product_id": printer["PRODUCT_ID"],
                    "product_name": printer["PRODUCT_NAME"],
                    "category": printer["CATEGORY_NAME"],
                    "warehouse_stock": int(printer.get("current_stock") or 0),
                    "sales_price": float(printer.get("SALES_PRICE") or 0),
                    "total_buyers": int(p_stats.get("total_buyers") or 0),
                    "printer_units_sold": int(p_stats.get("printer_units_sold") or 0),
                },
                "confirmed_inks": confirmed_inks,
                "possible_inks": possible_inks,
                "confirmed_count": len(confirmed_inks),
                "possible_count": len(possible_inks),
                "total_ink_units": total_ink_units,
                "total_ink_spend": round(total_ink_spend, 2),
                "consumables_count": len(all_consumables),
                "consumables": all_consumables,
            }


def get_ink_printer_matrix(ink_id: str) -> Dict[str, Any]:
    """
    Report 03 (Part B): Ink to Printer Dependency.
    Returns:
    - ink: Basic consumable info & sales metrics.
    - confirmed_printers: Printer models confirmed compatible by Admin (Admin confirmed).
    - possible_printers: Printer models owned by buyers of this ink in sales history (Possible match).
      NOTE: Co-purchase does not guarantee hardware compatibility.
    - printers: Combined list (confirmed first) for backward compatibility.
    - top_buyers: Top customer accounts buying this consumable.
    """
    init_ink_compatibility_table()
    i_id = (ink_id or "").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Consumable info
            cur.execute("""
                SELECT 
                    p.PRODUCT_ID, p.PRODUCT_NAME, COALESCE(c.CATEGORY_NAME, 'Consumable') as CATEGORY_NAME,
                    COALESCE(st.stock_qty, 0) as current_stock, p.SALES_PRICE
                FROM product p
                LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty 
                    FROM stock_non_serial 
                    WHERE PRODUCT_ID = %s
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                WHERE p.PRODUCT_ID = %s
            """, (i_id, i_id))
            ink = cur.fetchone()
            if not ink:
                return {}
            clean_record(ink)

            # 2. Total buyers & volume for this ink
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT sd.CS_ID) as total_buyers,
                    COALESCE(SUM(sd.QTY), 0) as total_units_sold,
                    ROUND(COALESCE(SUM(sd.QTY * sd.UNIT_PRICE), 0), 2) as total_revenue
                FROM sales_details sd
                WHERE sd.PRODUCT_ID = %s AND sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
            """, (i_id,))
            ink_stats = cur.fetchone() or {}

            # 3. Admin Confirmed Printers
            cur.execute("""
                SELECT 
                    c.id as relation_id,
                    c.printer_id,
                    c.ink_id,
                    c.status,
                    c.source,
                    c.notes,
                    c.created_by,
                    c.updated_by,
                    c.created_at,
                    c.updated_at,
                    p.PRODUCT_NAME as printer_name,
                    COALESCE(cat.CATEGORY_NAME, 'PRINTER') as category_name,
                    COALESCE(st.stock_qty, 0) as printer_stock,
                    COALESCE(p.SALES_PRICE, 0) as sales_price
                FROM printer_ink_compatibility c
                JOIN product p ON c.printer_id = p.PRODUCT_ID
                LEFT JOIN category cat ON p.CATEGORY_ID = cat.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial
                    GROUP BY PRODUCT_ID
                ) st ON p.PRODUCT_ID = st.PRODUCT_ID
                WHERE c.ink_id = %s AND c.status = 'active'
                ORDER BY c.created_at DESC
            """, (i_id,))
            confirmed_raw = cur.fetchall()
            confirmed_printers = []
            confirmed_ids = set()

            for cr in confirmed_raw:
                clean_record(cr)
                confirmed_ids.add(cr["printer_id"])
                confirmed_printers.append({
                    "relation_id": cr.get("relation_id"),
                    "printer_id": cr["printer_id"],
                    "printer_name": cr["printer_name"],
                    "category_name": cr.get("category_name", "PRINTER"),
                    "printer_stock": int(cr.get("printer_stock") or 0),
                    "sales_price": float(cr.get("sales_price") or 0),
                    "printer_units_owned": 0,
                    "shared_customers_count": 0,
                    "source": cr.get("source") or "Admin confirmed",
                    "notes": cr.get("notes") or "",
                    "updated_at": format_date(cr.get("updated_at") or cr.get("created_at")),
                    "status": "Admin confirmed",
                    "match_type": "Admin confirmed",
                    "is_confirmed": True,
                    "confidence_label": "Admin Confirmed (Verified)",
                })

            # 4. Possible Matches: Printers owned by buyers of this ink from sales history
            cur.execute("""
                WITH buyers AS (
                    SELECT DISTINCT CS_ID FROM sales_details
                    WHERE PRODUCT_ID = %s AND CS_ID IS NOT NULL AND CS_ID != ''
                )
                SELECT
                    p2.PRODUCT_ID as printer_id,
                    p2.PRODUCT_NAME as printer_name,
                    COALESCE(c2.CATEGORY_NAME, 'PRINTER') as category_name,
                    COALESCE(st2.stock_qty, 0) as printer_stock,
                    COALESCE(SUM(s2.QTY), 0) as printer_units_owned,
                    COUNT(DISTINCT s2.CS_ID) as shared_customers_count,
                    MAX(s2.SALES_DATE) as last_printer_purchase
                FROM sales_details s2
                JOIN buyers b ON s2.CS_ID = b.CS_ID
                JOIN product p2 ON s2.PRODUCT_ID = p2.PRODUCT_ID
                LEFT JOIN category c2 ON p2.CATEGORY_ID = c2.CATEGORY_ID
                LEFT JOIN (
                    SELECT PRODUCT_ID, SUM(STOCK_QTY) as stock_qty
                    FROM stock_non_serial
                    GROUP BY PRODUCT_ID
                ) st2 ON p2.PRODUCT_ID = st2.PRODUCT_ID
                WHERE s2.PRODUCT_ID != %s
                  AND (p2.CATEGORY_ID = 'CAT006' OR p2.PRODUCT_NAME LIKE '%%PRINTER%%')
                GROUP BY p2.PRODUCT_ID, p2.PRODUCT_NAME, category_name, st2.stock_qty
                ORDER BY printer_units_owned DESC
                LIMIT 30
            """, (i_id, i_id))
            printers_raw = cur.fetchall()

            possible_printers = []
            for p in printers_raw:
                clean_record(p)
                printer_id = p["printer_id"]
                item = {
                    "printer_id": printer_id,
                    "printer_name": p["printer_name"],
                    "category_name": p.get("category_name", "PRINTER"),
                    "printer_stock": int(p.get("printer_stock") or 0),
                    "printer_units_owned": int(p.get("printer_units_owned") or 0),
                    "shared_customers_count": int(p.get("shared_customers_count") or 0),
                    "last_printer_formatted": format_date(p.get("last_printer_purchase")),
                    "status": "Possible match",
                    "match_type": "Possible match",
                    "is_confirmed": False,
                    "confidence_label": "Possible Match (Sales Co-purchase)",
                    "match_disclaimer": "Purchased by customers who bought this ink. Co-purchase history does not guarantee hardware compatibility.",
                }

                if printer_id in confirmed_ids:
                    for conf in confirmed_printers:
                        if conf["printer_id"] == printer_id:
                            conf["printer_units_owned"] = item["printer_units_owned"]
                            conf["shared_customers_count"] = item["shared_customers_count"]
                            conf["last_printer_formatted"] = item["last_printer_formatted"]
                else:
                    possible_printers.append(item)

            # Backward compatibility: printers contains confirmed first, then possible
            all_printers = confirmed_printers + possible_printers

            # 5. Top active customers buying this ink
            cur.execute("""
                SELECT 
                    sd.CS_ID as customer_id,
                    COALESCE(c.CS_NAME, 'Client') as customer_name,
                    COALESCE(c.MOBILE, '-') as phone,
                    COALESCE(SUM(sd.QTY), 0) as units_bought,
                    ROUND(COALESCE(SUM(sd.QTY * sd.UNIT_PRICE), 0), 2) as total_spent,
                    COUNT(DISTINCT sd.SALES_ID) as purchase_count,
                    MAX(sd.SALES_DATE) as last_bought_date
                FROM sales_details sd
                LEFT JOIN cs c ON sd.CS_ID = c.CS_ID
                WHERE sd.PRODUCT_ID = %s AND sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
                GROUP BY sd.CS_ID, customer_name, phone
                ORDER BY units_bought DESC
                LIMIT 20
            """, (i_id,))
            buyers = cur.fetchall()

            for b in buyers:
                clean_record(b)
                b["units_bought"] = int(b.get("units_bought") or 0)
                b["total_spent"] = float(b.get("total_spent") or 0)
                b["purchase_count"] = int(b.get("purchase_count") or 0)
                b["last_bought_formatted"] = format_date(b.get("last_bought_date"))

            return {
                "ink": {
                    "product_id": ink["PRODUCT_ID"],
                    "product_name": ink["PRODUCT_NAME"],
                    "category": ink["CATEGORY_NAME"],
                    "warehouse_stock": int(ink.get("current_stock") or 0),
                    "sales_price": float(ink.get("SALES_PRICE") or 0),
                    "total_buyers": int(ink_stats.get("total_buyers") or 0),
                    "total_units_sold": int(ink_stats.get("total_units_sold") or 0),
                    "total_revenue": float(ink_stats.get("total_revenue") or 0),
                },
                "confirmed_printers": confirmed_printers,
                "possible_printers": possible_printers,
                "confirmed_count": len(confirmed_printers),
                "possible_count": len(possible_printers),
                "printers_count": len(all_printers),
                "printers": all_printers,
                "top_buyers": buyers,
            }


# -----------------------------------------------------------------------------
# Refill Due Alerts (Preserved from existing implementation)
# -----------------------------------------------------------------------------

def get_refill_due_alerts() -> List[Dict[str, Any]]:
    """
    Actionable alerts: Customers who previously bought printer or ink,
    whose estimated consumption cycle has passed (> 30 days) without a refill.
    """
    now = time.time()
    if _REFILL_CACHE["data"] and (now - _REFILL_CACHE["ts"]) < _TTL:
        return _REFILL_CACHE["data"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    sd.CS_ID as customer_id,
                    COALESCE(c.CS_NAME, 'Client') as customer_name,
                    COALESCE(c.MOBILE, '-') as phone,
                    COALESCE(c.ADDRESS, '-') as address,
                    p.PRODUCT_NAME as last_product_name,
                    COALESCE(cat.CATEGORY_NAME, 'Consumable') as category_name,
                    MAX(sd.SALES_DATE) as last_purchase_date,
                    DATEDIFF(CURRENT_DATE, MAX(sd.SALES_DATE)) as days_since_purchase,
                    ROUND(SUM(sd.QTY), 0) as total_units_bought
                FROM sales_details sd
                JOIN product p ON sd.PRODUCT_ID = p.PRODUCT_ID
                LEFT JOIN category cat ON p.CATEGORY_ID = cat.CATEGORY_ID
                LEFT JOIN cs c ON sd.CS_ID = c.CS_ID
                WHERE sd.CS_ID IS NOT NULL AND sd.CS_ID != ''
                  AND (p.PRODUCT_NAME LIKE '%TONER%' 
                       OR p.PRODUCT_NAME LIKE '%INK%' 
                       OR p.PRODUCT_NAME LIKE '%CARTRIDGE%' 
                       OR p.CATEGORY_ID = 'CAT006')
                GROUP BY sd.CS_ID, customer_name, phone, address, p.PRODUCT_NAME, category_name
                HAVING days_since_purchase >= 30
                ORDER BY days_since_purchase DESC
                LIMIT 50
            """)
            rows = cur.fetchall()
            for r in rows:
                clean_record(r)
                r["days_since_purchase"] = int(r.get("days_since_purchase") or 0)
                r["total_units_bought"] = int(r.get("total_units_bought") or 0)
                r["last_purchase_formatted"] = format_date(r.get("last_purchase_date"))
                dsp = r["days_since_purchase"]
                if dsp > 90:
                    r["urgency"] = "High (Overdue)"
                    r["urgency_badge"] = "bg-rose-50 text-rose-700 border-rose-200"
                elif dsp >= 60:
                    r["urgency"] = "Medium (Due Now)"
                    r["urgency_badge"] = "bg-amber-50 text-amber-700 border-amber-200"
                else:
                    r["urgency"] = "Upcoming"
                    r["urgency_badge"] = "bg-blue-50 text-blue-700 border-blue-200"
            _REFILL_CACHE.update(data=rows, ts=now)
            return rows
