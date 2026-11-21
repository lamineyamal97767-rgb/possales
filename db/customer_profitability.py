"""Customer Profitability data access for Report 14 (read-only).

Sources
-------
- Sale lines: ``sales_details`` (QTY, UNIT_PRICE, ORG_COST, SALES_DATE, CS_ID, PRODUCT_ID),
  grouped per invoice, product, cost validity and price validity.
- Invoice headers: ``sales`` (TOTAL_PRICE to check the lines, DISCOUNT to split across lines).
- Returns: ``sales_return_details`` (RETURN_DATE, QTY, TOTAL_PRICE, ORG_COST).
- Names: ``cs``, ``product`` and ``category``.

Every query runs inside ``START TRANSACTION READ ONLY`` and the calculations
live in ``customer_profitability_rules``.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .connection import get_connection
from . import customer_profitability_rules as rules
from .customer_profitability_rules import InvoiceHeader, ReturnFact, SaleFact
from .report_support import (
    UNKNOWN,
    CustomerNotFoundError,
    ReportInputError,
    can_view_menu,
    display_date,
    iso_date,
    paginate,
    public_row,
    read_only_cursor,
    resolve_menu_id,
    to_cents,
    to_date,
)

CUSTOMER_PROFITABILITY_MENU_ID = 38
CUSTOMER_PROFITABILITY_SLUGS = {
    "customer-profitability",
    "customer_profitability",
    "customer profitability",
    "/customer-profitability",
    "/menus/customer-profitability",
}
VIEWS = ("customers", "products")
CACHE_TTL_SECONDS = 60
MIN_REFRESH_SECONDS = 10  # a forced refresh reuses data loaded within this window
CACHE_MAX_ENTRIES = 8

DISCLAIMER = (
    "Gross profit is net sales minus the recorded cost of the goods sold. It is not net profit: "
    "operating expenses, commissions, delivery and other overheads are not deducted. Gross profit "
    "is shown only when every sale and return in the row has a recorded price and cost; otherwise "
    "it is shown as Unknown."
)
CALCULATION_BASIS = (
    "Sales = quantity x unit price of each invoice line, less the invoice discount split across the "
    "invoice's lines in proportion to their amounts. Service and other invoice charges are not product "
    "sales. Cost of sales = quantity x ORG_COST recorded on each sale line when it was sold. Returns "
    "are counted on their return date at the amount and ORG_COST recorded on the return (the same cost "
    "as the original sale line). Gross profit = (sales - returns) - (sale cost - returned cost). Gross "
    "margin = gross profit / net sales. Gross profit on costed lines adds up only lines with a recorded "
    "cost and is a partial figure."
)
COLUMN_LABELS = {
    "sales": "Sales",
    "returns": "Returns",
    "net_sales": "Net sales",
    "cost_of_sales": "Cost of sales",
    "gross_profit": "Gross profit",
    "gross_margin": "Gross margin",
    "cost_coverage": "Cost coverage",
    "costed_gross_profit": "Gross profit on costed lines",
}

_cache_lock = threading.Lock()
_load_lock = threading.Lock()  # one heavy load at a time; waiting requests reuse its result
_facts_cache: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}


# ---------------------------------------------------------------------------
# Permissions and metadata
# ---------------------------------------------------------------------------

def resolve_customer_profitability_menu_id() -> int:
    """Return the sidebar menu ID for Customer Profitability (38 when lookup fails)."""
    return resolve_menu_id(
        lambda: get_connection(), "customer profitability",
        ("customer-profitability", "/customer-profitability", "/menus/customer-profitability"),
        CUSTOMER_PROFITABILITY_MENU_ID,
    )


def can_view_customer_profitability(user: Mapping[str, Any] | None) -> bool:
    return can_view_menu(user, CUSTOMER_PROFITABILITY_SLUGS, resolve_customer_profitability_menu_id)


def get_customer_profitability_metadata(today: Optional[date] = None) -> Dict[str, Any]:
    end = today or date.today()
    start = end - timedelta(days=rules.DEFAULT_PERIOD_DAYS - 1)
    return {
        "menu_id": resolve_customer_profitability_menu_id(),
        "default_start_date": start.isoformat(),
        "default_end_date": end.isoformat(),
        "max_period_days": rules.MAX_PERIOD_DAYS,
        "disclaimer": DISCLAIMER,
        "calculation_basis": CALCULATION_BASIS,
        "column_labels": COLUMN_LABELS,
        "reason_labels": rules.REASON_LABELS,
    }


def _read_only_cursor():
    # The lambda looks up get_connection at call time so tests can patch this module.
    return read_only_cursor(lambda: get_connection())


def clear_customer_profitability_cache() -> None:
    with _cache_lock:
        _facts_cache.clear()


def get_customer_profitability_data_range() -> Dict[str, Any]:
    with _read_only_cursor() as cur:
        cur.execute("SELECT MIN(SALES_DATE) AS first_date, MAX(SALES_DATE) AS last_date FROM sales")
        row = cur.fetchone() or {}
    first, last = to_date(row.get("first_date")), to_date(row.get("last_date"))
    return {
        "first_date": iso_date(first),
        "last_date": iso_date(last),
        "display": f"{display_date(first)} – {display_date(last)}" if first and last else UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _customer_clause(customer_id: Optional[str]) -> Tuple[str, Tuple[Any, ...]]:
    return (" AND CS_ID = %s", (customer_id,)) if customer_id else ("", ())


def _fetch_sale_facts(cur, start: date, end: date, customer_id: Optional[str]) -> List[SaleFact]:
    clause, params = _customer_clause(customer_id)
    cur.execute(f"""
        SELECT SALES_ID, CS_ID, PRODUCT_ID, MIN(SALES_DATE) AS sale_date,
               COUNT(*) AS line_count, SUM(QTY) AS quantity,
               SUM(QTY * UNIT_PRICE) AS gross_amount, SUM(QTY * ORG_COST) AS cost_amount,
               SUM(CASE WHEN UNIT_PRICE = 0 THEN 1 ELSE 0 END) AS zero_price_lines,
               CASE WHEN ORG_COST > 0 THEN 1 ELSE 0 END AS cost_known,
               CASE WHEN UNIT_PRICE >= 0 AND QTY > 0 THEN 1 ELSE 0 END AS price_ok
        FROM sales_details
        WHERE SALES_DATE >= %s AND SALES_DATE < %s{clause}
        GROUP BY SALES_ID, CS_ID, PRODUCT_ID, cost_known, price_ok
    """, (start, end + timedelta(days=1), *params))
    return [SaleFact(
        sales_id=str(row["SALES_ID"]),
        customer_id=str(row.get("CS_ID") or ""),
        product_id=str(row.get("PRODUCT_ID") or ""),
        sale_date=to_date(row.get("sale_date")),
        quantity=float(row.get("quantity") or 0),
        line_count=int(row.get("line_count") or 0),
        gross_cents=to_cents(row.get("gross_amount")) if row.get("price_ok") else None,
        cost_cents=to_cents(row.get("cost_amount")) if row.get("cost_known") else None,
        zero_price_lines=int(row.get("zero_price_lines") or 0),
    ) for row in cur.fetchall()]


def _fetch_headers(cur, start: date, end: date, customer_id: Optional[str]) -> Dict[str, InvoiceHeader]:
    clause, params = _customer_clause(customer_id)
    cur.execute(f"""
        SELECT SALES_ID, TOTAL_PRICE, DISCOUNT FROM sales
        WHERE SALES_DATE >= %s AND SALES_DATE < %s{clause}
    """, (start, end + timedelta(days=1), *params))
    return {str(row["SALES_ID"]): InvoiceHeader(str(row["SALES_ID"]), to_cents(row.get("TOTAL_PRICE")),
                                                to_cents(row.get("DISCOUNT")) or 0)
            for row in cur.fetchall()}


def _fetch_returns(cur, start: date, end: date, customer_id: Optional[str]) -> List[ReturnFact]:
    clause, params = _customer_clause(customer_id)
    cur.execute(f"""
        SELECT CS_ID, PRODUCT_ID, RETURN_DATE, QTY, TOTAL_PRICE, ORG_COST FROM sales_return_details
        WHERE RETURN_DATE >= %s AND RETURN_DATE < %s{clause}
    """, (start, end + timedelta(days=1), *params))
    facts = []
    for row in cur.fetchall():
        quantity = float(row.get("QTY") or 0)
        amount = to_cents(row.get("TOTAL_PRICE"))
        unit_cost = row.get("ORG_COST")
        cost = to_cents(quantity * float(unit_cost)) if unit_cost is not None and float(unit_cost) > 0 and quantity > 0 else None
        facts.append(ReturnFact(
            customer_id=str(row.get("CS_ID") or ""),
            product_id=str(row.get("PRODUCT_ID") or ""),
            return_date=to_date(row.get("RETURN_DATE")),
            quantity=quantity,
            amount_cents=amount if amount is not None and amount >= 0 else None,
            cost_cents=cost,
        ))
    return facts


def _fetch_customer(cur, customer_id: str) -> Optional[Dict[str, Any]]:
    cur.execute("SELECT CS_ID, CS_NAME, MOBILE, PHONE, CUSTOMER_TYPE FROM cs WHERE CS_ID = %s LIMIT 1", (customer_id,))
    return cur.fetchone()


def _fetch_customer_names(cur) -> Dict[str, Dict[str, Any]]:
    cur.execute("SELECT CS_ID, CS_NAME, MOBILE, PHONE, CUSTOMER_TYPE FROM cs")
    return {str(row["CS_ID"]): row for row in cur.fetchall()}


def _fetch_product_names(cur) -> Dict[str, Dict[str, Any]]:
    cur.execute("""
        SELECT p.PRODUCT_ID, p.PRODUCT_NAME, c.CATEGORY_NAME
        FROM product p LEFT JOIN category c ON c.CATEGORY_ID = p.CATEGORY_ID
    """)
    return {str(row["PRODUCT_ID"]): row for row in cur.fetchall()}


def _cached_facts(key: Tuple[Any, ...], max_age: float):
    with _cache_lock:
        cached = _facts_cache.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
    return None


def _store_facts(key: Tuple[Any, ...], loaded_at: float, facts) -> None:
    with _cache_lock:
        if key not in _facts_cache and len(_facts_cache) >= CACHE_MAX_ENTRIES:
            _facts_cache.pop(min(_facts_cache, key=lambda k: _facts_cache[k][0]))
        _facts_cache[key] = (loaded_at, facts)


def _load_facts(cur, filters: Mapping[str, Any], refresh: bool = False):
    """Return (net sales, returns) for the period from a short-lived cache, loading one copy at a time."""
    key = (filters["start_date"], filters["end_date"], filters["customer_id"])
    max_age = MIN_REFRESH_SECONDS if refresh else CACHE_TTL_SECONDS
    cached = _cached_facts(key, max_age)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _cached_facts(key, max_age)  # another request may have just loaded it
        if cached is not None:
            return cached
        started = time.monotonic()
        start, end, customer_id = key
        sales = rules.apply_invoice_discounts(_fetch_sale_facts(cur, start, end, customer_id),
                                              _fetch_headers(cur, start, end, customer_id))
        facts = (tuple(sales), tuple(_fetch_returns(cur, start, end, customer_id)))
        _store_facts(key, started, facts)
        return facts


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _customer_row(customer_id: str, totals: rules.GroupTotals, names: Mapping[str, Any]) -> Dict[str, Any]:
    info = names.get(customer_id, {})
    name = _text(info.get("CS_NAME"))
    phone = _text(info.get("MOBILE")) or _text(info.get("PHONE"))
    return {
        "customer_id": customer_id or None,
        "customer_name": name or UNKNOWN,
        "phone": phone,
        "phone_display": phone or UNKNOWN,
        "customer_type": _text(info.get("CUSTOMER_TYPE")),
        **rules.evaluate_group(totals),
        "_name_sort": name.casefold() if name else None,
        "_id_sort": customer_id,
        "_totals": totals,
    }


def _product_row(product_id: str, totals: rules.GroupTotals, names: Mapping[str, Any]) -> Dict[str, Any]:
    info = names.get(product_id, {})
    name = _text(info.get("PRODUCT_NAME"))
    return {
        "product_id": product_id or None,
        "product_name": name or UNKNOWN,
        "category_name": _text(info.get("CATEGORY_NAME")) or UNKNOWN,
        **rules.evaluate_group(totals),
        "_name_sort": name.casefold() if name else None,
        "_id_sort": product_id,
        "_totals": totals,
    }


def _customer_summary(row: Optional[Mapping[str, Any]], customer_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not customer_id:
        return None
    phone = _text(row.get("MOBILE")) or _text(row.get("PHONE"))
    return {
        "customer_id": customer_id,
        "customer_name": _text(row.get("CS_NAME")) or UNKNOWN,
        "phone_display": phone or UNKNOWN,
        "customer_type": _text(row.get("CUSTOMER_TYPE")),
    }


def get_customer_profitability_report(view: str = "customers", today: Optional[date] = None,
                                      refresh: bool = False, **raw_filters: Any) -> Dict[str, Any]:
    """Return gross profit rows by customer or by product, with summary and pagination.

    The summary covers the rows that match every filter (period, customer, search and
    gross profit status); ``summary_filtered`` tells the page when search or status narrowed it.
    """
    if view not in VIEWS:
        raise ReportInputError("Invalid view. Use customers or products.")
    filters = rules.normalize_filters(raw_filters, today or date.today())
    customer_id = filters["customer_id"]
    with _read_only_cursor() as cur:
        customer = _fetch_customer(cur, customer_id) if customer_id else None
        if customer_id and not customer:
            raise CustomerNotFoundError("Customer not found.")
        sales, returns = _load_facts(cur, filters, refresh)
        names = _fetch_customer_names(cur) if view == "customers" else _fetch_product_names(cur)
    if view == "customers":
        groups = rules.aggregate(sales, returns, key=lambda fact: fact.customer_id)
        rows = [_customer_row(key, totals, names) for key, totals in groups.items()]
    else:
        groups = rules.aggregate(sales, returns, key=lambda fact: fact.product_id)
        rows = [_product_row(key, totals, names) for key, totals in groups.items()]
    selected = rules.sort_rows(rules.filter_rows(rows, filters), filters["sort_by"], filters["sort_order"])
    page_rows, pagination = paginate(selected, filters["page"], filters["page_size"])
    return {
        "success": True,
        "view": view,
        "period": rules.period(filters),
        "filters": rules.public_filters(filters),
        "customer": _customer_summary(customer, customer_id),
        "summary": rules.summarize([row["_totals"] for row in selected], selected),
        "summary_filtered": bool(filters["q"]) or filters["profit_status"] != "all",
        "rows": [public_row(row) for row in page_rows],
        "pagination": pagination,
    }
