"""Customer Payment Risk data access for Report 13.

This module only reads existing business tables. Every query runs inside a
``START TRANSACTION READ ONLY`` block (and through the optional read-only
database user), and the calculations live in ``customer_payment_risk_rules``.

Sources
-------
- Customers: ``cs`` (CS_ID, CS_NAME, MOBILE/PHONE, CUSTOMER_TYPE, BALANCE, CREDIT_LIMIT)
- Invoices: ``sales`` headers (one row per SALES_ID, amount = NET_SALES)
- Due dates: ``sales_details.PAYMENT_DATE`` grouped per invoice
- Payments: ``collection`` detail rows. ``collection_master`` holds the same
  money as headers and is deliberately not added again.
- Returns: ``sales_return_details`` lines (linked to an invoice by SALES_ID)
- Other ledger entries: ``journal_vouchar.CSID`` and ``payment.CS_ID``. They
  cannot be matched to invoices, so customers with such entries get Unknown.
- History tables (``*_hist``) are audit copies and are ignored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .connection import get_connection
from . import customer_payment_risk_rules as rules
from .customer_payment_risk_rules import CustomerLedger, InvoiceFacts
from .report_support import can_view_menu, placeholders as _placeholders, read_only_cursor, resolve_menu_id

logger = logging.getLogger("customer_payment_risk")

CUSTOMER_PAYMENT_RISK_MENU_ID = 37
CUSTOMER_PAYMENT_RISK_MENU_TITLE = "Customer Payment Risk"
CUSTOMER_PAYMENT_RISK_SLUGS = {
    "customer-payment-risk",
    "customer_payment_risk",
    "customer payment risk",
    "/customer-payment-risk",
    "/menus/customer-payment-risk",
}

# The meaning of cs.BALANCE and cs.CREDIT_LIMIT has not been confirmed: the
# stored balance does not reconcile with invoices, payments and returns, and
# almost every credit limit is 0. Change these only after the business
# confirms the meaning (see customer_payment_risk_rules for the options).
CS_BALANCE_MEANING = rules.BALANCE_UNVERIFIED
CS_CREDIT_LIMIT_MEANING = rules.CREDIT_LIMIT_UNVERIFIED

DETAIL_PAGE_SIZE = 20
CACHE_TTL_SECONDS = 60  # same short TTL as the other dashboard reports
MIN_REFRESH_SECONDS = 10  # a forced refresh reuses data loaded within this window

_cache_lock = threading.Lock()
_load_lock = threading.Lock()  # one full load at a time; waiting requests reuse its result
_report_cache: Dict[str, Any] = {"as_of": None, "loaded_at": 0.0, "rows": None}

DISCLAIMER = (
    "This report shows recorded invoices, payments and returns only. It does not predict "
    "whether a customer will pay, and it never sends messages or requests payments."
)
CALCULATION_BASIS = (
    "Invoice amount = invoice header net sales (invoice lines are not added, so duplicate lines "
    "are not double counted). Unpaid = invoice amount minus payments and returns linked to that "
    "invoice; it is calculated only when every payment, return and voucher of the customer is "
    "linked to an invoice. Due date = the payment date recorded on every line of the invoice. An "
    "invoice due today becomes overdue the next day. Anything that cannot be established is shown "
    "as Unknown, never as zero."
)
FIELD_NOTES = [
    "Outstanding balance: the customer BALANCE field does not match recorded invoices, payments "
    "and returns, and its sign is not confirmed, so it is shown as Unknown.",
    "Credit limit: the CREDIT_LIMIT field is 0 for almost every customer and it is not confirmed "
    "whether 0 means no limit or no credit, so it is shown as Unknown.",
]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def resolve_customer_payment_risk_menu_id() -> int:
    """Return the sidebar menu ID for Customer Payment Risk (37 when lookup fails)."""
    return resolve_menu_id(
        lambda: get_connection(), "customer payment risk",
        ("customer-payment-risk", "/customer-payment-risk", "/menus/customer-payment-risk"),
        CUSTOMER_PAYMENT_RISK_MENU_ID,
    )


def can_view_customer_payment_risk(user: Mapping[str, Any] | None) -> bool:
    """Allow Admin users, '*' grants, or the Customer Payment Risk menu (slug or ID)."""
    return can_view_menu(user, CUSTOMER_PAYMENT_RISK_SLUGS, resolve_customer_payment_risk_menu_id)


def get_customer_payment_risk_metadata(today: Optional[date] = None) -> Dict[str, Any]:
    as_of = today or date.today()
    return {
        "menu_id": resolve_customer_payment_risk_menu_id(),
        "as_of": as_of.isoformat(),
        "as_of_display": rules.display_date(as_of),
        "disclaimer": DISCLAIMER,
        "calculation_basis": CALCULATION_BASIS,
        "field_notes": FIELD_NOTES,
        "reason_labels": rules.REASON_LABELS,
        "overdue_status_options": [
            {"value": "all", "label": "All statuses"},
            {"value": rules.STATUS_OVERDUE, "label": "Overdue (confirmed)"},
            {"value": rules.STATUS_NOT_OVERDUE, "label": "Not overdue (confirmed)"},
            {"value": rules.STATUS_UNKNOWN, "label": "Unknown"},
            {"value": rules.STATUS_NO_INVOICES, "label": "No invoices"},
        ],
    }


# ---------------------------------------------------------------------------
# Read-only access
# ---------------------------------------------------------------------------

def _read_only_cursor():
    # The lambda looks up get_connection at call time so tests can patch this module.
    return read_only_cursor(lambda: get_connection())


def _fetch_customers(cur, customer_id: Optional[str]) -> List[Dict[str, Any]]:
    where, params = ("WHERE CS_ID = %s", (customer_id,)) if customer_id else ("", ())
    cur.execute(f"""
        SELECT CS_ID, CS_NAME, MOBILE, PHONE, CUSTOMER_TYPE, BALANCE, CREDIT_LIMIT
        FROM cs {where}
    """, params)
    return cur.fetchall()


def _fetch_invoices(cur, customer_id: Optional[str]) -> List[Dict[str, Any]]:
    where, params = ("CS_ID = %s", (customer_id,)) if customer_id else ("CS_ID IS NOT NULL AND CS_ID <> ''", ())
    cur.execute(f"""
        SELECT SALES_ID, CS_ID, SALES_DATE, NET_SALES, SALES_TYPE
        FROM sales WHERE {where}
    """, params)
    return cur.fetchall()


def _fetch_invoice_lines(cur, sales_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if sales_ids is not None and not sales_ids:
        return []
    where = f"WHERE SALES_ID IN ({_placeholders(sales_ids)})" if sales_ids is not None else ""
    cur.execute(f"""
        SELECT SALES_ID, COUNT(*) AS line_count, COUNT(PAYMENT_DATE) AS dated_line_count,
               MIN(PAYMENT_DATE) AS due_min, MAX(PAYMENT_DATE) AS due_max
        FROM sales_details {where}
        GROUP BY SALES_ID
    """, tuple(sales_ids or ()))
    return cur.fetchall()


def _fetch_linked_payments(cur, customer_id: Optional[str], sales_ids: Sequence[str]) -> List[Dict[str, Any]]:
    linked = "SALES_ID IS NOT NULL AND SALES_ID <> ''"
    if customer_id is None:
        cur.execute(f"SELECT CS_ID, SALES_ID, AMOUNT, CHEQUE_STATUS FROM collection WHERE {linked}")
        return cur.fetchall()
    scope = "CS_ID = %s" + (f" OR SALES_ID IN ({_placeholders(sales_ids)})" if sales_ids else "")
    cur.execute(f"""
        SELECT CS_ID, SALES_ID, AMOUNT, CHEQUE_STATUS FROM collection
        WHERE {linked} AND ({scope})
    """, (customer_id, *sales_ids))
    return cur.fetchall()


def _fetch_payment_totals(cur, customer_id: Optional[str]) -> List[Dict[str, Any]]:
    where, params = ("WHERE CS_ID = %s", (customer_id,)) if customer_id else ("", ())
    cur.execute(f"""
        SELECT CS_ID, COUNT(*) AS row_count, SUM(AMOUNT) AS total_amount,
               SUM(CASE WHEN SALES_ID IS NULL OR SALES_ID = '' THEN 1 ELSE 0 END) AS unlinked_rows,
               SUM(CASE WHEN SALES_ID IS NULL OR SALES_ID = '' THEN AMOUNT ELSE 0 END) AS unlinked_amount,
               MAX(COLLECTION_DATE) AS last_date
        FROM collection {where}
        GROUP BY CS_ID
    """, params)
    return cur.fetchall()


def _fetch_return_lines(cur, customer_id: Optional[str], sales_ids: Sequence[str]) -> List[Dict[str, Any]]:
    if customer_id is None:
        cur.execute("SELECT CS_ID, SALES_ID, TOTAL_PRICE FROM sales_return_details")
        return cur.fetchall()
    scope = "CS_ID = %s" + (f" OR SALES_ID IN ({_placeholders(sales_ids)})" if sales_ids else "")
    cur.execute(f"SELECT CS_ID, SALES_ID, TOTAL_PRICE FROM sales_return_details WHERE {scope}",
                (customer_id, *sales_ids))
    return cur.fetchall()


def _fetch_other_ledger_counts(cur, customer_id: Optional[str]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for table, column in (("journal_vouchar", "CSID"), ("payment", "CS_ID")):
        if customer_id:
            cur.execute(f"SELECT {column} AS cs_id, COUNT(*) AS n FROM {table} WHERE {column} = %s GROUP BY {column}",
                        (customer_id,))
        else:
            cur.execute(f"""SELECT {column} AS cs_id, COUNT(*) AS n FROM {table}
                            WHERE {column} IS NOT NULL AND {column} <> '' GROUP BY {column}""")
        for row in cur.fetchall():
            counts[str(row["cs_id"])] += int(row["n"] or 0)
    return counts


# ---------------------------------------------------------------------------
# Building ledgers
# ---------------------------------------------------------------------------

def _is_linked(sales_id: Any) -> bool:
    return bool(str(sales_id or "").strip())


def _allocate_payments(payments, invoice_owner: Mapping[str, str]) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    """Group linked payments per invoice and count payer rows pointing at someone else's invoice."""
    per_invoice: Dict[str, Dict[str, int]] = defaultdict(lambda: {"cents": 0, "rows": 0, "unconfirmed": 0})
    mislinked: Dict[str, int] = defaultdict(int)
    for row in payments:
        sales_id, payer = str(row["SALES_ID"]), str(row.get("CS_ID") or "")
        if sales_id in invoice_owner:
            slot = per_invoice[sales_id]
            slot["cents"] += rules.to_cents(row.get("AMOUNT")) or 0
            slot["rows"] += 1
            if str(row.get("CHEQUE_STATUS") or "").strip():
                slot["unconfirmed"] += 1
        if payer and invoice_owner.get(sales_id) != payer:
            mislinked[payer] += 1
    return per_invoice, mislinked


def _allocate_returns(returns, invoice_owner: Mapping[str, str]) -> Tuple[Dict[str, int], Dict[str, Dict[str, int]]]:
    per_invoice: Dict[str, int] = defaultdict(int)
    per_customer: Dict[str, Dict[str, int]] = defaultdict(lambda: {"cents": 0, "unlinked": 0})
    for row in returns:
        cents = rules.to_cents(row.get("TOTAL_PRICE")) or 0
        sales_id, customer = str(row.get("SALES_ID") or "").strip(), str(row.get("CS_ID") or "")
        if sales_id in invoice_owner:
            per_invoice[sales_id] += cents
        if customer:
            per_customer[customer]["cents"] += cents
            if not _is_linked(sales_id) or invoice_owner.get(sales_id) != customer:
                per_customer[customer]["unlinked"] += 1
    return per_invoice, per_customer


def _build_ledgers(customers, invoices, lines, payments, payment_totals, returns, other_counts) -> List[CustomerLedger]:
    invoice_owner = {str(row["SALES_ID"]): str(row["CS_ID"]) for row in invoices}
    line_facts = {str(row["SALES_ID"]): row for row in lines}
    paid, mislinked = _allocate_payments(payments, invoice_owner)
    returned, returns_by_customer = _allocate_returns(returns, invoice_owner)
    totals = {str(row["CS_ID"]): row for row in payment_totals if row.get("CS_ID")}

    invoices_by_customer: Dict[str, List[InvoiceFacts]] = defaultdict(list)
    for row in invoices:
        sales_id = str(row["SALES_ID"])
        line = line_facts.get(sales_id, {})
        payment = paid.get(sales_id, {"cents": 0, "rows": 0, "unconfirmed": 0})
        invoices_by_customer[str(row["CS_ID"])].append(InvoiceFacts(
            sales_id=sales_id,
            sales_date=rules.to_date(row.get("SALES_DATE")),
            amount_cents=rules.to_cents(row.get("NET_SALES")),
            sales_type=row.get("SALES_TYPE"),
            line_count=int(line.get("line_count") or 0),
            dated_line_count=int(line.get("dated_line_count") or 0),
            due_min=rules.to_date(line.get("due_min")),
            due_max=rules.to_date(line.get("due_max")),
            payment_cents=payment["cents"],
            payment_rows=payment["rows"],
            unconfirmed_payment_rows=payment["unconfirmed"],
            return_cents=returned.get(sales_id, 0),
        ))

    ledgers = []
    for row in customers:
        customer_id = str(row["CS_ID"])
        total = totals.get(customer_id, {})
        customer_returns = returns_by_customer.get(customer_id, {"cents": 0, "unlinked": 0})
        phone = (str(row.get("MOBILE") or "").strip() or str(row.get("PHONE") or "").strip()) or None
        ledgers.append(CustomerLedger(
            customer_id=customer_id,
            name=row.get("CS_NAME"),
            phone=phone,
            customer_type=(str(row.get("CUSTOMER_TYPE") or "").strip() or None),
            balance_raw=row.get("BALANCE"),
            credit_limit_raw=row.get("CREDIT_LIMIT"),
            invoices=tuple(invoices_by_customer.get(customer_id, ())),
            collected_cents=rules.to_cents(total.get("total_amount")) or 0,
            collection_rows=int(total.get("row_count") or 0),
            unlinked_payment_rows=int(total.get("unlinked_rows") or 0) + mislinked.get(customer_id, 0),
            unlinked_payment_cents=rules.to_cents(total.get("unlinked_amount")) or 0,
            last_payment_date=rules.to_date(total.get("last_date")),
            returned_cents=customer_returns["cents"],
            unlinked_return_rows=customer_returns["unlinked"],
            other_ledger_rows=other_counts.get(customer_id, 0),
        ))
    return ledgers


def _load_ledgers(cur, customer_id: Optional[str] = None) -> List[CustomerLedger]:
    customers = _fetch_customers(cur, customer_id)
    if customer_id and not customers:
        return []
    invoices = _fetch_invoices(cur, customer_id)
    sales_ids = [str(row["SALES_ID"]) for row in invoices] if customer_id else None
    return _build_ledgers(
        customers,
        invoices,
        _fetch_invoice_lines(cur, sales_ids),
        _fetch_linked_payments(cur, customer_id, sales_ids or []),
        _fetch_payment_totals(cur, customer_id),
        _fetch_return_lines(cur, customer_id, sales_ids or []),
        _fetch_other_ledger_counts(cur, customer_id),
    )


def _evaluate(ledger: CustomerLedger, today: date) -> Dict[str, Any]:
    return rules.evaluate_customer(ledger, today, CS_BALANCE_MEANING, CS_CREDIT_LIMIT_MEANING)


def _data_through(rows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    dates = [d for row in rows for d in (row.get("last_invoice_date"), row.get("last_payment_date")) if d]
    return max(dates) if dates else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_customer_payment_risk_filter_options() -> Dict[str, Any]:
    with _read_only_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT UPPER(TRIM(CUSTOMER_TYPE)) AS customer_type FROM cs
            WHERE CUSTOMER_TYPE IS NOT NULL AND TRIM(CUSTOMER_TYPE) <> ''
            ORDER BY customer_type
        """)
        types = [row["customer_type"] for row in cur.fetchall()]
    return {"customer_types": types}


def clear_customer_payment_risk_cache() -> None:
    with _cache_lock:
        _report_cache.update(as_of=None, loaded_at=0.0, rows=None)


def _cached_rows(as_of: date, max_age: float) -> Optional[Tuple[List[Dict[str, Any]], float]]:
    with _cache_lock:
        rows, loaded_at = _report_cache["rows"], _report_cache["loaded_at"]
        if rows is not None and _report_cache["as_of"] == as_of and time.time() - loaded_at < max_age:
            return rows, loaded_at
    return None


def _evaluated_rows(as_of: date, refresh: bool) -> Tuple[List[Dict[str, Any]], float]:
    """Return evaluated customer rows from a short-lived cache, loading at most one copy at a time."""
    max_age = MIN_REFRESH_SECONDS if refresh else CACHE_TTL_SECONDS
    cached = _cached_rows(as_of, max_age)
    if cached:
        return cached
    with _load_lock:
        cached = _cached_rows(as_of, max_age)  # another request may have just loaded it
        if cached:
            return cached
        started = time.time()
        with _read_only_cursor() as cur:
            ledgers = _load_ledgers(cur)
        rows = [{key: val for key, val in _evaluate(ledger, as_of).items() if key != "_invoices"} for ledger in ledgers]
        with _cache_lock:
            _report_cache.update(as_of=as_of, loaded_at=started, rows=rows)
        return rows, started


def get_customer_payment_risk_report(today: Optional[date] = None, refresh: bool = False, **raw_filters: Any) -> Dict[str, Any]:
    """Return the filtered, sorted and paginated report with summary cards."""
    filters = rules.normalize_report_filters(raw_filters)
    as_of = today or date.today()
    evaluated, loaded_at = _evaluated_rows(as_of, refresh)
    selected = rules.sort_customers(rules.filter_customers(evaluated, filters), filters["sort_by"], filters["sort_order"])
    page_rows, pagination = rules.paginate(selected, filters["page"], filters["page_size"])
    data_through = _data_through(evaluated)
    return {
        "success": True,
        "as_of": as_of.isoformat(),
        "as_of_display": rules.display_date(as_of),
        "loaded_at_display": datetime.fromtimestamp(loaded_at).strftime("%H:%M:%S"),
        "data_through": data_through,
        "data_through_display": rules.display_date(rules.to_date(data_through)),
        "filters": filters,
        "summary": rules.summarize(selected),
        "customers": [rules.public_row(row) for row in page_rows],
        "pagination": pagination,
    }


def _payment_rows(cur, customer_id: str) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT COLLECTION_ID, COLLECTION_DATE, COLLECTION_TYPE, AMOUNT, SALES_ID, CHEQUE_STATUS
        FROM collection WHERE CS_ID = %s
        ORDER BY COLLECTION_DATE DESC, COLLECTION_ID DESC
    """, (customer_id,))
    rows = []
    for row in cur.fetchall():
        cents = rules.to_cents(row.get("AMOUNT"))
        status_code = str(row.get("CHEQUE_STATUS") or "").strip()
        linked = str(row.get("SALES_ID") or "").strip() or None
        rows.append({
            "collection_id": row.get("COLLECTION_ID"),
            "date": rules.iso_date(rules.to_date(row.get("COLLECTION_DATE"))),
            "date_display": rules.display_date(rules.to_date(row.get("COLLECTION_DATE"))),
            "type": (str(row.get("COLLECTION_TYPE") or "").strip() or rules.UNKNOWN),
            "amount": rules.cents_to_amount(cents),
            "amount_display": rules.format_money(cents),
            "linked_invoice": linked,
            "linked_invoice_display": linked or "Not linked",
            "status_label": f"Status code {status_code} (not confirmed)" if status_code else "Recorded",
        })
    return rows


def _return_rows(cur, customer_id: str) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT SALES_RETURN_ID, MAX(RETURN_DATE) AS return_date, SALES_ID, SUM(TOTAL_PRICE) AS amount
        FROM sales_return_details WHERE CS_ID = %s
        GROUP BY SALES_RETURN_ID, SALES_ID
        ORDER BY return_date DESC, SALES_RETURN_ID DESC
    """, (customer_id,))
    rows = []
    for row in cur.fetchall():
        cents = rules.to_cents(row.get("amount"))
        linked = str(row.get("SALES_ID") or "").strip() or None
        rows.append({
            "return_id": row.get("SALES_RETURN_ID"),
            "date": rules.iso_date(rules.to_date(row.get("return_date"))),
            "date_display": rules.display_date(rules.to_date(row.get("return_date"))),
            "linked_invoice": linked,
            "linked_invoice_display": linked or "Not linked",
            "amount": rules.cents_to_amount(cents),
            "amount_display": rules.format_money(cents),
        })
    return rows


def _page_of(rows: Sequence[Any], page: Any, key: str) -> Tuple[List[Any], Dict[str, Any]]:
    return rules.paginate(rows, rules.parse_page(page, key), DETAIL_PAGE_SIZE)


def get_customer_payment_risk_detail(
    customer_id: Any,
    invoice_page: Any = 1,
    payment_page: Any = 1,
    return_page: Any = 1,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Return one customer's summary plus invoice, payment and return history."""
    clean_id = rules.validate_customer_id(customer_id)
    pages = {key: rules.parse_page(value, key) for key, value in
             (("invoice_page", invoice_page), ("payment_page", payment_page), ("return_page", return_page))}
    as_of = today or date.today()
    with _read_only_cursor() as cur:
        ledgers = _load_ledgers(cur, clean_id)
        if not ledgers:
            raise rules.CustomerNotFoundError("Customer not found.")
        payments = _payment_rows(cur, clean_id)
        returns = _return_rows(cur, clean_id)
    ledger = ledgers[0]
    row = _evaluate(ledger, as_of)
    invoices = [rules.public_row(inv) for inv in row["_invoices"]]
    customer = rules.public_row(row)
    customer.update({
        "recorded_balance": rules.cents_to_amount(rules.to_cents(ledger.balance_raw)),
        "recorded_credit_limit": rules.cents_to_amount(rules.to_cents(ledger.credit_limit_raw)),
        "field_notes": FIELD_NOTES,
    })
    invoice_slice, invoice_pagination = _page_of(invoices, pages["invoice_page"], "invoice_page")
    payment_slice, payment_pagination = _page_of(payments, pages["payment_page"], "payment_page")
    return_slice, return_pagination = _page_of(returns, pages["return_page"], "return_page")
    return {
        "success": True,
        "as_of": as_of.isoformat(),
        "as_of_display": rules.display_date(as_of),
        "customer": customer,
        "invoices": invoice_slice,
        "invoice_pagination": invoice_pagination,
        "payments": payment_slice,
        "payment_pagination": payment_pagination,
        "returns": return_slice,
        "return_pagination": return_pagination,
        "calculation_basis": CALCULATION_BASIS,
    }
