"""Calculation rules for Report 13: Customer Payment Risk.

This module is pure: it never touches the database. It receives invoice and
payment facts that ``db.customer_payment_risk`` read from the existing tables
and decides what can and cannot be stated about each customer.

Rules
-----
- Money is handled in integer cents so float storage noise never changes a
  paid / unpaid decision.
- An invoice amount is the invoice header (``sales.NET_SALES``). Invoice lines
  are only used for the due date, so duplicate lines are never added twice.
- Unpaid = invoice amount - payments linked to the invoice - returns linked to
  the invoice. It is only calculated when every payment, return and ledger
  entry of the customer can be traced to an invoice and no linked cheque or
  transfer is still waiting for confirmation. Otherwise it is Unknown.
- The due date comes from ``sales_details.PAYMENT_DATE`` and is used only when
  every line of the invoice carries the same date.
- An invoice due today is not overdue; it becomes overdue the next day.
- Unknown values stay ``None`` (shown as "Unknown"). They are never turned
  into zero, and they sort after known values in both directions.
- No risk score is produced and nothing predicts whether a customer will pay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .report_support import (  # noqa: F401  (re-exported for callers and tests)
    UNKNOWN,
    CustomerNotFoundError,
    ReportInputError,
    cents_to_amount,
    display_date,
    format_money,
    iso_date,
    paginate,
    public_row,
    sort_known_first,
    to_cents,
    to_date,
)

STATUS_OVERDUE = "overdue"
STATUS_NOT_OVERDUE = "not_overdue"
STATUS_UNKNOWN = "unknown"
STATUS_NO_INVOICES = "no_invoices"
STATUS_LABELS = {
    STATUS_OVERDUE: "Overdue",
    STATUS_NOT_OVERDUE: "Not overdue",
    STATUS_UNKNOWN: "Unknown",
    STATUS_NO_INVOICES: "No invoices",
}

BALANCE_UNVERIFIED = "unverified"
BALANCE_RECEIVABLE_POSITIVE = "receivable_positive"
CREDIT_LIMIT_UNVERIFIED = "unverified"
CREDIT_LIMIT_AMOUNT = "amount_zero_means_not_set"

DUE_KNOWN = "known"
DUE_MISSING = "missing"
DUE_CONFLICT = "conflict"

REASON_LABELS = {
    "unlinked_payments": "Some payments are not linked to this customer's invoices",
    "unlinked_returns": "Some returns are not linked to this customer's invoices",
    "other_ledger_entries": "Journal or payment vouchers reference this customer",
    "unconfirmed_payments": "A linked cheque or transfer has an unconfirmed status",
    "missing_due_date": "An unpaid invoice has no due date",
    "conflicting_due_date": "Invoice lines disagree on the due date",
    "missing_invoice_amount": "An invoice amount is missing",
}
CUSTOMER_REASONS = ("unlinked_payments", "unlinked_returns", "other_ledger_entries")

PAYMENT_STATUS_LABELS = {
    "paid": "Paid",
    "partially_paid": "Partially paid",
    "unpaid": "Unpaid",
    "overpaid": "Overpaid (credit)",
    "unknown": "Unknown",
}
DUE_STATUS_LABELS = {
    "settled": "Settled",
    "overdue": "Overdue",
    "due_today": "Due today",
    "not_yet_due": "Not yet due",
    "unknown": "Unknown",
}

SORT_KEYS = {
    "overdue_amount": "_overdue_sort",
    "days_overdue": "days_overdue",
    "unpaid_total": "unpaid_total",
    "invoiced_total": "invoiced_total",
    "customer_name": "_name_sort",
    "customer_id": "customer_id",
    "last_payment_date": "last_payment_date",
    "last_invoice_date": "last_invoice_date",
}
SORT_ORDERS = ("asc", "desc")
OVERDUE_FILTERS = ("all", STATUS_OVERDUE, STATUS_NOT_OVERDUE, STATUS_UNKNOWN, STATUS_NO_INVOICES)
ACTIVITY_FILTERS = ("with_invoices", "all")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_SEARCH_LENGTH = 100
MAX_CUSTOMER_TYPE_LENGTH = 30
CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,10}$")
MIN_PHONE_DIGITS = 3


class PaymentRiskInputError(ReportInputError):
    """Raised for invalid filter or identifier input (safe to show to users)."""


@dataclass(frozen=True)
class InvoiceFacts:
    sales_id: str
    sales_date: Optional[date]
    amount_cents: Optional[int]
    sales_type: Optional[str] = None
    line_count: int = 0
    dated_line_count: int = 0
    due_min: Optional[date] = None
    due_max: Optional[date] = None
    payment_cents: int = 0
    payment_rows: int = 0
    unconfirmed_payment_rows: int = 0
    return_cents: int = 0


@dataclass(frozen=True)
class CustomerLedger:
    customer_id: str
    name: Optional[str]
    phone: Optional[str]
    customer_type: Optional[str]
    balance_raw: Optional[float]
    credit_limit_raw: Optional[float]
    invoices: Tuple[InvoiceFacts, ...] = ()
    collected_cents: int = 0
    collection_rows: int = 0
    unlinked_payment_rows: int = 0
    unlinked_payment_cents: int = 0
    last_payment_date: Optional[date] = None
    returned_cents: int = 0
    unlinked_return_rows: int = 0
    other_ledger_rows: int = 0


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def interpret_balance(raw: Any, meaning: str) -> Optional[int]:
    """Return the outstanding balance in cents only when the field meaning is confirmed."""
    if raw is None or meaning != BALANCE_RECEIVABLE_POSITIVE:
        return None
    return to_cents(raw)


def interpret_credit_limit(raw: Any, meaning: str) -> Optional[int]:
    """Return the credit limit in cents only when the field meaning is confirmed."""
    if raw is None or meaning != CREDIT_LIMIT_AMOUNT:
        return None
    cents = to_cents(raw)
    return cents if cents is not None and cents > 0 else None


# ---------------------------------------------------------------------------
# Invoice rules
# ---------------------------------------------------------------------------

def resolve_due_date(invoice: InvoiceFacts) -> Tuple[Optional[date], str]:
    if invoice.line_count <= 0 or invoice.dated_line_count <= 0 or invoice.due_min is None:
        return None, DUE_MISSING
    if invoice.dated_line_count < invoice.line_count or invoice.due_min != invoice.due_max:
        return None, DUE_CONFLICT
    return invoice.due_min, DUE_KNOWN


def _unpaid_cents(invoice: InvoiceFacts, allocation_reliable: bool, reasons: List[str]) -> Tuple[Optional[int], int, str]:
    """Return (unpaid cents or None, credit cents, payment status)."""
    if invoice.amount_cents is None:
        reasons.append("missing_invoice_amount")
        return None, 0, "unknown"
    if not allocation_reliable:
        return None, 0, "unknown"
    if invoice.unconfirmed_payment_rows:
        reasons.append("unconfirmed_payments")
        return None, 0, "unknown"
    credits = invoice.payment_cents + invoice.return_cents
    remaining = invoice.amount_cents - credits
    if remaining > 0:
        return remaining, 0, "partially_paid" if credits > 0 else "unpaid"
    if remaining < 0:
        return 0, -remaining, "overpaid"
    return 0, 0, "paid"


def _due_position(unpaid: Optional[int], due: Optional[date], today: date) -> Tuple[Optional[bool], Optional[int], Optional[int], str]:
    """Return (is_overdue, overdue cents, days overdue, due status)."""
    if unpaid == 0:
        return False, 0, 0, "settled"
    if due is not None and today <= due:
        return False, 0, 0, "due_today" if today == due else "not_yet_due"
    if due is not None and unpaid is not None:
        return True, unpaid, (today - due).days, "overdue"
    return None, None, None, "unknown"


def _due_status_label(status: str, days_overdue: Optional[int], due: Optional[date], today: date) -> str:
    if status == "overdue" and days_overdue is not None:
        return f"Overdue {_plural(days_overdue, 'day')}"
    if status == "not_yet_due" and due is not None:
        return f"Due in {_plural((due - today).days, 'day')}"
    return DUE_STATUS_LABELS[status]


def evaluate_invoice(invoice: InvoiceFacts, allocation_reliable: bool, today: date) -> Dict[str, Any]:
    reasons: List[str] = []
    due, due_state = resolve_due_date(invoice)
    unpaid, credit, payment_status = _unpaid_cents(invoice, allocation_reliable, reasons)
    is_overdue, overdue, days, due_status = _due_position(unpaid, due, today)
    if is_overdue is None and due_state == DUE_MISSING:
        reasons.append("missing_due_date")
    elif is_overdue is None and due_state == DUE_CONFLICT:
        reasons.append("conflicting_due_date")
    return {
        "sales_id": invoice.sales_id,
        "sales_date": iso_date(invoice.sales_date),
        "sales_date_display": display_date(invoice.sales_date),
        "sales_type": invoice.sales_type,
        "line_count": invoice.line_count,
        "amount": cents_to_amount(invoice.amount_cents),
        "amount_display": format_money(invoice.amount_cents),
        "paid_amount": cents_to_amount(invoice.payment_cents),
        "paid_display": format_money(invoice.payment_cents),
        "returned_amount": cents_to_amount(invoice.return_cents),
        "returned_display": format_money(invoice.return_cents),
        "unpaid_amount": cents_to_amount(unpaid),
        "unpaid_display": format_money(unpaid),
        "credit_amount": cents_to_amount(credit) if credit else None,
        "credit_display": format_money(credit) if credit else None,
        "payment_status": payment_status,
        "payment_status_label": PAYMENT_STATUS_LABELS[payment_status],
        "due_date": iso_date(due),
        "due_date_display": display_date(due),
        "due_date_status": due_state,
        "is_overdue": is_overdue,
        "overdue_amount": cents_to_amount(overdue),
        "days_overdue": days,
        "due_status": due_status,
        "due_status_label": _due_status_label(due_status, days, due, today),
        "reason_codes": reasons,
        "_unpaid_cents": unpaid,
        "_overdue_cents": overdue,
        "_amount_cents": invoice.amount_cents,
    }


# ---------------------------------------------------------------------------
# Customer rules
# ---------------------------------------------------------------------------

def _customer_reasons(ledger: CustomerLedger) -> List[str]:
    reasons = []
    if ledger.unlinked_payment_rows:
        reasons.append("unlinked_payments")
    if ledger.unlinked_return_rows:
        reasons.append("unlinked_returns")
    if ledger.other_ledger_rows:
        reasons.append("other_ledger_entries")
    return reasons


def _sum_known(values: Iterable[Optional[int]]) -> Optional[int]:
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _overdue_rollup(invoices: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    confirmed = [inv for inv in invoices if inv["is_overdue"] is True]
    all_known = all(inv["is_overdue"] is not None for inv in invoices)
    confirmed_cents = sum(inv["_overdue_cents"] for inv in confirmed)
    if not invoices:
        status, exact, days = STATUS_NO_INVOICES, 0, 0
    elif confirmed:
        status = STATUS_OVERDUE
        exact = confirmed_cents if all_known else None
        days = max(inv["days_overdue"] for inv in confirmed)
    elif all_known:
        status, exact, days = STATUS_NOT_OVERDUE, 0, 0
    else:
        status, exact, days = STATUS_UNKNOWN, None, None
    return {
        "status": status,
        "exact_cents": exact,
        "confirmed_cents": confirmed_cents,
        "confirmed_count": len(confirmed),
        "days": days,
        "days_is_minimum": status == STATUS_OVERDUE and not all_known,
    }


def _overdue_display(rollup: Mapping[str, Any]) -> str:
    if rollup["exact_cents"] is not None:
        return format_money(rollup["exact_cents"])
    if rollup["confirmed_cents"] > 0:
        return f"At least {format_money(rollup['confirmed_cents'])}"
    return UNKNOWN


def _days_display(rollup: Mapping[str, Any]) -> str:
    status, days = rollup["status"], rollup["days"]
    if status == STATUS_OVERDUE:
        text = _plural(days, "day")
        return f"At least {text}" if rollup["days_is_minimum"] else text
    if status == STATUS_NOT_OVERDUE:
        return "Not overdue"
    if status == STATUS_NO_INVOICES:
        return "No invoices"
    return UNKNOWN


def _collect_reasons(ledger: CustomerLedger, invoices: Sequence[Mapping[str, Any]]) -> List[str]:
    affected = [inv for inv in invoices if inv["_unpaid_cents"] is None or inv["is_overdue"] is None]
    if not affected:
        return []
    codes = list(_customer_reasons(ledger))
    for inv in affected:
        codes.extend(inv["reason_codes"])
    return [code for code in REASON_LABELS if code in set(codes)]


def evaluate_customer(
    ledger: CustomerLedger,
    today: date,
    balance_meaning: str = BALANCE_UNVERIFIED,
    credit_limit_meaning: str = CREDIT_LIMIT_UNVERIFIED,
) -> Dict[str, Any]:
    """Roll invoice facts up into one customer row (internal keys start with '_')."""
    reliable = not _customer_reasons(ledger)
    ordered = sorted(ledger.invoices, key=lambda inv: (inv.sales_date or date.min, inv.sales_id), reverse=True)
    invoices = [evaluate_invoice(inv, reliable, today) for inv in ordered]
    rollup = _overdue_rollup(invoices)
    invoiced = _sum_known(inv["_amount_cents"] for inv in invoices)
    unpaid = _sum_known(inv["_unpaid_cents"] for inv in invoices)
    balance = interpret_balance(ledger.balance_raw, balance_meaning)
    credit_limit = interpret_credit_limit(ledger.credit_limit_raw, credit_limit_meaning)
    last_invoice = max((inv.sales_date for inv in ordered if inv.sales_date), default=None)
    reasons = _collect_reasons(ledger, invoices)
    name = (ledger.name or "").strip() or None
    overdue_sort = rollup["exact_cents"] if rollup["exact_cents"] is not None else (
        rollup["confirmed_cents"] if rollup["confirmed_cents"] > 0 else None)
    return {
        "customer_id": ledger.customer_id,
        "customer_name": name or UNKNOWN,
        "phone": ledger.phone,
        "phone_display": ledger.phone or UNKNOWN,
        "customer_type": ledger.customer_type,
        "invoice_count": len(invoices),
        "invoiced_total": cents_to_amount(invoiced),
        "invoiced_display": format_money(invoiced),
        "last_invoice_date": iso_date(last_invoice),
        "last_invoice_date_display": display_date(last_invoice) if invoices else "No invoices",
        "collected_total": cents_to_amount(ledger.collected_cents),
        "collected_display": format_money(ledger.collected_cents),
        "last_payment_date": iso_date(ledger.last_payment_date),
        "last_payment_date_display": display_date(ledger.last_payment_date) if ledger.collection_rows else "No payments",
        "returned_total": cents_to_amount(ledger.returned_cents),
        "returned_display": format_money(ledger.returned_cents),
        "unpaid_total": cents_to_amount(unpaid),
        "unpaid_display": format_money(unpaid),
        "overdue_status": rollup["status"],
        "overdue_status_label": STATUS_LABELS[rollup["status"]],
        "overdue_amount": cents_to_amount(rollup["exact_cents"]),
        "overdue_amount_confirmed": cents_to_amount(rollup["confirmed_cents"]),
        "overdue_display": _overdue_display(rollup),
        "overdue_invoice_count": rollup["confirmed_count"],
        "days_overdue": rollup["days"],
        "days_overdue_is_minimum": rollup["days_is_minimum"],
        "days_overdue_display": _days_display(rollup),
        "outstanding_balance": cents_to_amount(balance),
        "outstanding_balance_display": format_money(balance),
        "credit_limit": cents_to_amount(credit_limit),
        "credit_limit_display": format_money(credit_limit),
        "allocation_reliable": reliable,
        "reason_codes": reasons,
        "data_notes": [REASON_LABELS[code] for code in reasons],
        "_overdue_sort": overdue_sort,
        "_name_sort": name.casefold() if name else None,
        "_unpaid_cents": unpaid,
        "_confirmed_overdue_cents": rollup["confirmed_cents"],
        "_invoices": invoices,
    }


# ---------------------------------------------------------------------------
# Filters, sorting, pagination and summary
# ---------------------------------------------------------------------------

def _choice(raw: Mapping[str, Any], key: str, default: str, allowed: Sequence[str]) -> str:
    value = str(raw.get(key) or default).strip().lower()
    if value not in allowed:
        raise PaymentRiskInputError(f"Invalid {key}. Use one of: {', '.join(allowed)}.")
    return value


def _bounded_int(raw: Mapping[str, Any], key: str, default: int, maximum: Optional[int]) -> int:
    value = raw.get(key, default)
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise PaymentRiskInputError(f"Invalid {key}. Use a whole number.") from None
    if number < 1 or (maximum is not None and number > maximum):
        limit = f" between 1 and {maximum}" if maximum else " of 1 or more"
        raise PaymentRiskInputError(f"Invalid {key}. Use a number{limit}.")
    return number


def normalize_report_filters(raw: Mapping[str, Any]) -> Dict[str, Any]:
    q = str(raw.get("q") or "").strip()
    if len(q) > MAX_SEARCH_LENGTH:
        raise PaymentRiskInputError(f"Search text must be {MAX_SEARCH_LENGTH} characters or fewer.")
    customer_type = str(raw.get("customer_type") or "").strip()
    if len(customer_type) > MAX_CUSTOMER_TYPE_LENGTH:
        raise PaymentRiskInputError("Invalid customer_type.")
    return {
        "q": q,
        "overdue_status": _choice(raw, "overdue_status", "all", OVERDUE_FILTERS),
        "customer_type": customer_type,
        "activity": _choice(raw, "activity", "with_invoices", ACTIVITY_FILTERS),
        "sort_by": _choice(raw, "sort_by", "overdue_amount", tuple(SORT_KEYS)),
        "sort_order": _choice(raw, "sort_order", "desc", SORT_ORDERS),
        "page": _bounded_int(raw, "page", 1, None),
        "page_size": _bounded_int(raw, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
    }


def parse_page(value: Any, key: str = "page") -> int:
    """Validate a 1-based page number."""
    return _bounded_int({key: value}, key, 1, None)


def validate_customer_id(value: Any) -> str:
    text = str(value or "").strip()
    if not CUSTOMER_ID_PATTERN.fullmatch(text):
        raise PaymentRiskInputError("Invalid customer_id.")
    return text


def _matches_search(row: Mapping[str, Any], query: str) -> bool:
    needle = query.casefold()
    if needle in (row.get("_name_sort") or "") or needle in str(row.get("customer_id") or "").casefold():
        return True
    digits = re.sub(r"\D", "", query)
    phone_digits = re.sub(r"\D", "", str(row.get("phone") or ""))
    return len(digits) >= MIN_PHONE_DIGITS and digits in phone_digits


def filter_customers(rows: Iterable[Mapping[str, Any]], filters: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    status = filters.get("overdue_status", "all")
    customer_type = str(filters.get("customer_type") or "").casefold()
    query = filters.get("q") or ""
    selected = []
    for row in rows:
        if filters.get("activity", "with_invoices") == "with_invoices" and row["invoice_count"] == 0:
            continue
        if status != "all" and row["overdue_status"] != status:
            continue
        if customer_type and str(row.get("customer_type") or "").casefold() != customer_type:
            continue
        if query and not _matches_search(row, query):
            continue
        selected.append(row)
    return selected


def sort_customers(rows: Iterable[Mapping[str, Any]], sort_by: str, sort_order: str) -> List[Mapping[str, Any]]:
    """Sort known values in the requested direction; unknown values always go last."""
    field = SORT_KEYS.get(sort_by, "_overdue_sort")
    return sort_known_first(
        rows, field, sort_order == "desc",
        tie_key=lambda r: (r.get("_name_sort") or str(r.get("customer_name") or "").casefold(),
                           str(r.get("customer_id") or "")),
    )


def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    def count(status: str) -> int:
        return sum(1 for row in rows if row["overdue_status"] == status)

    overdue_rows = [row for row in rows if row["overdue_status"] == STATUS_OVERDUE]
    confirmed_overdue = sum(row["_confirmed_overdue_cents"] for row in overdue_rows)
    known_unpaid = [row["_unpaid_cents"] for row in rows if row["_unpaid_cents"] is not None]
    reason_counts: Dict[str, int] = {}
    for row in rows:
        for code in row["reason_codes"]:
            reason_counts[code] = reason_counts.get(code, 0) + 1
    unknown = count(STATUS_UNKNOWN)
    return {
        "customer_count": len(rows),
        "overdue_customers": len(overdue_rows),
        "not_overdue_customers": count(STATUS_NOT_OVERDUE),
        "unknown_customers": unknown,
        "no_invoice_customers": count(STATUS_NO_INVOICES),
        "confirmed_overdue_amount": cents_to_amount(confirmed_overdue),
        "confirmed_overdue_display": format_money(confirmed_overdue),
        "overdue_amount_complete": unknown == 0 and all(row["overdue_amount"] is not None for row in overdue_rows),
        "confirmed_unpaid_amount": cents_to_amount(sum(known_unpaid)),
        "confirmed_unpaid_display": format_money(sum(known_unpaid)),
        "unpaid_unknown_customers": len(rows) - len(known_unpaid),
        "unknown_reasons": [
            {"code": code, "label": REASON_LABELS[code], "count": reason_counts[code]}
            for code in sorted(reason_counts, key=lambda c: (-reason_counts[c], c))
        ],
    }
