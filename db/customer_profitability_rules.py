"""Calculation rules for Report 14: Customer Profitability (pure, no database access).

Rules
-----
- Sales come from invoice lines (QTY x UNIT_PRICE). Every recorded line is a
  real unit: invoice headers equal the sum of their lines, including repeated
  lines, so lines are summed once and never joined against other line tables.
- The invoice header discount is split across the invoice's lines in
  proportion to line amounts (exact to the cent). Service and other charges
  on the header are not product sales and are left out.
- Cost of a sale = QTY x ORG_COST recorded on the sale line at the time of the
  sale. A line without a positive ORG_COST has an Unknown cost.
- Returns are counted on their return date at the amount and ORG_COST recorded
  on the return line (the system copies the original sale line's ORG_COST).
  A return without a cost makes the cost of sales Unknown.
- Gross profit = net sales (sales - returns) - cost of sales (sale cost -
  returned cost). It is Unknown whenever any part is Unknown, never zero.
  This is gross profit, not net profit: operating expenses, commissions and
  overheads are not deducted.
- Gross margin = gross profit / net sales, only when net sales are positive.
- "Gross profit on costed lines" adds up only lines whose sale amount and cost
  are both known, and is always labelled as partial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence

from .report_support import (
    UNKNOWN,
    ReportInputError,
    cents_to_amount,
    display_date,
    format_money,
    iso_date,
    sort_known_first,
)

INVOICE_TOTAL_TOLERANCE_CENTS = 50
DEFAULT_PERIOD_DAYS = 90
MAX_PERIOD_DAYS = 5 * 366
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_SEARCH_LENGTH = 100
CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,10}$")

REASON_LABELS = {
    "missing_cost": "Some sale lines have no recorded cost",
    "return_missing_cost": "Some returns have no recorded cost",
    "return_missing_amount": "Some returns have no recorded amount",
    "invoice_total_mismatch": "Invoice lines do not add up to the invoice total",
    "missing_invoice": "Some sale lines have no invoice header",
    "invalid_price": "Some sale lines have an invalid price or quantity",
    "discount_unallocatable": "An invoice discount is negative, larger than the invoice, or cannot be split",
}
SORT_KEYS = {
    "gross_profit": "_profit_sort",
    "gross_margin": "gross_margin_pct",
    "net_sales": "net_sales",
    "sales": "sales",
    "returns": "returns",
    "cost_coverage": "cost_coverage_pct",
    "invoice_count": "invoice_count",
    "name": "_name_sort",
    "id": "_id_sort",
}
SORT_ORDERS = ("asc", "desc")
PROFIT_STATUSES = ("all", "known", "unknown")


@dataclass(frozen=True)
class SaleFact:
    """Sale lines of one invoice and product that share the same cost / price validity."""
    sales_id: str
    customer_id: str
    product_id: str
    sale_date: Optional[date]
    quantity: float
    line_count: int
    gross_cents: Optional[int]          # None when a price or quantity is invalid
    cost_cents: Optional[int]           # None when the lines have no positive ORG_COST
    zero_price_lines: int = 0


@dataclass(frozen=True)
class InvoiceHeader:
    sales_id: str
    total_cents: Optional[int]
    discount_cents: int


@dataclass(frozen=True)
class NetSale:
    sales_id: str
    customer_id: str
    product_id: str
    sale_date: Optional[date]
    quantity: float
    net_cents: Optional[int]
    cost_cents: Optional[int]
    zero_price_lines: int
    revenue_issue: Optional[str]


@dataclass(frozen=True)
class ReturnFact:
    customer_id: str
    product_id: str
    return_date: Optional[date]
    quantity: float
    amount_cents: Optional[int]
    cost_cents: Optional[int]


@dataclass
class GroupTotals:
    """Accumulator for one customer, product or summary group (internal only)."""
    sales_cents: int = 0
    sales_unknown: int = 0
    cost_cents: int = 0
    cost_unknown: int = 0
    costed_sales_cents: int = 0
    costed_cost_cents: int = 0
    uncosted_sales_cents: int = 0
    return_cents: int = 0
    return_amount_unknown: int = 0
    return_cost_cents: int = 0
    return_cost_unknown: int = 0
    costed_return_cents: int = 0
    costed_return_cost_cents: int = 0
    costed_lines: int = 0
    quantity_sold: float = 0
    quantity_returned: float = 0
    zero_price_lines: int = 0
    invoices: set = field(default_factory=set)
    reasons: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Invoice discounts
# ---------------------------------------------------------------------------

def allocate_discount(amounts: Sequence[int], discount: int) -> Optional[List[int]]:
    """Split a discount across amounts in proportion, exact to the cent (integer largest remainder).

    Returns None when the discount cannot be split reliably: no amount to split it over, a
    negative discount, or a discount larger than the amounts it applies to.
    """
    if discount == 0:
        return [0] * len(amounts)
    total = sum(amounts)
    if total <= 0 or discount < 0 or discount > total or any(amount < 0 for amount in amounts):
        return None
    parts = [divmod(discount * amount, total) for amount in amounts]
    shares = [quotient for quotient, _ in parts]
    order = sorted(range(len(amounts)), key=lambda i: (-parts[i][1], i))
    for index in order[:discount - sum(shares)]:
        shares[index] += 1
    return shares


def _invoice_issue(facts: Sequence[SaleFact], header: Optional[InvoiceHeader]) -> Optional[str]:
    if header is None:
        return "missing_invoice"
    if any(fact.gross_cents is None for fact in facts):
        return "invalid_price"
    line_total = sum(fact.gross_cents for fact in facts)
    if header.total_cents is None or abs(line_total - header.total_cents) > INVOICE_TOTAL_TOLERANCE_CENTS:
        return "invoice_total_mismatch"
    return None


def apply_invoice_discounts(facts: Iterable[SaleFact], headers: Mapping[str, InvoiceHeader]) -> List[NetSale]:
    """Turn sale facts into net sales after the header discount, flagging unreliable invoices."""
    by_invoice: Dict[str, List[SaleFact]] = {}
    for fact in facts:
        by_invoice.setdefault(fact.sales_id, []).append(fact)
    result: List[NetSale] = []
    for sales_id, invoice_facts in by_invoice.items():
        header = headers.get(sales_id)
        issue = _invoice_issue(invoice_facts, header)
        shares = None
        if issue is None:
            shares = allocate_discount([fact.gross_cents for fact in invoice_facts], header.discount_cents)
            issue = None if shares is not None else "discount_unallocatable"
        for index, fact in enumerate(invoice_facts):
            net = fact.gross_cents - shares[index] if issue is None else None
            result.append(NetSale(fact.sales_id, fact.customer_id, fact.product_id, fact.sale_date, fact.quantity,
                                  net, fact.cost_cents, fact.zero_price_lines, issue))
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _add_sale(totals: GroupTotals, sale: NetSale) -> None:
    totals.invoices.add(sale.sales_id)
    totals.quantity_sold += sale.quantity
    totals.zero_price_lines += sale.zero_price_lines
    if sale.net_cents is None:
        totals.sales_unknown += 1
        totals.reasons.add(sale.revenue_issue or "invalid_price")
    else:
        totals.sales_cents += sale.net_cents
    if sale.cost_cents is None:
        totals.cost_unknown += 1
        totals.reasons.add("missing_cost")
        if sale.net_cents is not None:
            totals.uncosted_sales_cents += sale.net_cents
    else:
        totals.cost_cents += sale.cost_cents
        if sale.net_cents is not None:
            totals.costed_sales_cents += sale.net_cents
            totals.costed_cost_cents += sale.cost_cents
            totals.costed_lines += 1


def _add_return(totals: GroupTotals, ret: ReturnFact) -> None:
    totals.quantity_returned += ret.quantity
    if ret.amount_cents is None:
        totals.return_amount_unknown += 1
        totals.reasons.add("return_missing_amount")
    else:
        totals.return_cents += ret.amount_cents
    if ret.cost_cents is None:
        totals.return_cost_unknown += 1
        totals.reasons.add("return_missing_cost")
    else:
        totals.return_cost_cents += ret.cost_cents
        if ret.amount_cents is not None:
            totals.costed_return_cents += ret.amount_cents
            totals.costed_return_cost_cents += ret.cost_cents
            totals.costed_lines += 1


def aggregate(sales: Iterable[NetSale], returns: Iterable[ReturnFact],
              key: Callable[[Any], Hashable]) -> Dict[Hashable, GroupTotals]:
    groups: Dict[Hashable, GroupTotals] = {}
    for sale in sales:
        _add_sale(groups.setdefault(key(sale), GroupTotals()), sale)
    for ret in returns:
        _add_return(groups.setdefault(key(ret), GroupTotals()), ret)
    return groups


def combine(groups: Iterable[GroupTotals]) -> GroupTotals:
    combined = GroupTotals()
    for group in groups:
        for name in ("sales_cents", "sales_unknown", "cost_cents", "cost_unknown", "costed_sales_cents",
                     "costed_cost_cents", "uncosted_sales_cents", "return_cents", "return_amount_unknown",
                     "return_cost_cents", "return_cost_unknown", "costed_return_cents",
                     "costed_return_cost_cents", "costed_lines", "quantity_sold", "quantity_returned",
                     "zero_price_lines"):
            setattr(combined, name, getattr(combined, name) + getattr(group, name))
        combined.invoices |= group.invoices
        combined.reasons |= group.reasons
    return combined


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _percent(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if part is None or whole is None or whole <= 0:
        return None
    return round(part * 100 / whole, 1)


def _format_percent(value: Optional[float]) -> str:
    return UNKNOWN if value is None else f"{value:.1f}%"


def _margin_display(margin: Optional[float], profit: Optional[int], net_sales: Optional[int]) -> str:
    if margin is not None:
        return _format_percent(margin)
    if profit is not None and net_sales is not None and net_sales <= 0:
        return "Not applicable"  # no positive net sales to divide by
    return UNKNOWN


def _quantity(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 2)


def evaluate_group(totals: GroupTotals) -> Dict[str, Any]:
    sales = None if totals.sales_unknown else totals.sales_cents
    returns = None if totals.return_amount_unknown else totals.return_cents
    net_sales = None if sales is None or returns is None else sales - returns
    cost = None if totals.cost_unknown or totals.return_cost_unknown else totals.cost_cents - totals.return_cost_cents
    profit = None if net_sales is None or cost is None else net_sales - cost
    costed_profit = None if totals.costed_lines == 0 else (
        (totals.costed_sales_cents - totals.costed_return_cents)
        - (totals.costed_cost_cents - totals.costed_return_cost_cents))
    known_sales = totals.costed_sales_cents + totals.uncosted_sales_cents
    margin = _percent(profit, net_sales)
    coverage = _percent(totals.costed_sales_cents, known_sales)
    reasons = [code for code in REASON_LABELS if code in totals.reasons]
    return {
        "invoice_count": len(totals.invoices),
        "quantity_sold": _quantity(totals.quantity_sold),
        "quantity_returned": _quantity(totals.quantity_returned),
        "sales": cents_to_amount(sales),
        "sales_display": format_money(sales),
        "returns": cents_to_amount(returns),
        "returns_display": format_money(returns),
        "net_sales": cents_to_amount(net_sales),
        "net_sales_display": format_money(net_sales),
        "cost_of_sales": cents_to_amount(cost),
        "cost_of_sales_display": format_money(cost),
        "gross_profit": cents_to_amount(profit),
        "gross_profit_display": format_money(profit),
        "gross_margin_pct": margin,
        "gross_margin_display": _margin_display(margin, profit, net_sales),
        "profit_status": "known" if profit is not None else "unknown",
        "profit_status_label": "Known" if profit is not None else UNKNOWN,
        "costed_gross_profit": cents_to_amount(costed_profit),
        "costed_gross_profit_display": format_money(costed_profit),
        "cost_coverage_pct": coverage,
        "cost_coverage_display": f"{_format_percent(coverage)} of sales" if coverage is not None else UNKNOWN,
        "uncosted_sales": cents_to_amount(totals.uncosted_sales_cents),
        "uncosted_sales_display": format_money(totals.uncosted_sales_cents),
        "zero_price_lines": totals.zero_price_lines,
        "reason_codes": reasons,
        "data_notes": [REASON_LABELS[code] for code in reasons],
        "_profit_sort": profit,
    }


def summarize(groups: Sequence[GroupTotals], rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    summary = {key: value for key, value in evaluate_group(combine(groups)).items() if not key.startswith("_")}
    summary.update({
        "row_count": len(rows),
        "known_count": sum(1 for row in rows if row["profit_status"] == "known"),
        "unknown_count": sum(1 for row in rows if row["profit_status"] == "unknown"),
    })
    return summary


# ---------------------------------------------------------------------------
# Filters, sorting
# ---------------------------------------------------------------------------

def _parse_date(raw: Mapping[str, Any], key: str, default: date) -> date:
    value = str(raw.get(key) or "").strip()
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReportInputError(f"Invalid {key}. Use YYYY-MM-DD.") from None


def _choice(raw: Mapping[str, Any], key: str, default: str, allowed: Sequence[str]) -> str:
    value = str(raw.get(key) or default).strip().lower()
    if value not in allowed:
        raise ReportInputError(f"Invalid {key}. Use one of: {', '.join(allowed)}.")
    return value


def _positive_int(raw: Mapping[str, Any], key: str, default: int, maximum: Optional[int] = None) -> int:
    try:
        number = int(raw.get(key) if raw.get(key) is not None else default)
    except (TypeError, ValueError):
        raise ReportInputError(f"Invalid {key}. Use a whole number.") from None
    if number < 1 or (maximum is not None and number > maximum):
        raise ReportInputError(f"Invalid {key}. Use a number from 1{f' to {maximum}' if maximum else ''}.")
    return number


def normalize_filters(raw: Mapping[str, Any], today: date) -> Dict[str, Any]:
    end = _parse_date(raw, "end_date", today)
    start = _parse_date(raw, "start_date", end - timedelta(days=DEFAULT_PERIOD_DAYS - 1))
    if start > end:
        raise ReportInputError("Invalid start_date. It must be on or before end_date.")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ReportInputError("Invalid date range. Choose a period of 5 years or less.")
    q = str(raw.get("q") or "").strip()
    if len(q) > MAX_SEARCH_LENGTH:
        raise ReportInputError(f"Search text must be {MAX_SEARCH_LENGTH} characters or fewer.")
    customer_id = str(raw.get("customer_id") or "").strip()
    if customer_id and not CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise ReportInputError("Invalid customer_id.")
    return {
        "start_date": start,
        "end_date": end,
        "q": q,
        "customer_id": customer_id or None,
        "profit_status": _choice(raw, "profit_status", "all", PROFIT_STATUSES),
        "sort_by": _choice(raw, "sort_by", "gross_profit", tuple(SORT_KEYS)),
        "sort_order": _choice(raw, "sort_order", "desc", SORT_ORDERS),
        "page": _positive_int(raw, "page", 1),
        "page_size": _positive_int(raw, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
    }


def public_filters(filters: Mapping[str, Any]) -> Dict[str, Any]:
    return {**filters, "start_date": iso_date(filters["start_date"]), "end_date": iso_date(filters["end_date"])}


def period(filters: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "start_date": iso_date(filters["start_date"]),
        "end_date": iso_date(filters["end_date"]),
        "display": f"{display_date(filters['start_date'])} – {display_date(filters['end_date'])}",
    }


def matches_search(row: Mapping[str, Any], query: str) -> bool:
    needle = query.casefold()
    haystack = " ".join(str(row.get(key) or "") for key in ("_name_sort", "_id_sort", "phone", "category_name"))
    return needle in haystack.casefold()


def filter_rows(rows: Iterable[Mapping[str, Any]], filters: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    status = filters["profit_status"]
    return [row for row in rows
            if (status == "all" or row["profit_status"] == status)
            and (not filters["q"] or matches_search(row, filters["q"]))]


def sort_rows(rows: Iterable[Mapping[str, Any]], sort_by: str, sort_order: str) -> List[Mapping[str, Any]]:
    return sort_known_first(rows, SORT_KEYS[sort_by], sort_order == "desc",
                            tie_key=lambda r: (r.get("_name_sort") or "￿", r.get("_id_sort") or ""))
