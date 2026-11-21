from fastapi import FastAPI, Request, Response, Query
from typing import Optional
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from starlette.concurrency import run_in_threadpool
import json
import logging
import threading
import io
import time
from collections import defaultdict, deque
import database
from ai_service import (
    AIServiceError,
    answer_question,
    build_business_context,
    context_metadata,
    generate_forecast,
    generate_insights,
    stream_answer_question,
    stream_forecast,
    stream_insights,
)
from database import (
    save_ai_chat,
    get_ai_chat_history,
    get_session_user,
)
from datetime import date

try:
    import product_research_service
except ImportError:
    product_research_service = None

logger = logging.getLogger(__name__)
_LOGIN_ATTEMPTS = defaultdict(deque)
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 5

app = FastAPI(
    title="Printer Analytics Dashboard",
    description="Product peak period analysis and sales intelligence powered by FastAPI.",
    version="2.1.0",
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

def is_authenticated(request: Request) -> bool:
    """Check if incoming request has a valid session cookie."""
    token = request.cookies.get("session_token")
    return database.verify_session(token)


def require_ai_auth(request: Request) -> JSONResponse | None:
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    return None


def _get_request_user(request: Request):
    """Retrieve authenticated user object from session token, or None."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    try:
        return database.get_session_user(token)
    except Exception:
        return None


def _require_admin(request: Request):
    """Ensure user is logged in and possesses the Admin role."""
    user = _get_request_user(request)
    if not user:
        return None, JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    role = str(user.get("role", "")).strip().lower()
    if role != "admin":
        return None, JSONResponse(status_code=403, content={"success": False, "message": "Admin privileges required."})
    return user, None


def _append_xlsx_row(sheet, values):
    """Append a row and force text cells to stay text so values like '=1+1' are not stored as formulas."""
    sheet.append(values)
    row = sheet.max_row
    for column, value in enumerate(values, start=1):
        if isinstance(value, str):
            sheet.cell(row=row, column=column).data_type = "s"


# ==============================================================================
# AUTHENTICATION ROUTES (LOGIN / LOGOUT)
# ==============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render Login Page. Redirect to dashboard if already authenticated."""
    if is_authenticated(request):
        return RedirectResponse(url="/menus/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/api/auth/login")
async def process_login(request: Request, response: Response):
    """Authenticate credentials and set secure session cookie."""
    payload = await request.json()
    username = payload.get("username", "").strip()
    password = payload.get("password", "").strip()
    remember = payload.get("remember", False)
    client_ip = request.client.host if request.client else "unknown"
    attempts = _LOGIN_ATTEMPTS[client_ip]
    now = time.time()
    while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return JSONResponse(status_code=429, content={"success": False, "message": "Too many login attempts. Try again later."})

    user = database.authenticate_user(username, password, str(payload.get("otp", "")).strip())
    if not user:
        attempts.append(now)
        try:
            database.audit_event(None, "login_failed", f"username={username}", client_ip)
        except Exception:
            logger.exception("Could not write login audit event")
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "Invalid username or password. Please try again."}
        )

    # Valid login -> create session
    attempts.clear()
    days = 30 if remember else 1
    raw_token = database.create_session(user["id"], days=days)
    try:
        database.audit_event(user["id"], "login_success", "", client_ip)
    except Exception:
        logger.exception("Could not write login audit event")
    
    json_resp = JSONResponse(content={"success": True, "redirect": "/menus/dashboard"})
    json_resp.set_cookie(
        key="session_token",
        value=raw_token,
        max_age=days * 86400,
        httponly=True,
        samesite="lax",
    )
    return json_resp


@app.get("/logout")
async def logout(request: Request):
    """Clear session token from database and cookie, redirect to login."""
    token = request.cookies.get("session_token")
    if token:
        user = None
        try:
            user = database.get_session_user(token)
        except Exception:
            logger.exception("Could not read session for logout audit")
        database.destroy_session(token)
        try:
            database.audit_event(user["id"] if user else None, "logout")
        except Exception:
            logger.exception("Could not write logout audit event")
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(key="session_token")
    return resp


# ==============================================================================
# PROTECTED DASHBOARD ROUTES
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def root_home(request: Request):
    """Admin arrives at full executive dashboard or login if not authenticated."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/menus/dashboard", response_class=HTMLResponse)
async def executive_dashboard_page(request: Request):
    """Full comprehensive business operations dashboard."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/menus/main", response_class=HTMLResponse)
async def main_menus_page(request: Request):
    """Page to view, add, edit, and delete Main Menus."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "main_menus.html")


@app.get("/menus/sub", response_class=HTMLResponse)
async def sub_menus_page(request: Request):
    """Page to view, add, edit, and delete Submenus."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "sub_menus.html")


@app.get("/user", response_class=HTMLResponse)
async def user_page_redirect(request: Request):
    """Convenience redirect from /user to /menus/user."""
    return RedirectResponse(url="/menus/user", status_code=303)


@app.get("/menus/user", response_class=HTMLResponse)
async def user_management_page(request: Request):
    """Page to view, add, and edit dashboard users."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "user_management.html")


# ==============================================================================
# USER MANAGEMENT API
# ==============================================================================

@app.get("/api/users")
async def get_users_list(request: Request):
    """Get all dashboard users."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return database.get_dashboard_users_list()


@app.post("/api/users")
async def create_user(request: Request):
    """Create a new dashboard user."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    payload = await request.json()
    try:
        new_id = database.create_dashboard_user(payload)
        return {"success": True, "id": new_id, "message": "User created successfully"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "message": str(e)})


@app.put("/api/users/{user_id}")
async def update_user(user_id: int, request: Request):
    """Update an existing dashboard user."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    payload = await request.json()
    success = database.update_dashboard_user(user_id, payload)
    return {"success": success, "message": "User updated successfully" if success else "User not found"}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    """Delete a dashboard user."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    success = database.delete_dashboard_user(user_id)
    return {"success": success, "message": "User deleted successfully" if success else "User not found"}


@app.get("/peak-period", response_class=HTMLResponse)
async def peak_period_page(request: Request):
    """Module 01: Product Peak Period Analysis."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "index.html")


@app.get("/menus/customer-affinity", response_class=HTMLResponse)
async def customer_affinity_page(request: Request):
    """Module 02: Customer for against Product (Customer Affinity)."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "customer_affinity.html")


@app.get("/api/customer-affinity/overview")
def get_affinity_overview_endpoint():
    """Get high-level summary KPIs for Customer Affinity."""
    return database.get_affinity_overview()


@app.get("/api/customer-affinity/customers")
def get_affinity_customers_endpoint(limit: int = 500):
    """Get ranked list of customers with affinity metrics."""
    return database.get_customer_affinity_list(limit=limit)


@app.get("/api/customer-affinity/bootstrap")
def get_affinity_bootstrap(limit: int = 1000):
    """Single-request bootstrap: overview + customers + dropdown in one round trip."""
    overview = database.get_affinity_overview()
    customers = database.get_customer_affinity_list(limit=limit)
    dropdown = [{"id": c["customer_id"], "name": f'{c["customer_name"]} ({c["customer_id"]})'} for c in customers[:200]]
    return {"overview": overview, "customers": customers, "dropdown": dropdown}


@app.get("/api/customer-affinity/customer/{customer_id}")
def get_customer_affinity_detail(customer_id: str):
    """Get product basket breakdown and category affinity for a specific customer."""
    return database.get_customer_product_breakdown(customer_id)


@app.get("/api/customer-affinity/product/{product_id}")
def get_product_affinity_detail(product_id: str):
    """Get reverse affinity: all customers who purchased a specific product."""
    return database.get_product_customer_affinity(product_id)


@app.get("/api/customer-affinity/dropdown")
def get_customers_dropdown_endpoint():
    """Get customer list for explorer dropdown."""
    return database.get_customers_dropdown()


def _affinity_filters(start_date: str = "", end_date: str = "", customer_id: str = "",
                      product_id: str = "", category_id: str = ""):
    return {
        "start_date": start_date, "end_date": end_date,
        "customer_id": customer_id, "product_id": product_id, "category_id": category_id,
    }


@app.get("/api/customer-affinity/report")
def get_filtered_affinity_report_endpoint(request: Request, start_date: str = "", end_date: str = "",
                                          customer_id: str = "", product_id: str = "", category_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        return database.get_filtered_affinity_report(
            _affinity_filters(start_date, end_date, customer_id, product_id, category_id)
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})


@app.get("/api/customer-affinity/report.xlsx")
def export_filtered_affinity_report(request: Request, start_date: str = "", end_date: str = "",
                                    customer_id: str = "", product_id: str = "", category_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        report = database.get_filtered_affinity_report(
            _affinity_filters(start_date, end_date, customer_id, product_id, category_id)
        )
        from openpyxl import Workbook
        book = Workbook()
        customer_sheet = book.active
        customer_sheet.title = "Customer view"
        _append_xlsx_row(customer_sheet, ["Customer ID", "Customer", "Quantity", "Purchase frequency", "Total spending", "Last purchase"])
        for row in report["customer_view"]:
            _append_xlsx_row(customer_sheet, [
                row.get("customer_id"), row.get("customer_name"), row.get("quantity"),
                row.get("purchase_frequency"), row.get("spending"), row.get("last_purchase_date"),
            ])
        product_sheet = book.create_sheet("Product view")
        _append_xlsx_row(product_sheet, ["Product ID", "Product", "Category", "Quantity", "Customers", "Purchase frequency", "Spending", "Last purchase"])
        for row in report["product_view"]:
            _append_xlsx_row(product_sheet, [
                row.get("product_id"), row.get("product_name"), row.get("category_name"),
                row.get("quantity"), row.get("customers"), row.get("purchase_frequency"),
                row.get("spending"), row.get("last_purchase_date"),
            ])
        history_sheet = book.create_sheet("Purchase history")
        _append_xlsx_row(history_sheet, ["Period", "Quantity", "Spending", "Repeat customers"])
        for row in report["monthly"]:
            _append_xlsx_row(history_sheet, [row["period"], row["quantity"], row["spending"], row["repeat_purchases"]])
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=customer-affinity-report.xlsx"},
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except ImportError:
        return JSONResponse(status_code=503, content={"success": False, "message": "Excel export dependency is not installed."})


@app.get("/menus/ink", response_class=HTMLResponse)
@app.get("/menus/ink-dependency", response_class=HTMLResponse)
async def ink_dependency_page(request: Request):
    """Module 03: Printer to ink dependent and ink to printer dependant."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    return templates.TemplateResponse(request, "ink_dependency.html", {"request": request, "user": user})


@app.get("/api/ink-dependency/bootstrap")
def get_ink_dependency_bootstrap(request: Request):
    """Bootstrap: Overview + Printers + Inks + Refill Alerts + User Info in one round trip."""
    user = _get_request_user(request)
    user_info = {
        "id": user.get("id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "is_admin": str(user.get("role", "")).strip().lower() == "admin",
    } if user else None

    return {
        "overview": database.get_ink_dependency_overview(),
        "printers": database.get_printer_models_list(),
        "inks": database.get_ink_products_list(),
        "refill_alerts": database.get_refill_due_alerts(),
        "current_user": user_info,
    }


@app.get("/api/ink-dependency/overview")
def get_ink_overview_endpoint():
    return database.get_ink_dependency_overview()


@app.get("/api/ink-dependency/printers")
def get_ink_printers_endpoint():
    return database.get_printer_models_list()


@app.get("/api/ink-dependency/printer/{printer_id}")
def get_printer_ink_matrix_endpoint(printer_id: str):
    return database.get_printer_ink_matrix(printer_id)


@app.get("/api/ink-dependency/inks")
def get_inks_endpoint():
    return database.get_ink_products_list()


@app.get("/api/ink-dependency/ink/{ink_id}")
def get_ink_printer_matrix_endpoint(ink_id: str):
    return database.get_ink_printer_matrix(ink_id)


@app.get("/api/ink-dependency/refill-alerts")
def get_refill_alerts_endpoint():
    return database.get_refill_due_alerts()


@app.get("/api/ink-dependency/relations")
def list_ink_relations_endpoint(
    request: Request,
    printer_id: str = "",
    ink_id: str = "",
    status: str = "active",
):
    """List confirmed printer <-> ink dependencies."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    relations = database.list_printer_ink_relations(
        printer_id=printer_id or None,
        ink_id=ink_id or None,
        status=status or None,
    )
    return {"success": True, "count": len(relations), "relations": relations}


@app.post("/api/ink-dependency/relations")
async def add_ink_relation_endpoint(request: Request):
    """Admin-only: Add a verified dependency relation between a printer and an ink/toner."""
    user, error_resp = _require_admin(request)
    if error_resp:
        return error_resp

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid JSON body."})

    printer_id = str(payload.get("printer_id", "")).strip()
    ink_id = str(payload.get("ink_id", "")).strip()
    source = str(payload.get("source", "Admin confirmed")).strip() or "Admin confirmed"
    notes = str(payload.get("notes", "")).strip()

    try:
        result = database.add_printer_ink_relation(
            printer_id=printer_id,
            ink_id=ink_id,
            source=source,
            notes=notes,
            username=user.get("username", "admin"),
        )
        return JSONResponse(
            status_code=201,
            content={"success": True, "message": "Relationship added successfully.", "data": result},
        )
    except ValueError as e:
        msg = str(e)
        status_code = 409 if "duplicate" in msg.lower() else 422
        return JSONResponse(status_code=status_code, content={"success": False, "message": msg})
    except Exception as e:
        logger.exception("Failed to add printer-ink relation")
        return JSONResponse(status_code=500, content={"success": False, "message": f"Server error: {e}"})


@app.put("/api/ink-dependency/relations/{relation_id}")
async def update_ink_relation_endpoint(relation_id: int, request: Request):
    """Admin-only: Update dependency relation status, source, or notes."""
    user, error_resp = _require_admin(request)
    if error_resp:
        return error_resp

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid JSON body."})

    status = payload.get("status")
    source = payload.get("source")
    notes = payload.get("notes")

    try:
        updated = database.update_printer_ink_relation(
            relation_id=relation_id,
            status=status,
            source=source,
            notes=notes,
            username=user.get("username", "admin"),
        )
        if not updated:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        return {"success": True, "message": "Relationship updated successfully."}
    except ValueError as e:
        return JSONResponse(status_code=422, content={"success": False, "message": str(e)})
    except Exception as e:
        logger.exception("Failed to update printer-ink relation")
        return JSONResponse(status_code=500, content={"success": False, "message": f"Server error: {e}"})


@app.delete("/api/ink-dependency/relations/{relation_id}")
async def deactivate_ink_relation_endpoint(relation_id: int, request: Request, permanent: bool = False):
    """Admin-only: Deactivate or permanently delete a dependency relation."""
    user, error_resp = _require_admin(request)
    if error_resp:
        return error_resp

    try:
        if permanent:
            ok = database.delete_printer_ink_relation(relation_id)
        else:
            ok = database.deactivate_printer_ink_relation(relation_id, username=user.get("username", "admin"))
        if not ok:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        action_desc = "deleted permanently" if permanent else "deactivated"
        return {"success": True, "message": f"Relationship {action_desc} successfully."}
    except Exception as e:
        logger.exception("Failed to deactivate printer-ink relation")
        return JSONResponse(status_code=500, content={"success": False, "message": f"Server error: {e}"})


# ==============================================================================
# NEW PRODUCTS FROM WEBSITES (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_new_products(user: dict | None) -> bool:
    """Check permission to access New Products using product_research_service contract."""
    if not user:
        return False
    if product_research_service and hasattr(product_research_service, "can_view_new_products"):
        try:
            return bool(product_research_service.can_view_new_products(user))
        except Exception:
            logger.exception("Error checking can_view_new_products in service")
    return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/new-products", response_class=HTMLResponse)
@app.get("/menus/new-products", response_class=HTMLResponse)
async def new_products_page(request: Request):
    """Render New Products from Websites page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_new_products(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to New Products required."},
        )

    template_file = BASE_DIR / "templates" / "new_products.html"
    if not template_file.exists():
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: new_products.html template is not yet available.",
                "pending_tasks": [
                    "Create templates/new_products.html",
                    "Design interactive tabs for Local, Market, and Comparison views",
                    "Add research progress indicator widget",
                ],
            },
        )
    return templates.TemplateResponse(request, "new_products.html", {"request": request, "user": user})


@app.get("/api/new-products/report")
def get_new_products_report_endpoint(
    request: Request,
    view: str = "local",
    start_date: str = "",
    end_date: str = "",
    category_id: str = "",
    q: str = "",
    brand: str = "",
    market_category: str = "",
):
    """
    Retrieve new products report.
    Accepts:
    - view: 'local' | 'market' | 'comparison'
    - start_date, end_date (defaults to 30 days up to latest sales date if omitted)
    - category_id, q (local catalog)
    - brand, market_category (persisted market evidence)
    """
    user = _get_request_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    if not _can_view_new_products(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to New Products required."},
        )

    clean_view = (view or "local").strip().lower()
    if clean_view not in ("local", "market", "comparison"):
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": f"Invalid view '{view}'. Allowed values: local, market, comparison."},
        )

    s_date = start_date.strip() if start_date else None
    e_date = end_date.strip() if end_date else None
    parsed_start = None
    parsed_end = None

    if s_date:
        try:
            parsed_start = date.fromisoformat(s_date)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": f"Invalid start_date '{start_date}'. Must be in YYYY-MM-DD format."},
            )

    if e_date:
        try:
            parsed_end = date.fromisoformat(e_date)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": f"Invalid end_date '{end_date}'. Must be in YYYY-MM-DD format."},
            )

    if parsed_start and parsed_end and parsed_start > parsed_end:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid date range: start_date cannot be after end_date."},
        )

    if not product_research_service or not hasattr(product_research_service, "get_product_report"):
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: product_research_service.get_product_report is not available.",
                "pending_tasks": [
                    "Implement product_research_service.get_product_report",
                    "Connect market comparison aggregation engine",
                ],
            },
        )

    try:
        report = product_research_service.get_product_report({
            "view": clean_view,
            "start_date": s_date,
            "end_date": e_date,
            "category_id": category_id.strip() if category_id else None,
            "q": q.strip() if q else None,
            "brand": brand.strip() if brand else None,
            "market_category": market_category.strip() if market_category else None,
        })
        return JSONResponse(status_code=200, content=report)
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except NotImplementedError as nie:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": f"503 Feature not ready: {nie}",
                "pending_tasks": ["Complete product_research_service.get_product_report implementation"],
            },
        )
    except Exception as exc:
        logger.exception("Unexpected error in /api/new-products/report")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while generating the report."},
        )


@app.get("/api/new-products/categories")
def get_new_products_categories_endpoint(request: Request):
    """Return local category IDs and names for the New Products category filter."""
    user = _get_request_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    if not _can_view_new_products(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to New Products required."},
        )
    if not product_research_service or not hasattr(product_research_service, "get_categories"):
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "503 Feature not ready: category lookup is not available."},
        )
    try:
        return JSONResponse(
            status_code=200,
            content={"success": True, "categories": product_research_service.get_categories()},
        )
    except Exception:
        logger.exception("Unexpected error in /api/new-products/categories")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load local categories."},
        )


@app.post("/api/new-products/research")
async def start_new_products_research_endpoint(request: Request):
    """Admin-only: Trigger an asynchronous product research job in the background."""
    user = _get_request_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    if str(user.get("role", "")).strip().lower() != "admin":
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Admin privileges required to start product research."},
        )

    if not product_research_service or not hasattr(product_research_service, "start_product_research"):
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: product_research_service.start_product_research is not available.",
                "pending_tasks": [
                    "Implement product_research_service.start_product_research",
                    "Set up asynchronous background research worker",
                ],
            },
        )

    try:
        user_id = user.get("id") or 1
        job_id = product_research_service.start_product_research(user_id)
        return JSONResponse(
            status_code=202,
            content={
                "success": True,
                "job_id": job_id,
                "status": "pending",
                "message": "Product research job queued in the background.",
            },
        )
    except product_research_service.DuplicateResearchJobError as duplicate:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "job_id": duplicate.job_id,
                "message": "A market research job is already running. Check its progress instead of starting a duplicate.",
            },
        )
    except NotImplementedError as nie:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": f"503 Feature not ready: {nie}",
                "pending_tasks": ["Implement background research engine"],
            },
        )
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except Exception as exc:
        logger.exception("Unexpected error in /api/new-products/research")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while queuing the research job."},
        )


@app.get("/api/new-products/research/{job_id}")
def get_new_products_research_status_endpoint(job_id: str, request: Request):
    """Retrieve progress or results of a background research job."""
    user = _get_request_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    if not _can_view_new_products(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to New Products required."},
        )

    clean_job_id = (job_id or "").strip()
    if not clean_job_id:
        return JSONResponse(status_code=422, content={"success": False, "message": "Job ID cannot be empty."})

    if not product_research_service or not hasattr(product_research_service, "get_product_research_status"):
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: product_research_service.get_product_research_status is not available.",
                "pending_tasks": ["Implement product_research_service.get_product_research_status"],
            },
        )

    try:
        status_info = product_research_service.get_product_research_status(clean_job_id)
        if status_info is None:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Research job '{clean_job_id}' not found."},
            )
        return JSONResponse(status_code=200, content={"success": True, "job_id": clean_job_id, "data": status_info})
    except NotImplementedError as nie:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": f"503 Feature not ready: {nie}",
                "pending_tasks": ["Complete product_research_service.get_product_research_status"],
            },
        )
    except Exception as exc:
        logger.exception(f"Unexpected error fetching status for job {clean_job_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving job status."},
        )


# ==============================================================================
# REPORT 05: DORMANT CLIENTS (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_dormant_clients(user: dict | None) -> bool:
    """Check permission to access Dormant Clients module."""
    if not user:
        return False
    try:
        if hasattr(database, "can_view_dormant_clients"):
            return bool(database.can_view_dormant_clients(user))
    except Exception:
        logger.exception("Error checking can_view_dormant_clients")
    return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/dormant-clients", response_class=HTMLResponse)
@app.get("/menus/dormant-clients", response_class=HTMLResponse)
async def dormant_clients_page(request: Request):
    """Render Dormant Clients analytics page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_dormant_clients(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Dormant Clients required."},
        )

    template_file = BASE_DIR / "templates" / "dormant_clients.html"
    if not template_file.exists():
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: dormant_clients.html template is not yet available.",
                "pending_tasks": [
                    "Create templates/dormant_clients.html",
                    "Design KPI summary cards (Dormant, Insufficient data, Stale date alert)",
                    "Implement customer cadence table with history view and phone call action",
                ],
            },
        )
    return templates.TemplateResponse(request, "dormant_clients.html", {"request": request, "user": user})


@app.get("/api/dormant-clients")
async def get_dormant_clients_report_endpoint(
    request: Request,
    q: Optional[str] = Query(None, description="Search query by customer name, phone, or ID"),
    status: str = Query("all", description="Status filter: all, dormant, insufficient_data, active"),
    sort_by: str = Query("days_overdue", description="Sort by: days_overdue, days_since_last, customer_name, last_purchase_date, avg_interval"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """API returning dormant clients report, summary KPIs, and paginated records."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_dormant_clients(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    clean_status = status.strip().lower()
    valid_statuses = {"all", "dormant", "insufficient_data", "active"}
    if clean_status not in valid_statuses:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": f"Invalid status '{status}'. Must be one of {sorted(valid_statuses)}"},
        )

    clean_sort_by = sort_by.strip().lower()
    valid_sort_fields = {"days_overdue", "days_since_last", "customer_name", "last_purchase_date", "avg_interval"}
    if clean_sort_by not in valid_sort_fields:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": f"Invalid sort_by '{sort_by}'. Must be one of {sorted(valid_sort_fields)}"},
        )

    clean_sort_order = sort_order.strip().lower()
    if clean_sort_order not in {"asc", "desc"}:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid sort_order. Must be 'asc' or 'desc'."},
        )

    try:
        report = database.get_dormant_clients_report(
            search=q.strip() if q else None,
            status_filter=clean_status,
            sort_by=clean_sort_by,
            sort_order=clean_sort_order,
            page=page,
            page_size=page_size,
        )
        return report
    except Exception as exc:
        logger.exception("Unexpected error in /api/dormant-clients")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while generating the dormant clients report."},
        )


@app.get("/api/dormant-clients/{customer_id}/history")
async def get_dormant_client_history_endpoint(request: Request, customer_id: str):
    """API returning detailed purchase history and cadence calculation for a specific customer."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_dormant_clients(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    clean_cid = str(customer_id).strip()
    if not clean_cid:
        return JSONResponse(status_code=422, content={"success": False, "message": "Customer ID cannot be empty."})

    try:
        history = database.get_customer_purchase_history(clean_cid)
        if not history:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Customer '{clean_cid}' not found or has no record."},
            )
        return history
    except Exception as exc:
        logger.exception(f"Unexpected error in /api/dormant-clients/{clean_cid}/history")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving customer purchase history."},
        )


# ==============================================================================
# REPORT 06: CUSTOMER BUSINESS KYC (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_customer_kyc(user: dict | None) -> bool:
    """Check permission to access Customer KYC module."""
    if not user:
        return False
    try:
        if hasattr(database, "can_view_customer_kyc"):
            return bool(database.can_view_customer_kyc(user))
    except Exception:
        logger.exception("Error checking can_view_customer_kyc")
    return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/menus/customer-kyc", response_class=HTMLResponse)
@app.get("/customer-kyc", response_class=HTMLResponse)
async def customer_kyc_page(request: Request):
    """Render Customer Business KYC page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_customer_kyc(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Customer KYC required."},
        )

    template_file = BASE_DIR / "templates" / "customer_kyc.html"
    if not template_file.exists():
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: customer_kyc.html template is not yet available.",
                "pending_tasks": [
                    "Create templates/customer_kyc.html",
                    "Build customer directory with business type filter",
                    "Add customer profile drawer/modal and admin business info editor",
                ],
            },
        )
    return templates.TemplateResponse(request, "customer_kyc.html", {"request": request, "user": user})


@app.get("/api/customer-kyc/customers")
async def get_customer_kyc_customers_endpoint(
    request: Request,
    q: Optional[str] = Query(None, description="Search by name, customer ID, or phone"),
    business_type: Optional[str] = Query(None, description="Filter by business type"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """API endpoint to search, filter, and paginate the customer KYC directory."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_kyc(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    try:
        report = database.get_customer_kyc_directory(
            search=q.strip() if q else None,
            business_type=business_type.strip() if business_type else None,
            page=page,
            page_size=page_size,
        )
        return report
    except Exception as exc:
        logger.exception("Unexpected error in /api/customer-kyc/customers")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while fetching customer KYC directory."},
        )


@app.get("/api/customer-kyc/customers/{customer_id}")
async def get_customer_kyc_detail_endpoint(request: Request, customer_id: str):
    """API endpoint returning customer details, business profile, and purchase summary."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_kyc(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    clean_cid = str(customer_id).strip()
    if not clean_cid:
        return JSONResponse(status_code=422, content={"success": False, "message": "Customer ID cannot be empty."})

    try:
        detail = database.get_customer_kyc_detail(clean_cid)
        if not detail:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Customer '{clean_cid}' was not found."},
            )
        return detail
    except Exception as exc:
        logger.exception(f"Unexpected error in /api/customer-kyc/customers/{clean_cid}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving customer profile."},
        )


@app.put("/api/customer-kyc/customers/{customer_id}")
async def update_customer_kyc_profile_endpoint(request: Request, customer_id: str):
    """Admin-only API endpoint to create or update the KYC business profile for a customer."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user, err_resp = _require_admin(request)
    if err_resp:
        return err_resp

    clean_cid = str(customer_id).strip()
    if not clean_cid:
        return JSONResponse(status_code=422, content={"success": False, "message": "Customer ID cannot be empty."})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid JSON payload."})

    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "Request body must be a JSON object."})

    b_name = str(body.get("business_name") or "").strip()
    if len(b_name) > 200:
        return JSONResponse(status_code=422, content={"success": False, "message": "Business name exceeds 200 characters limit."})

    b_type = str(body.get("business_type") or "").strip()
    if len(b_type) > 100:
        return JSONResponse(status_code=422, content={"success": False, "message": "Business type exceeds 100 characters limit."})

    c_person = str(body.get("contact_person") or "").strip()
    if len(c_person) > 100:
        return JSONResponse(status_code=422, content={"success": False, "message": "Contact person exceeds 100 characters limit."})

    try:
        result = database.save_customer_kyc_profile(clean_cid, body, user)
        status_code = result.get("status_code", 200)
        return JSONResponse(status_code=status_code, content=result)
    except Exception as exc:
        logger.exception(f"Unexpected error updating KYC profile for customer {clean_cid}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while updating the customer business profile."},
        )


# ==============================================================================
# REPORT 08: CUSTOMER WARRANTY (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_customer_warranty(user: dict | None) -> bool:
    """Check permission to access Customer Warranty module."""
    if not user:
        return False
    try:
        if hasattr(database, "can_view_customer_warranty"):
            return bool(database.can_view_customer_warranty(user))
    except Exception:
        logger.exception("Error checking can_view_customer_warranty")
    return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/menus/customer-warranty", response_class=HTMLResponse)
@app.get("/customer-warranty", response_class=HTMLResponse)
async def customer_warranty_page(request: Request):
    """Render Customer Warranty page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_customer_warranty(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Customer Warranty required."},
        )

    template_file = BASE_DIR / "templates" / "customer_warranty.html"
    if not template_file.exists():
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "503 Feature not ready: customer_warranty.html template is not yet available.",
                "pending_tasks": [
                    "Create templates/customer_warranty.html",
                    "Build customer warranty summary cards and table",
                    "Add warranty modal and admin editor",
                ],
            },
        )
    return templates.TemplateResponse(request, "customer_warranty.html", {"request": request, "user": user})


@app.get("/api/customer-warranty")
async def get_customer_warranty_list_endpoint(
    request: Request,
    search: Optional[str] = Query(None, description="Search by customer, phone, product, invoice, or serial"),
    q: Optional[str] = Query(None, description="Alias for search query"),
    status: Optional[str] = Query(None, description="Filter by status (active, expiring_soon, expired, not_started, unknown, all)"),
    date_from: Optional[str] = Query(None, description="Filter warranty end date from (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter warranty end date to (YYYY-MM-DD)"),
    sort_by: Optional[str] = Query("warranty_end_date", description="Sort field"),
    sort_order: Optional[str] = Query("asc", description="Sort order (asc/desc)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
    reference_date: Optional[str] = Query(None, description="Optional reference date (YYYY-MM-DD)"),
):
    """API endpoint to get warranty summary cards and filtered, paginated warranty records."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_warranty(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    search_query = (search or q or "").strip() or None
    parsed_ref_date = None
    if reference_date:
        try:
            from datetime import datetime as dt_cls
            parsed_ref_date = dt_cls.strptime(reference_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(status_code=422, content={"success": False, "message": "Invalid reference_date format. Expected YYYY-MM-DD."})

    try:
        report = database.get_warranty_records(
            search=search_query,
            status_filter=status.strip() if status else None,
            date_from=date_from.strip() if date_from else None,
            date_to=date_to.strip() if date_to else None,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
            reference_date=parsed_ref_date,
        )
        return report
    except Exception as exc:
        logger.exception("Unexpected error in /api/customer-warranty")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while fetching warranty records."},
        )


@app.get("/api/customer-warranty/{record_id}")
async def get_customer_warranty_detail_endpoint(
    request: Request,
    record_id: str,
    reference_date: Optional[str] = Query(None, description="Optional reference date (YYYY-MM-DD)"),
):
    """API endpoint returning complete details of a single warranty record."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_warranty(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied."})

    try:
        rid = int(record_id)
    except (ValueError, TypeError):
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid record ID. Must be an integer."})

    parsed_ref_date = None
    if reference_date:
        try:
            from datetime import datetime as dt_cls
            parsed_ref_date = dt_cls.strptime(reference_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(status_code=422, content={"success": False, "message": "Invalid reference_date format. Expected YYYY-MM-DD."})

    try:
        detail = database.get_warranty_record_detail(rid, reference_date=parsed_ref_date)
        if not detail:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Warranty record #{rid} was not found."},
            )
        return {"success": True, "data": detail}
    except Exception as exc:
        logger.exception(f"Unexpected error in /api/customer-warranty/{record_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving warranty record."},
        )


@app.post("/api/customer-warranty")
async def create_customer_warranty_endpoint(request: Request):
    """Admin-only API endpoint to create a new warranty record."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user, err_resp = _require_admin(request)
    if err_resp:
        return err_resp

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid JSON payload."})

    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "Request body must be a JSON object."})

    if not str(body.get("customer_id") or "").strip():
        return JSONResponse(status_code=422, content={"success": False, "message": "Customer ID is required."})
    if not str(body.get("sales_id") or "").strip():
        return JSONResponse(status_code=422, content={"success": False, "message": "Sales / Invoice ID is required."})
    if not str(body.get("product_id") or "").strip():
        return JSONResponse(status_code=422, content={"success": False, "message": "Product ID is required."})

    try:
        result = database.create_warranty_record(body, user)
        status_code = result.get("status_code", 201)
        return JSONResponse(status_code=status_code, content=result)
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except PermissionError as pe:
        return JSONResponse(status_code=403, content={"success": False, "message": str(pe)})
    except Exception as exc:
        logger.exception("Unexpected error in POST /api/customer-warranty")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while creating warranty record."},
        )


@app.put("/api/customer-warranty/{record_id}")
async def update_customer_warranty_endpoint(request: Request, record_id: str):
    """Admin-only API endpoint to update an existing warranty record."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user, err_resp = _require_admin(request)
    if err_resp:
        return err_resp

    try:
        rid = int(record_id)
    except (ValueError, TypeError):
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid record ID. Must be an integer."})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid JSON payload."})

    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "Request body must be a JSON object."})

    try:
        result = database.update_warranty_record(rid, body, user)
        status_code = result.get("status_code", 200)
        return JSONResponse(status_code=status_code, content=result)
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except PermissionError as pe:
        return JSONResponse(status_code=403, content={"success": False, "message": str(pe)})
    except Exception as exc:
        logger.exception(f"Unexpected error in PUT /api/customer-warranty/{record_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while updating warranty record."},
        )



# ==============================================================================
# REPORT 09: CUSTOMER GIFTS (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_customer_gifts(user: dict | None) -> bool:
    """Check permission to access Customer Gifts (session + Customer Gifts menu permission)."""
    if not user:
        return False
    try:
        return bool(database.can_view_customer_gifts(user))
    except Exception:
        logger.exception("Error checking can_view_customer_gifts")
        return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/menus/customer-gifts", response_class=HTMLResponse)
async def customer_gifts_page(request: Request):
    """Render the Customer Gifts page (Report 09)."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_customer_gifts(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Customer Gifts required."},
        )
    return templates.TemplateResponse(request, "customer_gifts.html", {"request": request, "user": user})


@app.get("/api/customer-gifts/customers")
async def get_customer_gifts_suggestions_endpoint(
    request: Request,
    search: Optional[str] = Query(None, description="Search by customer name, ID or phone"),
    group: str = Query("all", description="Filter group: all, new, repeat"),
    first_purchase_from: Optional[str] = Query(None, description="Filter first purchase date from (YYYY-MM-DD)"),
    first_purchase_to: Optional[str] = Query(None, description="Filter first purchase date to (YYYY-MM-DD)"),
    min_total_amount: Optional[str] = Query(None, description="Minimum total purchase amount"),
    min_order_count: Optional[int] = Query(None, description="Minimum number of orders"),
    new_customer_days: int = Query(90, ge=1, description="New customer window in days"),
    sort_by: str = Query("total_purchase_amount", description="Sort field"),
    sort_order: str = Query("desc", description="Sort order (asc/desc)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """API returning New Customers and Repeat Customers gift suggestions with filters."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_gifts(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Customer Gifts required."})
    try:
        return database.get_customer_gifts_suggestions(
            search=search, group=group,
            first_purchase_from=first_purchase_from, first_purchase_to=first_purchase_to,
            min_total_amount=min_total_amount, min_order_count=min_order_count,
            new_customer_days=new_customer_days, sort_by=sort_by, sort_order=sort_order,
            page=page, page_size=page_size,
        )
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except Exception as exc:
        logger.exception("Unexpected error in /api/customer-gifts/customers")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while generating customer gift suggestions."},
        )


@app.get("/api/customer-gifts/customers/{customer_id}")
def get_customer_gift_details_endpoint(request: Request, customer_id: str):
    """API returning customer details with purchase summary and previous gifts."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_gifts(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Customer Gifts required."})
    cid = str(customer_id).strip()
    if not cid:
        return JSONResponse(status_code=422, content={"success": False, "message": "Customer ID cannot be empty."})
    try:
        detail = database.get_customer_gift_details(cid)
        if not detail:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Customer '{cid}' was not found."},
            )
        return detail
    except Exception as exc:
        logger.exception(f"Unexpected error in /api/customer-gifts/customers/{cid}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving customer gift details."},
        )


@app.get("/api/customer-gifts/gifts")
def list_customer_gifts_endpoint(
    request: Request,
    search: Optional[str] = Query(None, description="Search by customer, gift name, or note"),
    customer_id: Optional[str] = Query(None, description="Filter by customer ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """API returning recorded customer gifts for the Gift History tab."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_gifts(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Customer Gifts required."})
    try:
        return database.get_customer_gifts_list(search=search, customer_id=customer_id, page=page, page_size=page_size)
    except Exception as exc:
        logger.exception("Unexpected error in /api/customer-gifts/gifts")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while fetching recorded gifts."},
        )


@app.post("/api/customer-gifts/gifts")
async def record_customer_gift_endpoint(request: Request):
    """Admin-only API endpoint to record a customer gift (name, date, optional note)."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user, err_resp = _require_admin(request)
    if err_resp:
        return err_resp
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"success": False, "message": "Invalid JSON payload."})
    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "Request body must be a JSON object."})

    cid = str(body.get("customer_id") or "").strip()
    gift_name = str(body.get("gift_name") or "").strip()
    gift_date = body.get("gift_date")
    note = body.get("note")

    try:
        result = database.record_customer_gift(cid, gift_name, gift_date, note, user)
        status_code = result.get("status_code", 201)
        return JSONResponse(status_code=status_code, content=result)
    except ValueError as ve:
        return JSONResponse(status_code=422, content={"success": False, "message": str(ve)})
    except PermissionError as pe:
        return JSONResponse(status_code=403, content={"success": False, "message": str(pe)})
    except Exception as exc:
        logger.exception("Unexpected error in POST /api/customer-gifts/gifts")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while recording the gift."},
        )


# ==============================================================================
# REPORT 09: STOCK-OUT SALES ANALYSIS (ROUTES & CONTROLLER)
# ==============================================================================

def _can_view_stock_out_analysis(user: dict | None) -> bool:
    """Check permission to access Stock-out Analysis (session + menu grant)."""
    if not user:
        return False
    try:
        return bool(database.can_view_stock_out_analysis(user))
    except Exception:
        logger.exception("Error checking can_view_stock_out_analysis")
        return str(user.get("role", "")).strip().lower() == "admin"


def _stock_out_filters(
    start_date: str = "",
    end_date: str = "",
    search: str = "",
    category_id: str = "",
    stock_status: str = "all",
    review_status: str = "all",
    sort_by: str = "restock_priority",
    sort_order: str = "desc",
) -> dict:
    return {
        "start_date": start_date,
        "end_date": end_date,
        "search": search,
        "category_id": category_id,
        "stock_status": stock_status,
        "review_status": review_status,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }


@app.get("/menus/stock-out-analysis", response_class=HTMLResponse)
async def stock_out_analysis_page(request: Request):
    """Render the Stock-out Sales Analysis page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_stock_out_analysis(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Stock-out Analysis required."},
        )
    return templates.TemplateResponse(request, "stock_out_analysis.html", {"request": request, "user": user})


@app.get("/api/stock-out-analysis/report")
def stock_out_analysis_report_endpoint(
    request: Request,
    start_date: str = Query("", description="Selected period start (YYYY-MM-DD)"),
    end_date: str = Query("", description="Selected period end (YYYY-MM-DD)"),
    search: str = Query("", description="Search product name, ID or category"),
    category_id: str = Query("", description="Category ID filter"),
    stock_status: str = Query("all", description="all, in_stock, out_of_stock, negative_stock, unknown"),
    review_status: str = Query("all", description="all, needs_restock, sales_decreased, out_of_stock, history_unavailable"),
    sort_by: str = Query("restock_priority", description="Sort field"),
    sort_order: str = Query("desc", description="Sort order (asc/desc)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """Return summary, filtered product rows, stock observations and pagination."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_stock_out_analysis(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Stock-out Analysis required."})
    try:
        return database.get_stock_out_report(
            _stock_out_filters(
                start_date, end_date, search, category_id,
                stock_status, review_status, sort_by, sort_order,
            ),
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Unexpected error in /api/stock-out-analysis/report")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while generating the stock-out analysis report."},
        )


@app.get("/api/stock-out-analysis/products/{product_id}")
def stock_out_analysis_product_detail_endpoint(
    request: Request,
    product_id: str,
    start_date: str = Query("", description="Selected period start (YYYY-MM-DD)"),
    end_date: str = Query("", description="Selected period end (YYYY-MM-DD)"),
):
    """Return one product's sales trend, current stock and dated observations."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_stock_out_analysis(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Stock-out Analysis required."})
    pid = str(product_id or "").strip()
    if not pid:
        return JSONResponse(status_code=422, content={"success": False, "message": "Product ID is required."})
    try:
        detail = database.get_stock_out_product_detail(
            pid, _stock_out_filters(start_date=start_date, end_date=end_date)
        )
        if not detail:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Product '{pid}' was not found."},
            )
        return detail
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception(f"Unexpected error in /api/stock-out-analysis/products/{pid}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while retrieving stock-out product details."},
        )


@app.get("/api/stock-out-analysis/report.xlsx")
def stock_out_analysis_report_xlsx(
    request: Request,
    start_date: str = Query("", description="Selected period start (YYYY-MM-DD)"),
    end_date: str = Query("", description="Selected period end (YYYY-MM-DD)"),
    search: str = Query("", description="Search product name, ID or category"),
    category_id: str = Query("", description="Category ID filter"),
    stock_status: str = Query("all", description="all, in_stock, out_of_stock, negative_stock, unknown"),
    review_status: str = Query("all", description="all, needs_restock, sales_decreased, out_of_stock, history_unavailable"),
    sort_by: str = Query("restock_priority", description="Sort field"),
    sort_order: str = Query("desc", description="Sort order (asc/desc)"),
):
    """Export the same filtered stock-out rows as the on-screen report."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_stock_out_analysis(user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Permission denied: access to Stock-out Analysis required."})
    try:
        report = database.get_stock_out_report_export(
            _stock_out_filters(
                start_date, end_date, search, category_id,
                stock_status, review_status, sort_by, sort_order,
            )
        )
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "Stock-out Analysis"
        _append_xlsx_row(sheet, [
            "Product ID", "Product", "Category", "Current Stock", "Stock Status",
            "Stock Observed At", "Selected Quantity", "Previous Quantity",
            "Change %", "History Availability", "Review Recommendation",
        ])
        for row in report.get("rows", []):
            change = row.get("change_percent")
            _append_xlsx_row(sheet, [
                row.get("product_id"),
                row.get("product_name"),
                row.get("category_name"),
                row.get("current_stock") if row.get("current_stock") is not None else "Unknown",
                row.get("stock_status_label"),
                row.get("stock_observed_at") or "Unknown",
                row.get("selected_quantity"),
                row.get("previous_quantity"),
                f"{change:.2f}%" if change is not None else "N/A",
                row.get("history_status_label"),
                row.get("review_label"),
            ])
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=stock-out-analysis.xlsx"},
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except ImportError:
        return JSONResponse(status_code=503, content={"success": False, "message": "Excel export dependency is not installed."})
    except Exception:
        logger.exception("Unexpected error in /api/stock-out-analysis/report.xlsx")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred while exporting the stock-out analysis report."},
        )


# ==============================================================================
# 11. EQUIVALENT MODELS
# ==============================================================================

def _can_view_equivalent_models(user: dict | None) -> bool:
    """Check permission to access Equivalent Models (session + menu grant)."""
    if not user:
        return False
    try:
        return bool(database.can_view_equivalent_models(user))
    except Exception:
        logger.exception("Error checking can_view_equivalent_models")
        return str(user.get("role", "")).strip().lower() == "admin"


def _equivalent_actor(request: Request):
    user, error_resp = _require_admin(request)
    if error_resp:
        return None, None, error_resp
    username = str(user.get("username") or "admin")
    user_id = user.get("id")
    ip_address = request.client.host if request.client else ""
    return user, {"username": username, "user_id": user_id, "ip_address": ip_address}, None


@app.get("/equivalent-models", response_class=HTMLResponse)
@app.get("/menus/equivalent-models", response_class=HTMLResponse)
async def equivalent_models_page(request: Request):
    """Render the Equivalent Models report page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_equivalent_models(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Equivalent Models required."},
        )
    return templates.TemplateResponse(request, "equivalent_models.html", {"request": request, "user": user})


@app.get("/api/equivalent-models/bootstrap")
def equivalent_models_bootstrap(request: Request):
    """Return page metadata, filter options and current user permissions."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_equivalent_models(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Equivalent Models required."},
        )
    try:
        database.init_equivalent_models_table()
        return {
            "success": True,
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "role": user.get("role"),
                "is_admin": str(user.get("role", "")).strip().lower() == "admin",
            },
            "metadata": database.get_equivalent_models_metadata(),
            "categories": database.get_equivalent_model_categories(),
        }
    except Exception:
        logger.exception("Could not load Equivalent Models bootstrap")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load Equivalent Models data."},
        )


@app.get("/api/equivalent-models/products")
def equivalent_models_search_products(
    request: Request,
    q: str = Query("", description="Search by product name, ID, description, category or brand"),
    category_id: str = Query("", description="Optional category ID filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """Search catalog products; the catalog has no model column."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_equivalent_models(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Equivalent Models required."},
        )
    try:
        return database.search_equivalent_model_products(
            q, category_id=category_id, page=page, page_size=page_size
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models product search failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not search products."},
        )


@app.get("/api/equivalent-models/alternatives")
def equivalent_models_alternatives(
    request: Request,
    source_product_id: str = Query("", description="Source product ID"),
    verification_status: str = Query("active", description="active, verified, needs_review, inactive or all"),
    category_id: str = Query("", description="Optional category ID filter"),
    stock_status: str = Query("all", description="all, in_stock, out_of_stock or unknown"),
    search: str = Query("", description="Search alternatives"),
    sort_by: str = Query("last_updated", description="last_updated, product_name, price, stock or status"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """List directional alternatives for a source product."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_equivalent_models(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Equivalent Models required."},
        )
    source_id = str(source_product_id or "").strip()
    if not source_id:
        return JSONResponse(status_code=422, content={"success": False, "message": "Source product ID is required."})
    try:
        result = database.list_alternative_relationships(
            source_id,
            verification_status=verification_status,
            category_id=category_id,
            stock_status=stock_status,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
        if result is None:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": f"Source product '{source_id}' was not found."},
            )
        return result
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models alternatives failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load alternatives."},
        )


@app.get("/api/equivalent-models/relationships/{relationship_id}")
def equivalent_models_relationship_detail(relationship_id: int, request: Request):
    """Return one relationship and its database-backed comparison."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_equivalent_models(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Equivalent Models required."},
        )
    try:
        detail = database.get_equivalent_model_relationship_detail(relationship_id)
        if not detail:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        return detail
    except Exception:
        logger.exception("Equivalent Models detail failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load relationship details."},
        )


@app.post("/api/equivalent-models/relationships")
async def equivalent_models_create_relationship(request: Request):
    """Admin-only: record one directional alternative relationship."""
    _user, actor, error_resp = _equivalent_actor(request)
    if error_resp:
        return error_resp
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid JSON body."})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "JSON body must be an object."})
    try:
        result = database.add_equivalent_relationship(
            payload.get("source_product_id", ""),
            payload.get("alternative_product_id", ""),
            payload.get("reason", ""),
            source=payload.get("source", "Admin entry"),
            username=actor["username"],
            user_id=actor["user_id"],
            ip_address=actor["ip_address"],
        )
        return {"success": True, **result, "message": "Relationship recorded for review."}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models create failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not record the relationship."},
        )


@app.put("/api/equivalent-models/relationships/{relationship_id}")
async def equivalent_models_update_relationship(relationship_id: int, request: Request):
    """Admin-only: update the evidence fields of a relationship."""
    _user, actor, error_resp = _equivalent_actor(request)
    if error_resp:
        return error_resp
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid JSON body."})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=422, content={"success": False, "message": "JSON body must be an object."})
    try:
        updated = database.update_equivalent_relationship(
            relationship_id,
            reason=payload.get("reason"),
            source=payload.get("source"),
            username=actor["username"],
            user_id=actor["user_id"],
            ip_address=actor["ip_address"],
        )
        if not updated:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        return {"success": True, "relationship": updated, "message": "Relationship updated."}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models update failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not update the relationship."},
        )


@app.post("/api/equivalent-models/relationships/{relationship_id}/verify")
async def equivalent_models_verify_relationship(relationship_id: int, request: Request):
    """Admin-only: explicitly verify a relationship with evidence."""
    _user, actor, error_resp = _equivalent_actor(request)
    if error_resp:
        return error_resp
    payload = {}
    try:
        raw = await request.body()
        if raw:
            payload = await request.json()
            if not isinstance(payload, dict):
                return JSONResponse(status_code=422, content={"success": False, "message": "JSON body must be an object."})
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid JSON body."})
    try:
        updated = database.verify_equivalent_relationship(
            relationship_id,
            reason=payload.get("reason"),
            source=payload.get("source"),
            username=actor["username"],
            user_id=actor["user_id"],
            ip_address=actor["ip_address"],
        )
        if not updated:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        return {"success": True, "relationship": updated, "message": "Relationship verified."}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models verify failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not verify the relationship."},
        )


@app.post("/api/equivalent-models/relationships/{relationship_id}/deactivate")
def equivalent_models_deactivate_relationship(relationship_id: int, request: Request):
    """Admin-only: deactivate a relationship without deleting its evidence."""
    _user, actor, error_resp = _equivalent_actor(request)
    if error_resp:
        return error_resp
    try:
        updated = database.deactivate_equivalent_relationship(
            relationship_id,
            username=actor["username"],
            user_id=actor["user_id"],
            ip_address=actor["ip_address"],
        )
        if not updated:
            return JSONResponse(status_code=404, content={"success": False, "message": "Relationship not found."})
        return {"success": True, "relationship": updated, "message": "Relationship deactivated."}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Equivalent Models deactivate failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not deactivate the relationship."},
        )


# ==============================================================================
# 12. OLD STOCK & BUYERS
# ==============================================================================


def _can_view_old_stock_buyers(user: dict | None) -> bool:
    """Check permission to access Old Stock & Buyers (session + menu grant)."""
    if not user:
        return False
    try:
        return bool(database.can_view_old_stock_buyers(user))
    except Exception:
        logger.exception("Error checking can_view_old_stock_buyers")
        return str(user.get("role", "")).strip().lower() == "admin"


@app.get("/old-stock-buyers", response_class=HTMLResponse)
@app.get("/menus/old-stock-buyers", response_class=HTMLResponse)
async def old_stock_buyers_page(request: Request):
    """Render the Old Stock & Buyers report page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_old_stock_buyers(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Old Stock & Buyers required."},
        )
    return templates.TemplateResponse(request, "old_stock_buyers.html", {"request": request, "user": user})


@app.get("/api/old-stock-buyers/bootstrap")
def old_stock_buyers_bootstrap(request: Request):
    """Return page metadata, filter options and current user permissions."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_old_stock_buyers(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Old Stock & Buyers required."},
        )
    try:
        return {
            "success": True,
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "role": user.get("role"),
                "is_admin": str(user.get("role", "")).strip().lower() == "admin",
            },
            "metadata": database.get_old_stock_buyers_metadata(),
            "categories": database.get_old_stock_categories(),
        }
    except Exception:
        logger.exception("Could not load Old Stock & Buyers bootstrap")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load Old Stock & Buyers data."},
        )


@app.get("/api/old-stock-buyers/products")
def old_stock_buyers_products(
    request: Request,
    q: str = Query("", description="Search by product name, ID, category or brand"),
    category_id: str = Query("", description="Optional category ID filter"),
    min_age_days: Optional[int] = Query(None, description="Minimum stock age in days"),
    stock_filter: str = Query("all", description="all, known, or unknown"),
    sort_by: str = Query("stock_age", description="stock_age, product_name, stock_quantity or category"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """List old stock products with stock age information."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_old_stock_buyers(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Old Stock & Buyers required."},
        )
    try:
        return database.search_products_with_stock_age(
            q,
            category_id=category_id,
            min_age_days=min_age_days,
            stock_filter=stock_filter,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("Old Stock & Buyers product search failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not search old stock products."},
        )


@app.get("/api/old-stock-buyers/buyers")
def old_stock_buyers_buyers(
    request: Request,
    product_id: str = Query("", description="Product ID to look up buyers for"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Page size"),
):
    """Return buyers for a specific product."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_old_stock_buyers(user):
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Permission denied: access to Old Stock & Buyers required."},
        )
    try:
        return database.get_product_buyers(product_id, page=page, page_size=page_size)
    except Exception:
        logger.exception("Old Stock & Buyers buyer lookup failed")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Could not load buyers."},
        )


# ==============================================================================
# 13. CUSTOMER PAYMENT RISK
# ==============================================================================

_PAYMENT_RISK_DENIED = "Permission denied: access to Customer Payment Risk required."
_PAYMENT_RISK_UNAVAILABLE = "Customer payment data is temporarily unavailable. Please try again."


def _can_view_customer_payment_risk(user: dict | None) -> bool:
    """Check permission to access Customer Payment Risk (session + menu grant)."""
    if not user:
        return False
    try:
        return bool(database.can_view_customer_payment_risk(user))
    except Exception as exc:
        logger.error("Customer Payment Risk permission check failed (%s)", type(exc).__name__)
        return str(user.get("role", "")).strip().lower() == "admin"


def _payment_risk_guard(request: Request):
    """Return (user, None) when allowed, otherwise (None, error response)."""
    if not is_authenticated(request):
        return None, JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_payment_risk(user):
        return None, JSONResponse(status_code=403, content={"success": False, "message": _PAYMENT_RISK_DENIED})
    return user, None


def _safe_report_error(exc: Exception, report: str, action: str, unavailable: str) -> JSONResponse:
    """Map report errors to safe responses. Only the exception type is logged, never its message or data."""
    if isinstance(exc, database.ReportInputError):
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    if isinstance(exc, database.CustomerNotFoundError):
        return JSONResponse(status_code=404, content={"success": False, "message": "Customer not found."})
    logger.error("%s %s failed (%s)", report, action, type(exc).__name__)
    return JSONResponse(status_code=500, content={"success": False, "message": unavailable})


def _payment_risk_error(exc: Exception, action: str) -> JSONResponse:
    return _safe_report_error(exc, "Customer Payment Risk", action, _PAYMENT_RISK_UNAVAILABLE)


@app.get("/customer-payment-risk", response_class=HTMLResponse)
@app.get("/menus/customer-payment-risk", response_class=HTMLResponse)
async def customer_payment_risk_page(request: Request):
    """Render the Customer Payment Risk report page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_customer_payment_risk(user):
        return JSONResponse(status_code=403, content={"success": False, "message": _PAYMENT_RISK_DENIED})
    return templates.TemplateResponse(request, "customer_payment_risk.html", {"request": request, "user": user})


@app.get("/api/customer-payment-risk/bootstrap")
def customer_payment_risk_bootstrap(request: Request):
    """Return labels, filter options and the current user's role."""
    user, denied = _payment_risk_guard(request)
    if denied:
        return denied
    try:
        options = database.get_customer_payment_risk_filter_options()
        return {
            "success": True,
            "user": {
                "username": user.get("username"),
                "role": user.get("role"),
                "is_admin": str(user.get("role", "")).strip().lower() == "admin",
            },
            "metadata": database.get_customer_payment_risk_metadata(),
            "customer_types": options["customer_types"],
        }
    except Exception as exc:
        return _payment_risk_error(exc, "bootstrap")


@app.get("/api/customer-payment-risk/customers")
def customer_payment_risk_customers(
    request: Request,
    q: str = Query("", description="Search by customer name, ID or phone"),
    overdue_status: str = Query("all", description="all, overdue, not_overdue, unknown or no_invoices"),
    customer_type: str = Query("", description="Optional customer type, e.g. DEALER"),
    activity: str = Query("with_invoices", description="with_invoices or all"),
    sort_by: str = Query("overdue_amount", description="overdue_amount, days_overdue, unpaid_total, invoiced_total, customer_name, customer_id, last_payment_date or last_invoice_date"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=100, description="Page size"),
    refresh: bool = Query(False, description="Reload from the database instead of the 60-second cache"),
):
    """Return the filtered customer payment report with summary cards."""
    _, denied = _payment_risk_guard(request)
    if denied:
        return denied
    try:
        return database.get_customer_payment_risk_report(
            refresh=refresh, q=q, overdue_status=overdue_status, customer_type=customer_type,
            activity=activity, sort_by=sort_by, sort_order=sort_order, page=page, page_size=page_size,
        )
    except Exception as exc:
        return _payment_risk_error(exc, "report")


@app.get("/api/customer-payment-risk/customer")
def customer_payment_risk_customer(
    request: Request,
    customer_id: str = Query("", description="Customer ID (CS_ID)"),
    invoice_page: int = Query(1, ge=1),
    payment_page: int = Query(1, ge=1),
    return_page: int = Query(1, ge=1),
):
    """Return one customer's payment summary with invoice, payment and return history."""
    _, denied = _payment_risk_guard(request)
    if denied:
        return denied
    try:
        return database.get_customer_payment_risk_detail(
            customer_id, invoice_page=invoice_page, payment_page=payment_page, return_page=return_page,
        )
    except Exception as exc:
        return _payment_risk_error(exc, "detail")


# ==============================================================================
# 14. CUSTOMER PROFITABILITY
# ==============================================================================

_PROFITABILITY_DENIED = "Permission denied: access to Customer Profitability required."
_PROFITABILITY_UNAVAILABLE = "Profitability data is temporarily unavailable. Please try again."


def _can_view_customer_profitability(user: dict | None) -> bool:
    """Check permission to access Customer Profitability (session + menu grant)."""
    if not user:
        return False
    try:
        return bool(database.can_view_customer_profitability(user))
    except Exception as exc:
        logger.error("Customer Profitability permission check failed (%s)", type(exc).__name__)
        return str(user.get("role", "")).strip().lower() == "admin"


def _profitability_guard(request: Request):
    """Return (user, None) when allowed, otherwise (None, error response)."""
    if not is_authenticated(request):
        return None, JSONResponse(status_code=401, content={"success": False, "message": "Authentication required."})
    user = _get_request_user(request)
    if not _can_view_customer_profitability(user):
        return None, JSONResponse(status_code=403, content={"success": False, "message": _PROFITABILITY_DENIED})
    return user, None


def _profitability_error(exc: Exception, action: str) -> JSONResponse:
    return _safe_report_error(exc, "Customer Profitability", action, _PROFITABILITY_UNAVAILABLE)


@app.get("/customer-profitability", response_class=HTMLResponse)
@app.get("/menus/customer-profitability", response_class=HTMLResponse)
async def customer_profitability_page(request: Request):
    """Render the Customer Profitability report page."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    user = _get_request_user(request)
    if not _can_view_customer_profitability(user):
        return JSONResponse(status_code=403, content={"success": False, "message": _PROFITABILITY_DENIED})
    return templates.TemplateResponse(request, "customer_profitability.html", {"request": request, "user": user})


@app.get("/api/customer-profitability/bootstrap")
def customer_profitability_bootstrap(request: Request):
    """Return labels, default dates and the range of recorded sales."""
    user, denied = _profitability_guard(request)
    if denied:
        return denied
    try:
        return {
            "success": True,
            "user": {"role": user.get("role"), "is_admin": str(user.get("role", "")).strip().lower() == "admin"},
            "metadata": database.get_customer_profitability_metadata(),
            "data_range": database.get_customer_profitability_data_range(),
        }
    except Exception as exc:
        return _profitability_error(exc, "bootstrap")


def _profitability_report(request: Request, view: str, **filters):
    _, denied = _profitability_guard(request)
    if denied:
        return denied
    try:
        return database.get_customer_profitability_report(view=view, **filters)
    except Exception as exc:
        return _profitability_error(exc, f"{view} report")


@app.get("/api/customer-profitability/customers")
def customer_profitability_customers(
    request: Request,
    start_date: str = Query("", description="YYYY-MM-DD; defaults to 90 days before end_date"),
    end_date: str = Query("", description="YYYY-MM-DD; defaults to today"),
    q: str = Query("", description="Search by customer name, ID or phone"),
    customer_id: str = Query("", description="Optional customer ID"),
    profit_status: str = Query("all", description="all, known or unknown"),
    sort_by: str = Query("gross_profit", description="gross_profit, gross_margin, net_sales, sales, returns, cost_coverage, invoice_count, name or id"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    refresh: bool = Query(False, description="Reload from the database instead of the 60-second cache"),
):
    """Gross profit by customer for the selected period."""
    return _profitability_report(request, "customers", start_date=start_date, end_date=end_date, q=q,
                                 customer_id=customer_id, profit_status=profit_status, sort_by=sort_by,
                                 sort_order=sort_order, page=page, page_size=page_size, refresh=refresh)


@app.get("/api/customer-profitability/products")
def customer_profitability_products(
    request: Request,
    start_date: str = Query("", description="YYYY-MM-DD; defaults to 90 days before end_date"),
    end_date: str = Query("", description="YYYY-MM-DD; defaults to today"),
    q: str = Query("", description="Search by product name, ID or category"),
    customer_id: str = Query("", description="Optional customer ID to limit products to one customer"),
    profit_status: str = Query("all", description="all, known or unknown"),
    sort_by: str = Query("gross_profit", description="gross_profit, gross_margin, net_sales, sales, returns, cost_coverage, invoice_count, name or id"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    refresh: bool = Query(False, description="Reload from the database instead of the 60-second cache"),
):
    """Gross profit by product for the selected period, optionally for one customer."""
    return _profitability_report(request, "products", start_date=start_date, end_date=end_date, q=q,
                                 customer_id=customer_id, profit_status=profit_status, sort_by=sort_by,
                                 sort_order=sort_order, page=page, page_size=page_size, refresh=refresh)


@app.get("/api/dashboard/full-overview")
def get_dashboard_full_overview(request: Request):
    """Return all high-level business intelligence data in one single response."""
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    return {
        "kpis": database.get_dashboard_kpis(),
        "monthly_trend": database.get_monthly_sales_trend(months=12),
        "top_products": database.get_top_selling_products(limit=6),
        "recent_invoices": database.get_recent_invoices(limit=8),
        "inventory_summary": database.get_inventory_summary(limit=6),
        "top_customers": database.get_top_customers(limit=6),
        "db_status": "connected" if database.test_connection() else "disconnected",
        "alerts": database.get_smart_alerts(),
    }


def _report_user(request: Request):
    return database.get_session_user(request.cookies.get("session_token"))


def _report_access(request: Request):
    user = _report_user(request)
    if not database.can_view_reports(user):
        return None
    return user


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    if not _report_access(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "reports.html")


@app.get("/api/reports/sales")
def sales_report(request: Request, start_date: str = "", end_date: str = "",
                 product_id: str = "", category_id: str = "", customer_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        return database.get_sales_report({
            "start_date": start_date, "end_date": end_date,
            "product_id": product_id, "category_id": category_id, "customer_id": customer_id,
        })
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})


@app.get("/api/reports/sales.xlsx")
def sales_report_xlsx(request: Request, start_date: str = "", end_date: str = "",
                      product_id: str = "", category_id: str = "", customer_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        report = database.get_sales_report(locals())
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "Sales report"
        _append_xlsx_row(sheet, ["Date", "Product", "Category", "Customer", "Quantity", "Revenue"])
        for row in report["rows"]:
            _append_xlsx_row(sheet, [row.get("sales_date"), row.get("product_name"), row.get("category_name"),
                                     row.get("customer_name"), row.get("quantity"), row.get("revenue")])
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": "attachment; filename=sales-report.xlsx"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except ImportError:
        return JSONResponse(status_code=503, content={"success": False, "message": "Excel export dependency is not installed."})


@app.get("/api/reports/sales.pdf")
def sales_report_pdf(request: Request, start_date: str = "", end_date: str = "",
                     product_id: str = "", category_id: str = "", customer_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        report = database.get_sales_report(locals())
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=A4)
        width, height = A4
        y = height - 45
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(40, y, "Printer Analytics - Sales Report")
        y -= 22
        pdf.setFont("Helvetica", 9)
        pdf.drawString(40, y, "Filters: " + json.dumps(report["filters"], default=str))
        y -= 22
        for row in report["rows"]:
            line = f'{row.get("sales_date")} | {str(row.get("product_name", ""))[:28]} | {row.get("quantity")} | {row.get("revenue")}'
            pdf.drawString(40, y, line)
            y -= 14
            if y < 45:
                pdf.showPage()
                y = height - 45
        pdf.save()
        output.seek(0)
        return StreamingResponse(output, media_type="application/pdf",
                                 headers={"Content-Disposition": "attachment; filename=sales-report.pdf"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except ImportError:
        return JSONResponse(status_code=503, content={"success": False, "message": "PDF export dependency is not installed."})


@app.get("/api/alerts")
def smart_alerts(request: Request):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    return {"alerts": database.get_smart_alerts()}


@app.get("/api/forecast/{product_id}")
def product_forecast(product_id: str, request: Request):
    if not is_authenticated(request):
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    return database.forecast_product_demand(product_id)


@app.get("/api/db-status")
def get_db_status():
    """Verify database health."""
    return {
        "status": "connected" if database.test_connection() else "error",
    }


@app.get("/api/stats")
def get_stats():
    """High level KPIs and overview metrics."""
    return database.get_dashboard_kpis()


# ==============================================================================
# AI BUSINESS ASSISTANT (READ-ONLY)
# ==============================================================================

def _warm_ai_context():
    try:
        build_business_context()
    except Exception:
        logger.exception("AI context warm-up failed")


@app.on_event("startup")
def _startup_warm_ai_context():
    """Pre-build the analytics snapshot so the first AI request is fast."""
    threading.Thread(target=_warm_ai_context, daemon=True).start()
    if product_research_service and hasattr(product_research_service, "initialize_market_research"):
        try:
            product_research_service.initialize_market_research()
        except Exception:
            logger.exception("Could not initialize market research storage")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _chat_question(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Invalid JSON body."})
    if not isinstance(payload, dict):
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Invalid request body."})
    raw_message = payload.get("message")
    if raw_message is None:
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Message is required."})
    if not isinstance(raw_message, str):
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Message must be a string."})
    question = raw_message.strip()
    if not question:
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Message is required."})
    if len(question) > 2000:
        return None, JSONResponse(status_code=422, content={"success": False, "message": "Message is too long."})
    return question, None


@app.post("/api/ai/chat")
async def ai_chat(request: Request):
    """Answer a business question using a bounded read-only analytics snapshot."""
    unauthorized = require_ai_auth(request)
    if unauthorized:
        return unauthorized
    question, error = await _chat_question(request)
    if error:
        return error
    try:
        context = await run_in_threadpool(build_business_context)
    except Exception:
        logger.exception("Could not build AI business context")
        return JSONResponse(status_code=503, content={"success": False, "message": "Could not prepare business data."})
    try:
        answer = await run_in_threadpool(answer_question, question, context)
    except AIServiceError as exc:
        return JSONResponse(status_code=503, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("AI chat request failed")
        return JSONResponse(status_code=500, content={"success": False, "message": "AI request failed."})
    try:
        user = get_session_user(request.cookies.get("session_token"))
        if user:
            await run_in_threadpool(save_ai_chat, user["id"], "chat", question, answer)
    except Exception:
        logger.exception("Could not save AI chat history")
    return {"success": True, "answer": answer, "metadata": context_metadata(context)}


@app.post("/api/ai/insights")
async def ai_insights(request: Request):
    """Generate management insights from current read-only business data."""
    unauthorized = require_ai_auth(request)
    if unauthorized:
        return unauthorized
    try:
        context = await run_in_threadpool(build_business_context)
    except Exception:
        logger.exception("Could not build AI business context")
        return JSONResponse(status_code=503, content={"success": False, "message": "Could not prepare business data."})
    try:
        answer = await run_in_threadpool(generate_insights, context)
    except AIServiceError as exc:
        return JSONResponse(status_code=503, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("AI insights request failed")
        return JSONResponse(status_code=500, content={"success": False, "message": "AI request failed."})
    try:
        user = get_session_user(request.cookies.get("session_token"))
        if user:
            await run_in_threadpool(save_ai_chat, user["id"], "insights", "", answer)
    except Exception:
        logger.exception("Could not save AI chat history")
    return {"success": True, "answer": answer, "metadata": context_metadata(context)}


@app.post("/api/ai/forecast")
async def ai_forecast(request: Request):
    """Generate a directional three-month forecast from historical trends."""
    unauthorized = require_ai_auth(request)
    if unauthorized:
        return unauthorized
    try:
        context = await run_in_threadpool(build_business_context)
    except Exception:
        logger.exception("Could not build AI business context")
        return JSONResponse(status_code=503, content={"success": False, "message": "Could not prepare business data."})
    try:
        answer = await run_in_threadpool(generate_forecast, context)
    except AIServiceError as exc:
        return JSONResponse(status_code=503, content={"success": False, "message": str(exc)})
    except Exception:
        logger.exception("AI forecast request failed")
        return JSONResponse(status_code=500, content={"success": False, "message": "AI request failed."})
    try:
        user = get_session_user(request.cookies.get("session_token"))
        if user:
            await run_in_threadpool(save_ai_chat, user["id"], "forecast", "", answer)
    except Exception:
        logger.exception("Could not save AI chat history")
    return {"success": True, "answer": answer, "metadata": context_metadata(context)}


@app.post("/api/ai/{action}/stream")
async def ai_stream(action: str, request: Request):
    """Stream AI output as server-sent events so text appears token by token."""
    unauthorized = require_ai_auth(request)
    if unauthorized:
        return unauthorized
    if action not in ("chat", "insights", "forecast"):
        return JSONResponse(status_code=404, content={"success": False, "message": "Unknown AI action."})

    question = None
    if action == "chat":
        question, error = await _chat_question(request)
        if error:
            return error

    try:
        context = await run_in_threadpool(build_business_context)
    except Exception:
        logger.exception("Could not build AI business context")
        return JSONResponse(status_code=503, content={"success": False, "message": "Could not prepare business data."})

    if action == "chat":
        chunks = stream_answer_question(question, context)
    elif action == "insights":
        chunks = stream_insights(context)
    else:
        chunks = stream_forecast(context)

    metadata = context_metadata(context)

    def event_stream():
        full_answer = ""
        try:
            yield _sse({"meta": metadata})
            for delta in chunks:
                full_answer += delta
                yield _sse({"delta": delta})
            yield _sse({"done": True})
            user = get_session_user(request.cookies.get("session_token"))
            if user:
                save_ai_chat(
                    user["id"], action,
                    question if action == "chat" else "",
                    full_answer,
                )
        except AIServiceError as exc:
            yield _sse({"error": str(exc)})
        except Exception:
            logger.exception("AI streaming request failed")
            yield _sse({"error": "AI request failed."})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/ai/history")
async def ai_history(request: Request, action: str = None, keyword: str = None,
                     from_date: str = None, to_date: str = None, limit: int = 50):
    """Retrieve AI chat history for the authenticated user with optional filters."""
    unauthorized = require_ai_auth(request)
    if unauthorized:
        return unauthorized
    user = get_session_user(request.cookies.get("session_token"))
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Unauthorized"})
    try:
        rows = await run_in_threadpool(
            get_ai_chat_history, user["id"], action, keyword, from_date, to_date, limit
        )
        return {"success": True, "chats": rows}
    except Exception:
        logger.exception("Could not fetch AI chat history")
        return JSONResponse(status_code=500, content={"success": False, "message": "Could not fetch history."})


# ==============================================================================
# 01. PEAK PERIOD OF PRODUCT SELL ENDPOINTS
# ==============================================================================

@app.get("/api/peak-timing")
def get_peak_timing():
    """Get peak day of week, peak hours, and monthly volume distribution."""
    return database.get_peak_timing_summary()


@app.get("/api/products-peak-ranking")
def get_products_peak_ranking(limit: int = 1500):
    """Get all products ranked by sales with their peak month, peak volume, and surge rate."""
    return database.get_products_peak_ranking(limit=limit)


@app.get("/api/peak/bootstrap")
def get_peak_bootstrap(limit: int = 1500):
    """Single-request bootstrap: timing + ranking + products + categories."""
    timing = database.get_peak_timing_summary()
    ranking = database.get_products_peak_ranking(limit=limit)
    products = database.get_products_dropdown_list()
    categories = database.get_categories_list()
    return {"timing": timing, "ranking": ranking, "products": products, "categories": categories}


def _peak_filters(start_date: str = "", end_date: str = "", product_id: str = "", category_id: str = ""):
    return {
        "start_date": start_date,
        "end_date": end_date,
        "product_id": product_id,
        "category_id": category_id,
    }


@app.get("/api/peak/report")
def get_filtered_peak_report_endpoint(request: Request, start_date: str = "", end_date: str = "",
                                      product_id: str = "", category_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        return database.get_filtered_peak_report(_peak_filters(start_date, end_date, product_id, category_id))
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})


@app.get("/api/peak/report.xlsx")
def export_filtered_peak_report(request: Request, start_date: str = "", end_date: str = "",
                                product_id: str = "", category_id: str = ""):
    if not _report_access(request):
        return JSONResponse(status_code=403, content={"success": False, "message": "Report permission required."})
    try:
        report = database.get_filtered_peak_report(_peak_filters(start_date, end_date, product_id, category_id))
        from openpyxl import Workbook
        book = Workbook()
        monthly = book.active
        monthly.title = "Monthly comparison"
        _append_xlsx_row(monthly, ["Period", "Quantity", "Sales amount"])
        for row in report["monthly"]:
            _append_xlsx_row(monthly, [row["period"], row["quantity"], row["sales"]])
        yearly = book.create_sheet("Yearly comparison")
        _append_xlsx_row(yearly, ["Year", "Quantity", "Sales amount"])
        for row in report["yearly"]:
            _append_xlsx_row(yearly, [row["period"], row["quantity"], row["sales"]])
        trends = book.create_sheet("Product trends")
        _append_xlsx_row(trends, ["Product ID", "Product name", "Period", "Quantity", "Sales amount"])
        for product in report["product_trends"]:
            for row in product["trend"]:
                _append_xlsx_row(trends, [product["product_id"], product["product_name"], row["period"], row["quantity"], row["sales"]])
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=product-peak-report.xlsx"},
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"success": False, "message": str(exc)})
    except ImportError:
        return JSONResponse(status_code=503, content={"success": False, "message": "Excel export dependency is not installed."})


@app.get("/api/product-peak-detail/{product_id}")
def get_product_peak_detail(product_id: str):
    """Get month-by-month seasonality breakdown for a single product."""
    return database.get_product_peak_detail(product_id)


@app.get("/api/products-list")
def get_products_list():
    """Get product list for search and selection dropdown."""
    return database.get_products_dropdown_list()


@app.get("/api/categories")
def get_categories():
    """Get list of all product categories."""
    return database.get_categories_list()


# ==============================================================================
# 02. DYNAMIC SIDEBAR MENUS (DATABASE CONTROLLED)
# ==============================================================================

@app.get("/api/menus")
async def get_menus(request: Request, all: bool = False):
    """Retrieve all sidebar menus in treeview hierarchy filtered by user permissions."""
    if all:
        # When all=true, return every menu (used by permission checkboxes) — no user filter
        return database.get_sidebar_menus(all_menus=True, user_id=None)
    token = request.cookies.get("session_token")
    user = database.get_session_user(token)
    user_id = user["id"] if user else None
    return database.get_sidebar_menus(all_menus=False, user_id=user_id)


@app.get("/api/menus/main-list")
async def get_main_menus():
    """Retrieve list of all Main Menus."""
    return database.get_main_menus_table()


@app.get("/api/menus/sub-list")
async def get_submenus():
    """Retrieve list of all Submenus."""
    return database.get_submenus_table()


@app.post("/api/menus")
async def create_menu(request: Request):
    """Create a new menu (main or sub) in database."""
    payload = await request.json()
    new_id = database.create_sidebar_menu(payload)
    return {"success": True, "id": new_id, "message": "Menu created successfully"}


@app.put("/api/menus/{menu_id}")
async def update_menu(menu_id: int, request: Request):
    """Update an existing sidebar menu in database."""
    payload = await request.json()
    success = database.update_sidebar_menu(menu_id, payload)
    return {"success": success, "message": "Menu updated successfully" if success else "Menu not found"}


@app.delete("/api/menus/{menu_id}")
async def delete_menu(menu_id: int):
    """Delete a sidebar menu from database."""
    success = database.delete_sidebar_menu(menu_id)
    return {"success": success, "message": "Menu deleted successfully" if success else "Menu not found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
