"""Evidence-backed Bangladesh printer/ink demand research for Report 10.

Design rules enforced here:

- Only an Admin-triggered run calls Tavily. A report render never performs a
  search.
- A model is called ``New Release`` only when the source supplies a release
  date plus release wording. Otherwise it is ``First Observed`` with the date
  this application first stored it.
- ``Bestseller`` / ``Popular`` require an explicit demand phrase in the source
  and store the exact source URL/quote. A listing alone is ``Demand Unknown``.
- Prices are stored in the source currency. BDT is never invented or converted.
- Local comparison reads the complete ``product`` catalog, including products
  with no recent sales. Similar names are at most a ``Possible Match``.
- Provider failures, quota exhaustion, timeouts and interrupted runs are
  persisted as failed/partial/interrupted, never as completed.
- Tavily credits are capped at 50 per run and 900 per calendar month.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import load_dotenv
from db import market_research as repo

load_dotenv()

logger = logging.getLogger("market_research_service")

PROVIDER_NAME = "tavily"
PROVIDER_ENDPOINT = "https://api.tavily.com/search"
MAX_CREDITS_PER_RESEARCH = 50
MAX_CREDITS_PER_MONTH = 900
CREDIT_COST_PER_SEARCH = 1
DEFAULT_SEARCH_TIMEOUT = 15
MAX_RESULTS_PER_QUERY = 5
STALE_RUN_MINUTES = 30

LOCAL_MATCH_LABELS = {
    "already_in_catalog": "Already in Catalog",
    "possible_match": "Possible Match",
    "not_found": "Not Found",
    "comparison_unavailable": "Comparison Unavailable",
}
DEMAND_LABELS = {
    "bestseller": "Bestseller",
    "popular": "Popular",
    "demand_unknown": "Demand Unknown",
}
RELEASE_LABELS = {
    "new_release": "New Release",
    "first_observed": "First Observed",
}

# Selected Bangladesh retailers, distributors and manufacturer storefronts.
# Queries only contain public product vocabulary and a site: filter.
BANGLADESH_MARKET_SOURCES: Sequence[Mapping[str, str]] = (
    {
        "name": "Star Tech",
        "type": "retailer",
        "base_url": "https://www.startech.com.bd",
        "search_domain": "startech.com.bd",
        "url_contains": "startech.com.bd",
    },
    {
        "name": "Ryans Computers",
        "type": "retailer",
        "base_url": "https://www.ryans.com",
        "search_domain": "ryans.com",
        "url_contains": "ryans.com",
    },
    {
        "name": "Techland BD",
        "type": "retailer",
        "base_url": "https://www.techlandbd.com",
        "search_domain": "techlandbd.com",
        "url_contains": "techlandbd.com",
    },
    {
        "name": "Gadget & Gear",
        "type": "retailer",
        "base_url": "https://www.gadgetandgear.com",
        "search_domain": "gadgetandgear.com",
        "url_contains": "gadgetandgear.com",
    },
    {
        "name": "Epson Bangladesh",
        "type": "manufacturer",
        "base_url": "https://www.epson.com.bd",
        "search_domain": "epson.com.bd",
        "url_contains": "epson.com.bd",
    },
    {
        "name": "HP Bangladesh",
        "type": "manufacturer",
        "base_url": "https://www.hp.com/bd-en/",
        "search_domain": "hp.com/bd-en",
        "url_contains": "hp.com/bd-",
    },
    {
        "name": "Canon Bangladesh",
        "type": "manufacturer",
        "base_url": "https://www.canon.com.bd",
        "search_domain": "canon.com.bd",
        "url_contains": "canon.com.bd",
    },
    {
        "name": "J.A.N. Associates (Canon distributor)",
        "type": "distributor",
        "base_url": "https://jan-bd.com",
        "search_domain": "jan-bd.com",
        "url_contains": "jan-bd.com",
    },
)

_COMMON_BRANDS = (
    "HP",
    "Epson",
    "Canon",
    "Brother",
    "Pantum",
    "Xerox",
    "Samsung",
    "Ricoh",
    "Kyocera",
    "Lexmark",
    "Dell",
    "Toshiba",
    "Sharp",
    "Konica Minolta",
    "Fujifilm",
    "Walton",
    "Riso",
    "Sindoh",
    "Deli",
    "Xiaomi",
)

_BRAND_ALIASES = {
    "HEWLETT PACKARD": "HP",
    "HEWLETT-PACKARD": "HP",
    "EPSON BANGLADESH": "EPSON",
    "CANON BANGLADESH": "CANON",
}

_GENERIC_BRAND_NAMES = {
    "CHINA",
    "DIGITAL HEAT PRESS",
    "DONGLE",
    "DTF",
    "ECO SOLVENT",
    "EURO",
    "HD",
    "HOT MELT",
    "HOT MELT (CHINA)",
    "HOT MELT (LOCAL)",
    "HTV",
    "INK",
    "NON BRAND",
    "NONE",
    "ORIGINAL",
    "OTHERS",
    "SMART",
}

_SERIES_WORDS = (
    "ECOTANK",
    "MEGATANK",
    "SMART TANK",
    "PIXMA",
    "MAXIFY",
    "IMAGE CLASS",
    "IMAGECLASS",
    "IMAGEPROGRAF",
    "LASERJET",
    "DESKJET",
    "OFFICEJET",
    "NEVERSTOP",
    "I-SENSYS",
    "SENSYS",
    "DCP",
    "HL",
    "MFC",
    "TN",
    "CF",
    "CE",
    "CB",
    "DR",
    "PG",
    "CL",
    "GI",
)

_EXCLUDED_MODEL_TOKENS = {
    "CAT",
    "SKU",
    "ITEM",
    "CODE",
    "MODEL",
    "BDT",
    "USD",
    "EUR",
    "GBP",
    "USB",
    "LAN",
    "WIFI",
    "DPI",
    "PPM",
    "IPM",
    "ISO",
    "A4",
    "A3",
    "A5",
    "PAGE",
    "PAGES",
    "MONTH",
    "YEAR",
    "WARRANTY",
}

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_MODEL_RE = re.compile(r"\b[A-Z]{1,6}[- ]?\d{2,5}[A-Z0-9]{0,5}\b")
_NUMERIC_MODEL_RE = re.compile(r"\b\d{3,4}\b")
_INK_CONTEXT_RE = re.compile(
    r"(INK|TONER|BOTTLE|CARTRIDGE|REFILL|RIBBON)", re.IGNORECASE
)

_DEMAND_PATTERNS = (
    (
        "bestseller",
        re.compile(
            r"\b(?:best[\s-]?seller|best[\s-]?selling|number one seller|no\.?\s*1 seller)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "popular",
        re.compile(
            r"\b(?:most popular|popular|top[\s-]?selling|trending|customer favorite)\b",
            re.IGNORECASE,
        ),
    ),
)

_RELEASE_RE = re.compile(
    r"\b(?:newly\s+launched|launched|release(?:d)?|release\s+date|"
    r"introduc(?:ed|ing)|announc(?:ed|ing)|debut(?:ed)?)\b",
    re.IGNORECASE,
)

_DATE_TEXT_RE = re.compile(
    r"(?:(?:release(?:d)?(?:\s+date)?|launched|announced|introduced|debut(?:ed)?)"
    r"[^.\n]{0,60}?)(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|"
    r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE,
)

_AVAILABILITY_PATTERNS = (
    ("Out of Stock", re.compile(r"\b(out\s+of\s+stock|sold\s+out|not\s+available|unavailable)\b", re.IGNORECASE)),
    ("Pre-order", re.compile(r"\b(pre[\s-]?order|upcoming)\b", re.IGNORECASE)),
    ("In Stock", re.compile(r"\b(in\s+stock|available\s+now|ready\s+to\s+ship)\b", re.IGNORECASE)),
    ("Available", re.compile(r"\b(available|buy\s+now|order\s+now)\b", re.IGNORECASE)),
)

_CURRENCY_PATTERNS = (
    (
        "BDT",
        (
            re.compile(r"(?:BDT|Tk\.?|Taka|৳)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE),
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:BDT|Tk\.?|Taka|৳)", re.IGNORECASE),
        ),
    ),
    (
        "USD",
        (
            re.compile(r"(?:USD|US\$|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE),
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*USD\b", re.IGNORECASE),
        ),
    ),
    (
        "EUR",
        (
            re.compile(r"(?:EUR|€)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE),
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*EUR\b", re.IGNORECASE),
        ),
    ),
    (
        "GBP",
        (
            re.compile(r"(?:GBP|£)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE),
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*GBP\b", re.IGNORECASE),
        ),
    ),
)

_COLOR_VARIANTS = ("Black", "Cyan", "Magenta", "Yellow", "Tri-Color", "Color")
_PACK_VARIANTS = ("Twin Pack", "Combo Pack", "High Yield", "Standard Yield", "XL")


class CatalogUnavailableError(RuntimeError):
    """Raised when the local product catalog cannot be read."""


class DuplicateResearchJobError(RuntimeError):
    """Raised when a pending or running research job already exists."""

    def __init__(self, job_id: str):
        super().__init__("A market research job is already running.")
        self.job_id = job_id


_START_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    return os.getenv("TAVILY_API_KEY", "").strip()


def provider_configured() -> bool:
    return bool(get_api_key())


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _iso_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text[:32], fmt).date()
        except ValueError:
            continue
    return None


def _sentence_with(text: str, pattern: re.Pattern[str]) -> Optional[str]:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if pattern.search(sentence):
            return " ".join(sentence.split())[:500]
    match = pattern.search(text or "")
    if match:
        start = max(0, match.start() - 90)
        end = min(len(text), match.end() + 140)
        return " ".join(text[start:end].split())[:500]
    return None


def _clean_source_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title or "")).strip()[:600]


def _compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9 ]+", " ", (value or "").upper()).strip()


# ---------------------------------------------------------------------------
# Tavily client
# ---------------------------------------------------------------------------

def classify_provider_error(result: Mapping[str, Any]) -> str:
    """Map a failed provider response to an operator-facing status."""
    code = str(result.get("error_code") or "").lower()
    status = result.get("status_code")
    message = str(result.get("error") or "").lower()
    if code in {"not_configured", "missing_api_key"}:
        return "not_configured"
    if code in {"invalid_api_key", "unauthorized"} or status in (401, 403):
        return "invalid_api_key"
    if code in {"quota_exhausted", "usage_limit", "plan_limit"}:
        return "quota_exhausted"
    if status in (402, 429, 432, 433) or any(
        phrase in message
        for phrase in ("quota", "usage limit", "plan limit", "credit limit", "monthly limit")
    ):
        return "quota_exhausted"
    if code in {"timeout", "timed_out"}:
        return "timeout"
    if code in {"network", "connection"}:
        return "network_error"
    return "provider_error"


def tavily_search(
    query: str,
    max_results: int = MAX_RESULTS_PER_QUERY,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Call Tavily's search endpoint without an SDK dependency.

    The return value always includes ``ok`` so callers cannot mistake a failed
    call for an empty-but-successful search.
    """
    api_key = get_api_key()
    if not api_key:
        return {
            "ok": False,
            "results": [],
            "error_code": "not_configured",
            "error": "TAVILY_API_KEY is not configured on the server.",
            "status_code": None,
        }
    timeout_seconds = timeout or int(os.getenv("TAVILY_TIMEOUT_SECONDS", str(DEFAULT_SEARCH_TIMEOUT)))
    payload = json.dumps(
        {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
    ).encode("utf-8")
    request = Request(
        PROVIDER_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return {
                "ok": True,
                "results": list(data.get("results") or []),
                "answer": data.get("answer"),
                "status_code": getattr(response, "status", 200),
                "error": None,
                "error_code": None,
            }
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        code = "quota_exhausted" if exc.code in (402, 429, 432, 433) else "provider_error"
        if exc.code in (401, 403):
            code = "invalid_api_key"
        return {
            "ok": False,
            "results": [],
            "status_code": exc.code,
            "error_code": code,
            "error": f"Tavily HTTP {exc.code}: {detail or exc.reason}",
        }
    except (socket.timeout, TimeoutError) as exc:
        return {
            "ok": False,
            "results": [],
            "status_code": None,
            "error_code": "timeout",
            "error": f"Tavily request timed out after {timeout_seconds}s: {exc}",
        }
    except URLError as exc:
        return {
            "ok": False,
            "results": [],
            "status_code": None,
            "error_code": "network_error",
            "error": f"Tavily network error: {exc.reason}",
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "results": [],
            "status_code": None,
            "error_code": "provider_error",
            "error": f"Tavily returned an unreadable response: {exc}",
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected Tavily failure")
        return {
            "ok": False,
            "results": [],
            "status_code": None,
            "error_code": "provider_error",
            "error": f"Tavily request failed: {exc}",
        }


# ---------------------------------------------------------------------------
# Source / result parsing
# ---------------------------------------------------------------------------

def build_queries(source: Mapping[str, str]) -> List[str]:
    domain = source["search_domain"]
    return [
        f"site:{domain} printer model price Bangladesh",
        f"site:{domain} ink toner cartridge price Bangladesh",
    ]


def match_source(url: str) -> Optional[Mapping[str, str]]:
    lowered = (url or "").lower()
    for source in BANGLADESH_MARKET_SOURCES:
        if source["url_contains"].lower() in lowered:
            return source
    return None


def _display_brand(normalized: str) -> str:
    for brand in _COMMON_BRANDS:
        if _normalize(brand) == normalized:
            return brand
    return normalized.title()


def detect_brand(text: str, known_brands: Iterable[str] = ()) -> str:
    upper = _normalize(text)
    blocked = {_normalize(name) for name in _GENERIC_BRAND_NAMES}
    brands = {
        _normalize(brand)
        for brand in _COMMON_BRANDS
        if _normalize(brand) not in blocked
    }
    brands.update({
        _normalize(brand)
        for brand in known_brands
        if brand and _normalize(brand) not in blocked
    })
    for alias, canonical in _BRAND_ALIASES.items():
        if _normalize(alias) in brands:
            brands.discard(_normalize(alias))
            brands.add(_normalize(canonical))
    best = ""
    for brand in brands:
        if not brand:
            continue
        pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(brand)}(?![A-Z0-9])")
        if pattern.search(upper) and len(brand) > len(best):
            best = brand
    return _display_brand(best) if best else "Unknown"


def classify_category(text: str) -> str:
    upper = _normalize(text)
    if re.search(r"\b(TONER|DRUM UNIT)\b", upper):
        return "Toner Cartridge"
    if re.search(r"\bRIBBON\b", upper):
        return "Ribbon"
    if re.search(r"\bCARTRIDGE\b", upper):
        return "Ink" if re.search(r"\bINK\b", upper) else "Toner Cartridge"
    if re.search(r"\b(PRINTER|PRINT|MFP|ALL IN ONE|MULTIFUNCTION|PLOTTER)\b", upper):
        return "Printer"
    if re.search(r"\b(INK|BOTTLE|REFILL|ECOTANK|MEGATANK)\b", upper):
        return "Ink"
    if re.search(r"\b(PAPER|ACCESSORY|ACCESSORIES)\b", upper):
        return "Accessory"
    return "Other"


def _is_price_context(text: str, start: int, end: int) -> bool:
    window_before = text[max(0, start - 18):start]
    window_after = text[end:end + 18]
    context = f"{window_before} {window_after}"
    return bool(re.search(r"(৳|TK\.?|BDT|TAKA|PRICE|USD|\$|EUR|GBP)", context, re.IGNORECASE))


def _clean_model_token(token: str, brand: str) -> str:
    token = re.sub(r"\s+", " ", token.strip())
    if brand and brand != "Unknown":
        compact_brand = _compact(brand)
        compact_token = _compact(token)
        if compact_token.startswith(compact_brand) and len(compact_token) > len(compact_brand):
            remainder = compact_token[len(compact_brand):]
            if re.search(r"\d", remainder):
                token = re.sub(
                    rf"^{re.escape(brand)}\s*[- ]?\s*", "", token, flags=re.IGNORECASE
                ).strip()
    return token


def extract_model(text: str, brand: str, category: str) -> Optional[str]:
    """Extract a conservative model token from a product title/page snippet."""
    if not text:
        return None
    upper = re.sub(r"<[^>]+>", " ", text.upper())
    candidates: List[tuple[int, int, str]] = []

    for match in _MODEL_RE.finditer(upper):
        raw = match.group(0)
        compact = _compact(raw)
        if not compact or _YEAR_RE.match(compact):
            continue
        if compact.isdigit():
            continue
        if compact[:3] in _EXCLUDED_MODEL_TOKENS:
            continue
        if _compact(raw).startswith(("CAT", "SKU", "MODEL", "ITEM")):
            continue
        if _is_price_context(upper, match.start(), match.end()):
            continue
        model = _clean_model_token(raw, brand)
        if not re.search(r"[A-Z]", model) or not re.search(r"\d", model):
            continue
        if _YEAR_RE.match(_compact(model)):
            continue
        score = 0
        before = upper[max(0, match.start() - 45):match.start()]
        after = upper[match.end():match.end() + 45]
        if brand and brand != "Unknown" and _compact(brand) in _compact(before + " " + after):
            score += 4
        if any(word in before or word in after for word in _SERIES_WORDS):
            score += 3
        if "-" in raw:
            score += 2
        score += min(4, len(re.sub(r"\D", "", model)))
        candidates.append((score, match.start(), model))

    if not candidates and category in ("Ink", "Toner Cartridge", "Ribbon"):
        for match in _NUMERIC_MODEL_RE.finditer(upper):
            number = match.group(0)
            if _YEAR_RE.match(number):
                continue
            if _is_price_context(upper, match.start(), match.end()):
                continue
            window = upper[max(0, match.start() - 35):match.end() + 35]
            if not _INK_CONTEXT_RE.search(window):
                continue
            candidates.append((2, match.start(), number))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2][:160]


def extract_variant(text: str) -> Optional[str]:
    upper = _normalize(text)
    found: List[str] = []
    for color in _COLOR_VARIANTS:
        if re.search(rf"\b{re.escape(_normalize(color))}\b", upper):
            if color == "Color" and any(item in found for item in ("Tri-Color",)):
                continue
            found.append(color)
    for pack in _PACK_VARIANTS:
        if re.search(rf"\b{re.escape(_normalize(pack))}\b", upper):
            found.append(pack)
    ml = re.search(r"\b(\d{1,4})\s?ML\b", upper)
    if ml:
        found.append(f"{ml.group(1)}ml")
    unique: List[str] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return ", ".join(unique) if unique else None


def parse_price(text: str) -> tuple[Optional[float], Optional[str]]:
    for currency, patterns in _CURRENCY_PATTERNS:
        for pattern in patterns:
            for match in pattern.finditer(text or ""):
                raw = match.group(1).replace(",", "")
                try:
                    amount = float(raw)
                except ValueError:
                    continue
                if amount <= 0 or amount >= 10_000_000:
                    continue
                return round(amount, 2), currency
    return None, None


def parse_availability(text: str) -> str:
    for label, pattern in _AVAILABILITY_PATTERNS:
        if pattern.search(text or ""):
            return label
    return "Unknown"


def detect_demand(text: str, model: str = "", brand: str = "") -> tuple[str, Optional[str]]:
    """Return an explicit demand label only when the source actually says it."""
    model_compact = _compact(model)
    brand_compact = _compact(brand) if brand and brand != "Unknown" else ""
    title_like, _, body = (text or "").partition("\n")
    for status, pattern in _DEMAND_PATTERNS:
        title_match = pattern.search(title_like)
        if title_match:
            return status, " ".join(title_like.split())[:500]
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body or ""):
            if not pattern.search(sentence):
                continue
            compact_sentence = _compact(sentence)
            if model_compact and model_compact in compact_sentence:
                return status, " ".join(sentence.split())[:500]
            if brand_compact and brand_compact in compact_sentence:
                return status, " ".join(sentence.split())[:500]
    return "demand_unknown", None


def detect_release(text: str, source_date: Optional[date]) -> tuple[str, Optional[date], Optional[str]]:
    release_sentence = _sentence_with(text or "", _RELEASE_RE)
    if not release_sentence:
        return "first_observed", None, None
    explicit = _DATE_TEXT_RE.search(text or "")
    if explicit:
        parsed = _iso_date(explicit.group(1))
        if parsed:
            return "new_release", parsed, release_sentence
    return "first_observed", None, None


def _base_observation(
    result: Mapping[str, Any],
    source: Mapping[str, str],
    collected_at: datetime,
    known_brands: Iterable[str],
    previous_first_observed: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    title = _clean_source_title(str(result.get("title") or ""))
    content = str(result.get("content") or result.get("raw_content") or "")
    url = str(result.get("url") or "").strip()
    if not title or not url:
        return None
    category = classify_category(title)
    if category not in ("Printer", "Ink", "Toner Cartridge", "Ribbon"):
        return None
    brand = detect_brand(title, known_brands)
    model = extract_model(title, brand, category)
    if not model:
        return None
    if brand == "Unknown":
        content_brand = detect_brand(content[:600], known_brands)
        if content_brand != "Unknown":
            brand = content_brand
    combined = f"{title}\n{content}"
    variant = extract_variant(combined)
    price_amount, price_currency = parse_price(combined)
    availability = parse_availability(combined)
    source_date = _iso_date(result.get("published_date") or result.get("publishedDate"))
    demand_status, demand_evidence = detect_demand(combined, model, brand)
    release_status, release_date, release_evidence = detect_release(combined, source_date)
    first_observed = previous_first_observed or collected_at.date()
    source_domain = urlparse(url).netloc.lower().replace("www.", "")
    return {
        "observation_key": repo.observation_key(brand, model, category, source_domain),
        "brand": brand,
        "model": model,
        "variant": variant,
        "category": category,
        "source_name": source["name"],
        "source_url": url[:1000],
        "source_domain": source_domain[:200],
        "source_date": source_date,
        "collected_at": collected_at,
        "price_amount": price_amount,
        "price_currency": price_currency,
        "availability": availability,
        "release_status": release_status,
        "release_date": release_date,
        "release_evidence": release_evidence,
        "first_observed_date": first_observed,
        "demand_status": demand_status,
        "demand_evidence": demand_evidence,
        "demand_source_url": url[:1000] if demand_status != "demand_unknown" else None,
        "local_match_status": "not_found",
        "local_product_id": None,
        "local_product_name": None,
        "local_category": None,
        "local_stock": None,
        "local_units_sold": None,
        "local_last_sold": None,
        "local_compare_note": None,
        "raw_title": title,
    }


def is_displayable_observation(observation: Mapping[str, Any]) -> bool:
    """Reject legacy rows that are not product-specific printer/ink evidence."""
    title = str(observation.get("raw_title") or "")
    category = classify_category(title)
    if category not in ("Printer", "Ink", "Toner Cartridge", "Ribbon"):
        return False
    brand = str(observation.get("brand") or "")
    return bool(extract_model(title, brand, category))


def parse_provider_results(
    results: Sequence[Mapping[str, Any]],
    source: Mapping[str, str],
    collected_at: datetime,
    known_brands: Iterable[str] = (),
    previous_first_observed: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Convert synthetic or live Tavily results into model observations."""
    observations: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for result in results or []:
        url = str(result.get("url") or "")
        matched_source = match_source(url)
        if not matched_source:
            continue
        observation = _base_observation(
            result, matched_source, collected_at, known_brands, previous_first_observed
        )
        if not observation:
            continue
        key = observation["observation_key"]
        if key in seen:
            continue
        seen.add(key)
        observations.append(observation)
    return observations


# ---------------------------------------------------------------------------
# Complete local catalog comparison
# ---------------------------------------------------------------------------

def fetch_catalog(conn=None) -> List[Dict[str, Any]]:
    """Read the complete catalog, including products with no recent sales."""
    from db.connection import get_connection

    query = """
        SELECT
            p.PRODUCT_ID AS product_id,
            p.PRODUCT_NAME AS product_name,
            p.SHORT_NAME AS short_name,
            COALESCE(b.BRAND_NAME, '') AS brand_name,
            p.CATEGORY_ID AS category_id,
            COALESCE(c.CATEGORY_NAME, 'General') AS category_name,
            CASE
                WHEN ns.stock_qty IS NULL AND ss.stock_qty IS NULL THEN NULL
                ELSE COALESCE(ns.stock_qty, 0) + COALESCE(ss.stock_qty, 0)
            END AS stock_qty,
            COALESCE(sd.units_sold, 0) AS units_sold,
            sd.last_sold_date AS last_sold_date
        FROM product p
        LEFT JOIN brand b ON p.BRAND_ID = b.BRAND_ID
        LEFT JOIN category c ON p.CATEGORY_ID = c.CATEGORY_ID
        LEFT JOIN (
            SELECT PRODUCT_ID, SUM(STOCK_QTY) AS stock_qty
            FROM stock_non_serial GROUP BY PRODUCT_ID
        ) ns ON p.PRODUCT_ID = ns.PRODUCT_ID
        LEFT JOIN (
            SELECT PRODUCT_ID, SUM(STOCK_QTY) AS stock_qty
            FROM stock_serial GROUP BY PRODUCT_ID
        ) ss ON p.PRODUCT_ID = ss.PRODUCT_ID
        LEFT JOIN (
            SELECT PRODUCT_ID, SUM(QTY) AS units_sold, MAX(SALES_DATE) AS last_sold_date
            FROM sales_details GROUP BY PRODUCT_ID
        ) sd ON p.PRODUCT_ID = sd.PRODUCT_ID
        ORDER BY p.PRODUCT_NAME ASC
    """
    try:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(query)
                return list(cur.fetchall())
        with get_connection() as c:
            with c.cursor() as cur:
                cur.execute(query)
                return list(cur.fetchall())
    except Exception as exc:
        logger.warning("Local catalog comparison unavailable: %s", exc)
        raise CatalogUnavailableError("The local product catalog could not be read.") from exc


def _model_numeric_core(model: str) -> str:
    compact = _compact(model)
    match = re.search(r"(\d{2,})", compact)
    return match.group(1) if match else ""


def _model_letter_prefix(model: str) -> str:
    compact = _compact(model)
    match = re.match(r"([A-Z]+)", compact)
    return match.group(1) if match else ""


def compare_against_catalog(
    observation: Mapping[str, Any], catalog: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Compare brand/model/variant against complete local catalog rows."""
    brand = str(observation.get("brand") or "").strip()
    model = str(observation.get("model") or "").strip()
    variant = str(observation.get("variant") or "").strip()
    if not model:
        return {
            "local_match_status": "not_found",
            "local_product_id": None,
            "local_product_name": None,
            "local_category": None,
            "local_stock": None,
            "local_units_sold": None,
            "local_last_sold": None,
            "local_compare_note": "No model token was extracted.",
        }

    brand_norm = _normalize(brand)
    brand_known = bool(brand_norm) and brand_norm != "UNKNOWN"
    model_norm = _normalize(model)
    model_pattern = (
        re.compile(rf"(?<![A-Z0-9]){re.escape(model_norm)}(?![A-Z0-9])")
        if model_norm
        else None
    )
    variant_norm = _normalize(variant)
    numeric_core = _model_numeric_core(model)
    letter_prefix = _model_letter_prefix(model)

    exact: List[tuple[int, Mapping[str, Any]]] = []
    possible: List[tuple[int, Mapping[str, Any], str]] = []

    for row in catalog:
        name_norm = _normalize(str(row.get("product_name") or ""))
        short_norm = _normalize(str(row.get("short_name") or ""))
        row_brand_norm = _normalize(str(row.get("brand_name") or ""))
        brand_match = bool(
            brand_known
            and row_brand_norm
            and (
                brand_norm == row_brand_norm
                or brand_norm in row_brand_norm
                or row_brand_norm in brand_norm
            )
        )
        model_match = bool(
            model_pattern
            and (model_pattern.search(name_norm) or model_pattern.search(short_norm))
        )
        variant_match = bool(
            not variant_norm
            or variant_norm in name_norm
            or variant_norm in short_norm
        )

        if brand_match and model_match:
            if variant_match:
                exact.append((0 if variant else 1, row))
            else:
                possible.append((0, row, "Same brand and model token, but the variant differs."))
            continue

        if model_match and not brand_known:
            possible.append((1, row, "Model token matches, but the source did not verify the brand."))
            continue
        if model_match and brand_known and not row_brand_norm:
            possible.append((2, row, "Model token matches, but the catalog brand is blank."))
            continue
        if brand_match and numeric_core:
            row_core = _model_numeric_core(str(row.get("product_name") or ""))
            row_letters = _model_letter_prefix(str(row.get("product_name") or ""))
            if row_core == numeric_core and letter_prefix and row_letters.startswith(letter_prefix[:1]):
                possible.append((3, row, "Same brand and model number family, but not an exact name match."))

    exact.sort(key=lambda item: item[0])
    possible.sort(key=lambda item: item[0])
    chosen = exact[0][1] if exact else (possible[0][1] if possible else None)
    if chosen:
        status = "already_in_catalog" if exact else "possible_match"
        note = None
        if possible and not exact:
            note = possible[0][2]
        stock = chosen.get("stock_qty")
        units = chosen.get("units_sold")
        return {
            "local_match_status": status,
            "local_product_id": chosen.get("product_id"),
            "local_product_name": chosen.get("product_name"),
            "local_category": chosen.get("category_name"),
            "local_stock": float(stock) if stock is not None else None,
            "local_units_sold": float(units) if units is not None else None,
            "local_last_sold": chosen.get("last_sold_date"),
            "local_compare_note": note,
        }
    return {
        "local_match_status": "not_found",
        "local_product_id": None,
        "local_product_name": None,
        "local_category": None,
        "local_stock": None,
        "local_units_sold": None,
        "local_last_sold": None,
        "local_compare_note": "No catalog product matched this brand/model/variant.",
    }


# ---------------------------------------------------------------------------
# Provider and persisted snapshot
# ---------------------------------------------------------------------------

def _provider_health_from_run(run: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not provider_configured():
        return {
            "configured": False,
            "status": "not_configured",
            "verified": False,
            "message": "TAVILY_API_KEY is not configured on the server. Market research cannot start.",
            "last_checked": None,
        }
    if not run:
        return {
            "configured": True,
            "status": "configured_unverified",
            "verified": False,
            "message": "Provider is configured but has not completed a verified live search yet.",
            "last_checked": None,
        }
    run_status = str(run.get("status") or "")
    stored_status = str(run.get("provider_status") or "unknown")
    calls_ok = int(run.get("provider_calls_ok") or 0)
    calls_failed = int(run.get("provider_calls_failed") or 0)
    last_checked = run.get("completed_at") or run.get("updated_at") or run.get("created_at")
    if run_status in ("pending", "running"):
        return {
            "configured": True,
            "status": "in_progress",
            "verified": calls_ok > 0,
            "message": "Market research is running. Provider status updates as responses arrive.",
            "last_checked": last_checked,
        }
    if stored_status == "quota_exhausted":
        return {
            "configured": True,
            "status": "quota_exhausted",
            "verified": calls_ok > 0,
            "message": "Tavily reported a quota or plan limit. No failed search is shown as completed.",
            "last_checked": last_checked,
        }
    if stored_status in ("invalid_api_key",):
        return {
            "configured": True,
            "status": "invalid_api_key",
            "verified": False,
            "message": "Tavily rejected TAVILY_API_KEY. Check the server-side key.",
            "last_checked": last_checked,
        }
    if calls_ok > 0 and calls_failed > 0:
        return {
            "configured": True,
            "status": "degraded",
            "verified": True,
            "message": "Tavily responded for part of the last run; some provider calls failed.",
            "last_checked": last_checked,
        }
    if calls_ok > 0:
        return {
            "configured": True,
            "status": "ready",
            "verified": True,
            "message": "Tavily responded successfully during the last run.",
            "last_checked": last_checked,
        }
    return {
        "configured": True,
        "status": "failed",
        "verified": False,
        "message": "Tavily did not respond successfully during the last run.",
        "last_checked": last_checked,
    }


def get_provider_health(conn=None) -> Dict[str, Any]:
    try:
        repo.init_market_research_tables(conn)
        latest = repo.get_latest_run(conn=conn)
    except Exception as exc:
        logger.warning("Could not read provider health: %s", exc)
        latest = None
    return _provider_health_from_run(latest)


def get_market_snapshot(filters: Optional[Mapping[str, Any]] = None, conn=None) -> Dict[str, Any]:
    """Return the latest persisted research run for report rendering."""
    filters = filters or {}
    try:
        repo.init_market_research_tables(conn)
        repo.mark_interrupted_runs(stale_after_minutes=STALE_RUN_MINUTES, conn=conn)
        latest = repo.get_latest_run(conn=conn)
    except Exception as exc:
        logger.warning("Market research storage unavailable: %s", exc)
        return {
            "available": False,
            "storage_error": "Market research storage is unavailable.",
            "run": None,
            "active_job": None,
            "observations": [],
            "sources": [],
            "summary": {},
            "provider": _json_safe(get_provider_health(conn=conn)),
            "usage": {
                "month_credits": 0,
                "monthly_limit": MAX_CREDITS_PER_MONTH,
                "per_research_limit": MAX_CREDITS_PER_RESEARCH,
            },
            "criteria": _criteria_notes(),
        }

    provider = _provider_health_from_run(latest)
    if not latest:
        return {
            "available": False,
            "storage_error": None,
            "run": None,
            "active_job": None,
            "observations": [],
            "sources": [],
            "summary": {},
            "provider": provider,
            "usage": {
                "month_credits": _safe_month_credits(conn),
                "monthly_limit": MAX_CREDITS_PER_MONTH,
                "per_research_limit": MAX_CREDITS_PER_RESEARCH,
            },
            "criteria": _criteria_notes(),
        }

    run_id = latest.get("run_id")
    try:
        sources = repo.get_source_statuses(run_id, conn=conn)
        stored_observations = repo.get_run_observations(run_id, conn=conn)
    except Exception as exc:
        logger.warning("Could not read market observations: %s", exc)
        sources = []
        stored_observations = []

    observations = [
        observation for observation in stored_observations
        if is_displayable_observation(observation)
    ]
    hidden_legacy_rows = len(stored_observations) - len(observations)

    visible_by_source: Dict[str, int] = {}
    for observation in observations:
        source_name = str(observation.get("source_name") or "")
        visible_by_source[source_name] = visible_by_source.get(source_name, 0) + 1
    for source in sources:
        source_name = str(source.get("source_name") or "")
        source["stored_results_count"] = int(source.get("results_count") or 0)
        source["results_count"] = visible_by_source.get(source_name, 0)

    q = str(filters.get("q") or "").strip().lower()
    brand = str(filters.get("brand") or "").strip().lower()
    market_category = str(filters.get("market_category") or "").strip().lower()
    filtered: List[Dict[str, Any]] = []
    for observation in observations:
        if q and q not in " ".join(
            str(observation.get(field) or "")
            for field in ("brand", "model", "variant", "category", "source_name")
        ).lower():
            continue
        if brand and brand != str(observation.get("brand") or "").strip().lower():
            continue
        if market_category and market_category != str(observation.get("category") or "").strip().lower():
            continue
        normalized = _json_safe(observation)
        normalized["demand_label"] = DEMAND_LABELS.get(
            normalized.get("demand_status"), "Demand Unknown"
        )
        normalized["release_label"] = RELEASE_LABELS.get(
            normalized.get("release_status"), "First Observed"
        )
        normalized["local_match_label"] = LOCAL_MATCH_LABELS.get(
            normalized.get("local_match_status"), "Comparison Unavailable"
        )
        filtered.append(normalized)

    summary = latest.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    summary = dict(summary)
    summary["results_count"] = len(observations)
    summary["visible_count"] = len(filtered)
    summary["hidden_legacy_rows"] = hidden_legacy_rows
    summary["brands"] = sorted({str(item.get("brand")) for item in observations if item.get("brand")})
    summary["categories"] = sorted({
        str(item.get("category")) for item in observations if item.get("category")
    })
    summary["demand_counts"] = _count_by(observations, "demand_status")
    summary["demand_evidenced_count"] = sum(
        1 for item in observations if item.get("demand_status") in ("bestseller", "popular")
    )
    summary["new_release_count"] = sum(
        1 for item in observations if item.get("release_status") == "new_release"
    )
    summary["first_observed_count"] = sum(
        1 for item in observations if item.get("release_status") == "first_observed"
    )
    summary["comparison_counts"] = _count_by(observations, "local_match_status")
    summary["comparison_available"] = not bool(latest.get("summary", {}).get("catalog_error"))

    source_status = _json_safe(sources)
    coverage = {
        "total": int(latest.get("sources_total") or len(sources)),
        "available": int(latest.get("sources_available") or 0),
        "no_results": int(latest.get("sources_no_results") or 0),
        "failed": int(latest.get("sources_failed") or 0),
    }
    run_data = _json_safe({
        key: latest.get(key)
        for key in (
            "run_id", "user_id", "status", "stage", "progress", "provider",
            "provider_status", "provider_calls_ok", "provider_calls_failed",
            "sources_total", "sources_available", "sources_no_results",
            "sources_failed", "queries_attempted", "credits_used",
            "results_count", "error", "summary", "started_at", "completed_at",
            "created_at", "updated_at",
        )
    })
    active_job = None
    if latest.get("status") in ("pending", "running"):
        active_job = {
            "job_id": run_id,
            "status": latest.get("status"),
            "stage": latest.get("stage"),
            "progress": latest.get("progress"),
        }
    return {
        "available": True,
        "storage_error": None,
        "run": run_data,
        "active_job": active_job,
        "provider": _json_safe(provider),
        "sources": source_status,
        "coverage": coverage,
        "observations": filtered,
        "summary": summary,
        "usage": {
            "month_credits": _safe_month_credits(conn),
            "monthly_limit": MAX_CREDITS_PER_MONTH,
            "per_research_limit": MAX_CREDITS_PER_RESEARCH,
        },
        "criteria": _criteria_notes(),
        "filters": {
            "q": filters.get("q") or "",
            "brand": filters.get("brand") or "",
            "market_category": filters.get("market_category") or "",
        },
    }


def _safe_month_credits(conn=None) -> int:
    try:
        return repo.monthly_credit_total(datetime.now().strftime("%Y-%m"), conn=conn)
    except Exception:
        return 0


def _count_by(rows: Sequence[Mapping[str, Any]], field: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _criteria_notes() -> Dict[str, str]:
    return {
        "release": "New Release requires a source-provided release date plus release wording. Otherwise the row shows First Observed with the date this system first stored it.",
        "demand": "Bestseller or Popular requires explicit source wording and a source link. A search result or product listing alone is Demand Unknown.",
        "price": "Prices are shown only in the currency supplied by the source. BDT is never inferred or converted.",
        "catalog": "Local comparison checks brand, model and variant against the complete product catalog, including products with no recent sales. Similar names are only a Possible Match.",
        "coverage": "A source that cannot be reached is recorded as failed. Partial runs are never labelled completed.",
    }


# ---------------------------------------------------------------------------
# Research lifecycle
# ---------------------------------------------------------------------------

def _fail_run(run_id: str, status: str, stage: str, message: str,
              provider_status: str, summary: Optional[Mapping[str, Any]] = None) -> None:
    fields: Dict[str, Any] = {
        "status": status,
        "stage": stage,
        "error": message[:600],
        "provider_status": provider_status,
        "completed_at": datetime.now(),
    }
    if summary is not None:
        fields["summary"] = summary
    repo.update_run(run_id, **fields)


def run_research_job(
    run_id: str,
    search_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    catalog_fn: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """Execute one research run with an injectable provider for tests."""
    search = search_fn or tavily_search
    get_catalog = catalog_fn or fetch_catalog
    now = now_fn or datetime.now
    started_at = now()
    period = started_at.strftime("%Y-%m")
    repo.init_market_research_tables()
    for source in BANGLADESH_MARKET_SOURCES:
        repo.upsert_source_status(
            run_id,
            source["name"],
            source["type"],
            source["base_url"],
            "not_checked",
            0,
            None,
            None,
        )

    if not provider_configured() and search_fn is None:
        _fail_run(
            run_id,
            "failed",
            "provider_not_configured",
            "TAVILY_API_KEY is not configured on the server. No search was performed.",
            "not_configured",
        )
        return repo.get_run(run_id) or {}

    repo.update_run(
        run_id,
        status="running",
        stage="checking_limits",
        progress=5,
        started_at=started_at,
        provider_status="in_progress",
    )

    month_total = repo.monthly_credit_total(period)
    if month_total >= MAX_CREDITS_PER_MONTH:
        _fail_run(
            run_id,
            "failed",
            "monthly_credit_limit",
            "Monthly Tavily credit limit reached (900 credits). No search was performed.",
            "quota_exhausted",
        )
        return repo.get_run(run_id) or {}

    try:
        catalog = list(get_catalog())
        catalog_error = None
    except Exception as exc:
        catalog = []
        catalog_error = str(exc)[:300] or "The local catalog could not be read."

    known_brands = {
        str(row.get("brand_name") or "").strip()
        for row in catalog
        if row.get("brand_name")
    }

    repo.update_run(run_id, stage="searching_sources", progress=10)
    all_observations: List[Dict[str, Any]] = []
    seen_observation_keys: set[str] = set()
    provider_ok = 0
    provider_failed = 0
    provider_status = "in_progress"
    abort_reason: Optional[str] = None
    any_source_failed = False
    total_sources = len(BANGLADESH_MARKET_SOURCES)

    for index, source in enumerate(BANGLADESH_MARKET_SOURCES, start=1):
        if abort_reason:
            break
        source_errors: List[str] = []
        source_reached = False
        source_results: List[Dict[str, Any]] = []

        for query in build_queries(source):
            if abort_reason:
                break
            reservation = repo.reserve_credits(
                run_id,
                period,
                CREDIT_COST_PER_SEARCH,
                MAX_CREDITS_PER_MONTH,
                MAX_CREDITS_PER_RESEARCH,
            )
            if not reservation.get("ok"):
                reason = reservation.get("reason")
                if reason == "monthly_limit_reached":
                    provider_status = "quota_exhausted"
                    abort_reason = "Monthly Tavily credit limit reached (900 credits)."
                elif reason == "run_limit_reached":
                    provider_status = "run_limit_reached"
                    abort_reason = "Per-research Tavily credit limit reached (50 credits)."
                else:
                    provider_status = "failed"
                    abort_reason = "Credit ledger lock was unavailable; no provider call was made."
                break

            result = search(query)
            if not result.get("ok"):
                provider_failed += 1
                classified = classify_provider_error(result)
                message = str(result.get("error") or "Tavily request failed.")[:300]
                source_errors.append(message)
                if classified == "quota_exhausted":
                    provider_status = "quota_exhausted"
                    abort_reason = message
                    break
                if classified == "invalid_api_key":
                    provider_status = "invalid_api_key"
                    abort_reason = message
                    break
                continue

            provider_ok += 1
            source_reached = True
            results = list(result.get("results") or [])
            if not results:
                continue
            observations = parse_provider_results(
                results,
                source,
                now(),
                known_brands=known_brands,
            )
            for observation in observations:
                if observation["observation_key"] in seen_observation_keys:
                    continue
                seen_observation_keys.add(observation["observation_key"])
                previous = repo.get_previous_first_observed(
                    observation["brand"],
                    observation["model"],
                    observation["category"],
                )
                if previous:
                    observation["first_observed_date"] = previous
                comparison = compare_against_catalog(observation, catalog)
                observation.update(comparison)
                if catalog_error:
                    observation["local_match_status"] = "comparison_unavailable"
                    observation["local_compare_note"] = "The local catalog could not be read."
                repo.insert_observation(run_id, observation)
                source_results.append(observation)
                all_observations.append(observation)

        if abort_reason:
            if provider_status == "in_progress":
                provider_status = "failed"
            any_source_failed = True
            repo.upsert_source_status(
                run_id,
                source["name"],
                source["type"],
                source["base_url"],
                "failed",
                len(source_results),
                "; ".join(source_errors) or abort_reason,
                now(),
            )
            break

        if source_results:
            source_status = "available"
        elif source_reached:
            source_status = "no_results"
        else:
            source_status = "failed"
            any_source_failed = True
        repo.upsert_source_status(
            run_id,
            source["name"],
            source["type"],
            source["base_url"],
            source_status,
            len(source_results),
            "; ".join(source_errors) or None,
            now(),
        )
        progress = 10 + int((index / max(1, total_sources)) * 75)
        repo.update_run(
            run_id,
            progress=min(85, progress),
            stage="searching_sources",
            results_count=len(all_observations),
            queries_attempted=provider_ok + provider_failed,
            credits_used=repo.run_credit_total(run_id),
        )

    sources = repo.get_source_statuses(run_id)
    coverage = {
        "available": sum(1 for row in sources if row.get("status") == "available"),
        "no_results": sum(1 for row in sources if row.get("status") == "no_results"),
        "failed": sum(1 for row in sources if row.get("status") == "failed"),
    }
    credits_used = repo.run_credit_total(run_id)
    if provider_ok and provider_failed == 0 and not abort_reason:
        provider_status = "ready"
    elif provider_ok and provider_failed:
        provider_status = "degraded"

    if abort_reason and provider_status in ("in_progress", "ready", "degraded"):
        provider_status = "failed"

    if provider_ok == 0:
        final_status = "failed"
        stage = "provider_failed"
        if provider_status in ("in_progress", "ready", "degraded"):
            provider_status = "failed"
        error = abort_reason or (
            "Tavily did not return a successful response for any source. "
            "No search was reported as completed."
        )
    elif abort_reason or any_source_failed or provider_failed:
        final_status = "partial"
        stage = "partial"
        error = abort_reason or (
            f"{provider_failed} provider call(s) failed or timed out. "
            "Only evidence collected before the failure is shown."
        )
    else:
        final_status = "completed"
        stage = "finished"
        error = None

    if catalog_error:
        for observation in all_observations:
            if observation.get("local_match_status") != "comparison_unavailable":
                observation["local_match_status"] = "comparison_unavailable"
                observation["local_compare_note"] = "The local catalog could not be read."
                repo.insert_observation(run_id, observation)

    summary = {
        "queries_planned": sum(len(build_queries(source)) for source in BANGLADESH_MARKET_SOURCES),
        "queries_attempted": provider_ok + provider_failed,
        "provider_calls_ok": provider_ok,
        "provider_calls_failed": provider_failed,
        "sources_total": total_sources,
        "sources_available": coverage["available"],
        "sources_no_results": coverage["no_results"],
        "sources_failed": coverage["failed"],
        "results_count": len(all_observations),
        "new_release_count": sum(
            1 for item in all_observations if item.get("release_status") == "new_release"
        ),
        "first_observed_count": sum(
            1 for item in all_observations if item.get("release_status") == "first_observed"
        ),
        "demand_evidenced_count": sum(
            1 for item in all_observations if item.get("demand_status") != "demand_unknown"
        ),
        "demand_unknown_count": sum(
            1 for item in all_observations if item.get("demand_status") == "demand_unknown"
        ),
        "comparison_counts": _count_by(all_observations, "local_match_status"),
        "catalog_error": catalog_error,
        "comparison_available": not bool(catalog_error),
        "credits_used": credits_used,
        "credit_limits": {
            "per_research": MAX_CREDITS_PER_RESEARCH,
            "per_month": MAX_CREDITS_PER_MONTH,
        },
    }
    repo.update_run(
        run_id,
        status=final_status,
        stage=stage,
        progress=100,
        provider_status=provider_status,
        provider_calls_ok=provider_ok,
        provider_calls_failed=provider_failed,
        sources_total=total_sources,
        sources_available=coverage["available"],
        sources_no_results=coverage["no_results"],
        sources_failed=coverage["failed"],
        queries_attempted=provider_ok + provider_failed,
        credits_used=credits_used,
        results_count=len(all_observations),
        error=error,
        summary=summary,
        completed_at=now(),
    )
    return repo.get_run(run_id) or {}


def _run_job_safely(run_id: str) -> None:
    try:
        run_research_job(run_id)
    except Exception as exc:  # pragma: no cover - defensive background guard
        logger.exception("Market research job %s failed unexpectedly", run_id)
        try:
            repo.update_run(
                run_id,
                status="failed",
                stage="provider_failed",
                error=f"Research job failed unexpectedly: {str(exc)[:500]}",
                provider_status="failed",
                completed_at=datetime.now(),
            )
        except Exception:
            logger.exception("Could not persist failure for market research job %s", run_id)


def start_product_research_job(user_id: Any) -> str:
    """Create a queued run and start its background worker."""
    repo.init_market_research_tables()
    with _START_LOCK:
        active = repo.get_active_run()
        if active:
            raise DuplicateResearchJobError(str(active.get("run_id")))
        run_id = f"mr-{uuid.uuid4().hex[:12]}"
        queries_planned = sum(len(build_queries(source)) for source in BANGLADESH_MARKET_SOURCES)
        repo.create_run(
            run_id,
            int(user_id) if str(user_id).isdigit() else 1,
            PROVIDER_NAME,
            len(BANGLADESH_MARKET_SOURCES),
            queries_planned,
        )
    thread = threading.Thread(
        target=_run_job_safely,
        args=(run_id,),
        daemon=True,
        name=f"market-research-{run_id}",
    )
    thread.start()
    return run_id


def get_research_status(job_id: str) -> Optional[Dict[str, Any]]:
    repo.init_market_research_tables()
    run = repo.get_run(job_id)
    if not run:
        return None
    return _json_safe(run)


def initialize() -> None:
    """Create tables and mark pre-restart jobs interrupted."""
    repo.init_market_research_tables()
    repo.mark_interrupted_runs()
