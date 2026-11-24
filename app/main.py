import csv
import io
import json
import math
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, date as _date_cls
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # crm_python/

# Error monitoring: only activates if SENTRY_DSN is set (Settings → get a free
# DSN at sentry.io, add it as a Railway/Render env var). Without it, this is a
# complete no-op — local dev and any deployment that hasn't set it up yet
# behave exactly as before. traces_sample_rate is kept low since this is a
# small-shop app, not a high-traffic service — no need to pay for full tracing.
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=0.1,
        environment=os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER") or "local",
        send_default_pii=False,  # don't auto-attach request bodies/cookies — this app handles customer PII
    )

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File, Query
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError

from .database import get_db, SessionLocal
from .models import (Staff, Product, Customer, Repair, Invoice, InvoiceLine, AuditLog, Setting, HeldCart,
                      CashSession, Supplier, PurchaseOrder, PurchaseOrderLine, RepairPart, SmsMessage, OutboundMessage,
                      TradeIn, Layaway, LayawayPayment, CardBatch, CardStatement, SaleReturn, RepairPhoto)
from .cardfees import (crm_card_month, month_key, parse_month_key, fee_rate)
from .reconcile import (crm_card_rows, crm_net_cents, parse_bank_csv, match_transactions, day_end_checks, summarize_checks)
from .money import (cash_rounding_on, require_card_ref_on, cash_refund_amount, plan_payment, scrub_card_numbers, round_cash)
from .auth import (
    hash_pin, verify_pin, attempt_login, get_current_staff, role_allowed, add_audit,
    start_session, pin_fingerprint,
)
from .phones import find_customer_by_phone, phone_search_clause
from . import inputs as _inputs
from . import returns as _returns_mod
from .report_types import _safe_cell

TRADE_IN_MAX = 10000.0   # one phone is never worth more than this; a bigger number is a typo
from . import security as _sec
from . import backup as _backup
from .tax import calc_canadian_tax, PROVINCE_LABELS
from .repairs_const import STATUS_LABELS, STATUS_ORDER, STATUS_BADGE, ISSUE_TYPES, next_status
from .barcode_gen import generate_barcode_svg
from .icons import icon_svg
from .csrf import csrf_protect, csrf_token_global, RequestContextMiddleware


def get_warranty_status(repair):
    """Warranty starts when the customer actually collects the device
    (not when work finishes), since that's when they start using it.
    Returns None if it hasn't been collected yet — nothing to check
    warranty against until then. Reads the COLLECTED entry from
    status_history rather than repair.updated_at, since updated_at can
    be touched later by unrelated edits (e.g. adjusting the final cost)
    and would silently corrupt the warranty start date."""
    if not repair.status_history:
        return None
    try:
        history = json.loads(repair.status_history)
    except (ValueError, TypeError):
        return None
    collected_entries = [h for h in history if h.get("status") == "COLLECTED" and h.get("date")]
    if not collected_entries:
        return None
    try:
        collected_date = datetime.fromisoformat(collected_entries[0]["date"])
    except (ValueError, KeyError):
        return None
    warranty_days = repair.warranty_days or 0
    expires = collected_date + timedelta(days=warranty_days)
    days_remaining = (expires - datetime.utcnow()).days
    return {
        "collected_date": collected_date, "expires": expires,
        "active": days_remaining >= 0, "days_remaining": max(0, days_remaining),
    }

from .product_const import CATEGORY_LABELS, CAT_SUBCATEGORIES
from . import repair_parts as _repair_parts
from .phone_const import PHONE_BRANDS, PHONE_MODELS, CASE_COLORS, COMMON_CASE_STYLES, LAPTOP_MODELS, CONSOLE_MODELS, COMMON_LAPTOP_GAMING_STYLES
from .notifications import send_email_receipt, send_sms, send_plain_email, send_backup_email
from .encryption import encrypt_value, decrypt_value
from apscheduler.schedulers.background import BackgroundScheduler
from .seed import init_db
from .localtime import (get_shop_tz, local_today, local_now, to_local, to_local_date, day_bounds_utc,
                        month_bounds, parse_month, parse_date, tz_label, zone_name_for, TIMEZONE_CHOICES, L as _local_dt)
from .reporting import daily_totals, cash_card_totals, SalesWindow
from .report_routes import build_router as _build_report_router
from . import messaging as _messaging
from .messaging_routes import build_router as _build_messaging_router
from .security_routes import build_router as _build_security_router
from . import barcodes as _barcodes
from . import imei as _imei
from . import devices as _devices
from . import setup as _setup
from . import intake as _intake
from .scan_routes import build_router as _build_scan_router
from .returns_routes import build_router as _build_returns_router
from .repair_prices_routes import build_router as _build_repair_prices_router
from . import brand as _brand
from .brand_routes import build_router as _build_brand_router
from .about_routes import build_router as _build_about_router
from . import repair_prices as _repair_prices
from .expenses_routes import build_router as _build_expenses_router
from .quote_routes import build_router as _build_quote_router, detail_context as _quote_detail_context
from . import quotes as _quotes
from . import returns as _returns
from . import expenses as _expenses_mod
from .intake_routes import (build_router as _build_intake_router, detail_context as _intake_detail_context,
                            form_context as _intake_form_context, intake_fields as _intake_fields,
                            settings_context as _intake_settings_context)

# The auto-generated API pages (/docs, /redoc, /openapi.json) list every route and field of the shop system, so they are switched off.
app = FastAPI(title="TechPro+ CRM", docs_url=None, redoc_url=None, openapi_url=None)

# Use a persistent secret (set SESSION_SECRET in your environment for
# production) — a fresh random one each start would log every staff
# member out whenever the server restarts.
_SECRET_FILE = os.path.join(os.path.dirname(__file__), "..", ".session_secret")
def _get_session_secret():
    # On Railway: set SESSION_SECRET as an environment variable in the dashboard
    # On local Windows: generated once and saved to .session_secret file
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE) as f:
            return f.read().strip()
    new_secret = secrets.token_hex(32)
    try:
        with open(_SECRET_FILE, "w") as f:
            f.write(new_secret)
    except OSError:
        pass  # read-only filesystem (Railway) — SESSION_SECRET env var must be set
    return new_secret

# Session cookie security: on a shared shop terminal, the default 14-day
# session length is far too long — anyone approaching an unattended browser
# stays logged in for two weeks. 12 hours covers a full shift and forces a
# fresh login the next day. https_only is auto-enabled when a known hosting
# platform's env var is present (Railway/Render both set one), so local
# `uvicorn --reload` testing over plain http:// keeps working unmodified —
# set FORCE_HTTPS_COOKIES=1 explicitly for any other host that needs it.
_IS_HOSTED = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER")
                  or os.environ.get("FORCE_HTTPS_COOKIES"))
app.add_middleware(
    SessionMiddleware,
    secret_key=_get_session_secret(),
    https_only=_IS_HOSTED,
    same_site="lax",
    max_age=12 * 60 * 60,
)
# Must be added after SessionMiddleware so it runs "inside" it (Starlette
# middlewares wrap outside-in in the order added, and this needs the
# session — populated by SessionMiddleware — to already be on the request
# by the time route handlers/templates read from it).
app.add_middleware(RequestContextMiddleware)
# Outermost, so every response — pages, static files, error pages — carries the security headers.
app.add_middleware(_setup.SetupGateMiddleware)       # a brand-new shop sees only the setup screen until it is claimed (v51)
app.add_middleware(_sec.SecurityHeadersMiddleware, hosted=_IS_HOSTED)
_sec.install_pan_guard()

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["csrf_token"] = csrf_token_global
templates.env.globals["icon"] = icon_svg
templates.env.globals["L"] = _local_dt  # stored UTC -> shop-local time, for templates


# v53: the dashboard feed used to print raw audit codes ("INVOICE_CREATE").
# Each code maps to (icon, tone, plain-English phrase). Unknown/future codes
# fall back to the code itself in lower case, so nothing is ever hidden.
_ACTIVITY = {
    "INVOICE_CREATE": ("receipt", "money", "rang up a sale"),
    "REFUND": ("undo-2", "bad", "refunded a sale"),
    "RETURN": ("undo-2", "warn", "processed a return"),
    "EXCHANGE": ("refresh-cw", "warn", "processed an exchange"),
    "STORE_CREDIT": ("wallet", "money", "changed store credit"),
    "LOYALTY": ("tag", "", "used loyalty points"),
    "PRICE_ADD": ("tag", "", "added a repair price"),
    "PRICE_CHANGE": ("tag", "warn", "changed a repair price"),
    "PRICE_DELETE": ("tag", "warn", "removed a repair price"),
    "PRICE_IMPORT": ("tag", "", "imported repair prices"),
    "BRAND_CHANGE": ("image", "", "changed the customer pages"),
    "HOLD_CART": ("shopping-cart", "", "held a cart"),
    "RECALL_CART": ("shopping-cart", "", "recalled a held cart"),
    "REPAIR_CREATE": ("wrench", "", "opened a repair ticket"),
    "REPAIR_STATUS": ("wrench", "", "moved a repair along"),
    "REPAIR_EDIT": ("wrench", "", "edited a repair"),
    "REPAIR_INTAKE": ("clipboard-list", "", "recorded a check-in"),
    "REPAIR_PHOTO": ("camera", "", "added a repair photo"),
    "REPAIR_NOTIFY": ("smartphone", "", "sent a pickup text"),
    "REPAIR_PART_ADD": ("nut", "", "used a part on a repair"),
    "REPAIR_PART_REMOVE": ("nut", "warn", "took a part off a repair"),
    "QUOTE_SENT": ("send", "", "sent a repair quote"),
    "QUOTE_RESENT": ("send", "", "re-sent a repair quote"),
    "QUOTE_CANCELLED": ("circle-x", "warn", "cancelled a repair quote"),
    "CUSTOMER_ADD": ("user", "", "added a customer"),
    "CUSTOMER_EDIT": ("user", "", "edited a customer"),
    "SMS_SENT": ("message-circle", "", "sent a text"),
    "SMS_RECEIPT": ("message-circle", "", "texted a receipt"),
    "SMS_OPT_OUT": ("bell", "warn", "marked a customer do-not-text"),
    "SMS_OPT_IN": ("bell", "", "allowed texts again"),
    "SMS_TEST": ("message-circle", "", "sent a test text"),
    "SMS_RETRY": ("message-circle", "", "retried a text"),
    "SMS_CANCEL": ("message-circle", "warn", "cancelled a queued text"),
    "SMS_CATCHUP": ("message-circle", "", "caught up queued texts"),
    "EMAIL_RECEIPT": ("mail", "", "emailed a receipt"),
    "PRODUCT_ADD": ("package", "", "added a product"),
    "PRODUCT_EDIT": ("package", "", "edited a product"),
    "PRODUCT_DELETE": ("package", "bad", "deleted a product"),
    "PRODUCT_IMPORT": ("upload", "", "imported products"),
    "PRODUCT_BULK_ADD": ("package", "", "added products in bulk"),
    "STOCK_COUNT": ("scan-line", "", "finished a stock count"),
    "PO_CREATE": ("truck", "", "started a purchase order"),
    "PO_SEND": ("truck", "", "sent a purchase order"),
    "PO_RECEIVE": ("truck", "money", "received a delivery"),
    "PO_CANCEL": ("truck", "warn", "cancelled a purchase order"),
    "SUPPLIER_ADD": ("truck", "", "added a supplier"),
    "SUPPLIER_EDIT": ("truck", "", "edited a supplier"),
    "SUPPLIER_TOGGLE": ("truck", "", "switched a supplier on/off"),
    "LAYAWAY_CREATE": ("package-check", "", "started a layaway"),
    "LAYAWAY_PAYMENT": ("package-check", "money", "took a layaway payment"),
    "LAYAWAY_EDIT": ("package-check", "", "changed a layaway"),
    "LAYAWAY_COMPLETE": ("package-check", "money", "completed a layaway"),
    "TRADE_IN": ("smartphone", "", "took in a trade-in"),
    "TRADE_IN_EDIT": ("smartphone", "", "updated a trade-in"),
    "PHONE_MODEL_ADD": ("smartphone", "", "added a phone model"),
    "PHONE_MODEL_REMOVE": ("smartphone", "", "removed a phone model"),
    "CASH_UP": ("banknote", "money", "counted the till"),
    "EXPENSE": ("wallet", "", "logged an expense"),
    "EXPENSE_VOID": ("wallet", "warn", "voided an expense"),
    "CARD_BATCH": ("credit-card", "", "recorded a card batch"),
    "CARD_FEES": ("credit-card", "", "updated card fees"),
    "BACKUP_EXPORT": ("save", "", "exported a backup"),
    "BACKUP_IMPORT": ("save", "warn", "restored a backup"),
    "BACKUP_FAILED": ("save", "bad", "had a backup fail"),
    "EXPORT": ("save", "", "exported data"),
    "LOGIN": ("key", "", "signed in"),
    "LOGOUT": ("key", "", "signed out"),
    "PIN_CHANGE": ("lock", "", "changed a PIN"),
    "SECURITY": ("shield", "", "changed security settings"),
    "SECURITY_BLOCK": ("shield", "bad", "was blocked by security"),
    "SETTINGS": ("settings", "", "changed settings"),
    "STAFF_ADD": ("users", "", "added a staff member"),
    "STAFF_EDIT": ("users", "", "edited a staff member"),
    "STAFF_TOGGLE": ("users", "", "switched a staff member on/off"),
}


def activity_label(action: str):
    ico, tone, phrase = _ACTIVITY.get(action or "", ("scroll-text", "", (action or "did something").replace("_", " ").lower()))
    return {"icon": ico, "tone": tone, "phrase": phrase}


templates.env.globals["activity_label"] = activity_label


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort catch for anything that isn't already handled — a bad
    query, a None slipping through, anything. Without this, FastAPI's
    default is a bare, unstyled "Internal Server Error" with no branding,
    no way back, and (depending on deployment) sometimes a raw traceback —
    not something a customer-facing till should ever show.

    Deliberately defensive: this must never itself throw, or the fallback
    for a crash becomes a worse crash. Every step below is wrapped so a
    failure in logging, in reading the session, or in rendering the error
    page still ends in *some* response rather than an unhandled 500.
    """
    import logging
    logging.getLogger("uvicorn.error").exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    if _SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass  # error reporting failing must never block the response

    try:
        return templates.TemplateResponse(request, "error_500.html", {}, status_code=500)
    except Exception:
        # The template engine itself is having a bad day — fall back to
        # the simplest possible response rather than let this propagate.
        return HTMLResponse(
            "<h1>Something went wrong</h1><p>Please <a href='/'>go back to the dashboard</a>.</p>",
            status_code=500,
        )

# WINDOWS FIX: %-d (remove leading zero from day) is Linux-only and
# crashes on Windows with a ValueError. This custom Jinja filter does
# the same thing cross-platform by using %d and stripping manually.
def _datefmt(dt, fmt: str) -> str:
    """Cross-platform date formatting. Use {d} instead of %-d in format
    strings passed to this filter — e.g. '%B {d}, %Y'."""
    if isinstance(dt, datetime):
        dt = _local_dt(dt)  # stored UTC -> shop-local wall clock
    result = dt.strftime(fmt.replace("{d}", "%d"))
    result = result.replace(" 0", " ").replace("/0", "/")
    return result

templates.env.filters["datefmt"] = _datefmt

init_db()

_tz_boot_db = SessionLocal()
try:
    get_shop_tz(_tz_boot_db)
finally:
    _tz_boot_db.close()


@app.get("/healthz")
def healthz():
    """Health check for Railway/Render — deliberately does NOT touch the
    database or require login, so it stays fast and doesn't false-negative
    if the DB is briefly reconnecting (e.g. Neon waking from idle)."""
    return {"status": "ok"}



# ── helpers ──────────────────────────────────────────────────────────
def get_setting(db: Session, key: str, default=""):
    s = db.get(Setting, key)
    return s.value if s else default


def set_setting(db: Session, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _messaging_hook(fn, db: Session, repair) -> None:
    """Run a messaging hook so that a problem in the text queue can NEVER
    block the workshop: the status change is already saved by the time this
    runs, so on any error we only discard the queued texts."""
    try:
        fn(db, repair)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        import logging
        logging.getLogger("techpro.messaging").exception("messaging hook failed for ticket %s: %s", getattr(repair, "ticket_no", "?"), e)


def _log_outgoing_sms(db: Session, phone: str, body: str, staff_name: str, customer_id: str = None):
    """Logs a successfully-sent SMS to the conversation thread. Only
    called after send_sms() reports success, so this represents messages
    the customer actually received, not failed attempts."""
    if not customer_id and phone:
        match = find_customer_by_phone(db, phone)
        customer_id = match.id if match else None
    db.add(SmsMessage(customer_id=customer_id, phone=phone, body=body, direction="out", staff_name=staff_name))
    db.commit()


def get_sms_webhook_secret(db: Session) -> str:
    """A random token embedded in the webhook URL Twilio calls when a
    customer replies. Twilio can't log in with a staff PIN, so this is
    the access control for that endpoint instead — generated once and
    reused. NOTE: this is a lighter-weight protection than Twilio's own
    request-signature validation (HMAC using your Auth Token); it stops
    randomly-guessed URLs but not a determined attacker who's somehow
    seen this exact URL. Full X-Twilio-Signature validation would be a
    further hardening step if that matters for your setup."""
    secret = get_setting(db, "sms_webhook_secret", "")
    if not secret:
        secret = secrets.token_urlsafe(24)
        set_setting(db, "sms_webhook_secret", secret)
    return secret


def cart_get(request: Request):
    return request.session.setdefault("cart", [])


def cart_totals(request: Request, db: Session):
    cart = cart_get(request)
    cart_sub = round(sum(i["price"] * i["qty"] for i in cart), 2)

    override = request.session.get("sub_override")
    sub = override if override is not None else cart_sub

    disc_mode = request.session.get("disc_mode", "$")
    disc_raw = request.session.get("disc_value", 0) or 0
    manual_disc = sub * (min(disc_raw, 100) / 100) if disc_mode == "%" else min(disc_raw, sub)

    loyalty_discount = request.session.get("loyalty_discount", 0) or 0
    store_credit_asked = request.session.get("store_credit_used", 0) or 0

    # Store credit is a way of PAYING, not a price cut, so GST/PST is worked out on the price before it (v52.2). Before, the credit
    # came off first and the shop collected tax on less than the customer actually bought. Only the credit that is needed is
    # used: asking for more than the sale comes to no longer burns the extra off the customer's balance.
    price_cut = round(manual_disc + loyalty_discount, 2)
    taxable = max(0, sub - price_cut)
    province = get_setting(db, "province", "ON")
    tax = calc_canadian_tax(taxable, province)
    store_credit_used = round(min(max(0.0, float(store_credit_asked)), tax["total"]), 2)
    disc = round(price_cut + store_credit_used, 2)          # the invoice's "discount" still includes the credit, so subtotal - discount + tax = total
    return {
        "cart": cart, "cart_sub": cart_sub, "sub": round(sub, 2),
        "disc": disc, "disc_mode": disc_mode, "disc_raw": disc_raw,
        "loyalty_discount": loyalty_discount, "store_credit_used": store_credit_used,
        "tax": tax, "total": round(tax["total"] - store_credit_used, 2),
    }


def reset_cart_overrides(request: Request):
    """Reset things that should not survive a cart-contents change —
    mirrors the original's behavior of dropping the manual subtotal
    override whenever items are added/removed/changed."""
    request.session["sub_override"] = None


def reset_customer_redemptions(request: Request):
    """Loyalty/store-credit redemptions are tied to a specific customer —
    reset them whenever the customer attached to the sale changes."""
    request.session["loyalty_discount"] = 0
    request.session["store_credit_used"] = 0


def pos_cart_context(request: Request, db: Session) -> dict:
    """Context for the POS cart/payment panel (templates/partials/pos_cart.html)
    — shared by the full /pos page render and every cart-mutating endpoint's
    AJAX response, so the panel is always built the same way no matter which
    route touched it. Deliberately leaner than the full /pos context: it
    skips the product catalog and category/brand menus, which never change
    from a cart action and would just be wasted queries on every keystroke.
    """
    customers = db.query(Customer).order_by(Customer.name).all()
    totals = cart_totals(request, db)
    customer_id = request.session.get("customer_id")
    selected_customer = db.get(Customer, customer_id) if customer_id else None
    held_carts = db.query(HeldCart).order_by(HeldCart.created_at.desc()).all()
    points_redeem_rate = float(get_setting(db, "points_redeem_rate", "100"))
    return {
        "customers": customers, "customer_id": customer_id,
        "selected_customer": selected_customer, "held_carts": held_carts,
        "points_redeem_rate": points_redeem_rate,
        "scan_error": request.session.pop("scan_error", None) or request.session.pop("checkout_error", None),
        "cash_rounding_on": cash_rounding_on(db), "require_card_ref": require_card_ref_on(db),
        "cash_due": round_cash(totals["total"]) if cash_rounding_on(db) else totals["total"],
        **totals,
    }


def pos_response(request: Request, db: Session):
    """The tail call for every cart-mutating POS route. If the till's own
    fetch() JS made this request (X-Pos-Ajax header), return just the
    cart-panel HTML fragment so it can be swapped in without a page reload.
    Otherwise — no JS, a stale tab, curl, a bookmark — fall back to the
    original full-page redirect, so nothing about this behavior depends on
    JavaScript being present."""
    if request.headers.get("X-Pos-Ajax") == "1":
        return templates.TemplateResponse(request, "partials/pos_cart.html", pos_cart_context(request, db))
    return RedirectResponse("/pos", status_code=303)


def resolve_tender_split(payment_method: str, total: float, cash_part: float, card_part: float) -> tuple:
    """Decomposes a checkout's payment_method into how much of it actually
    landed as physical cash in the drawer vs. ran through the card
    terminal — the two things Cash Up has to reconcile against something
    real (a physical count, a terminal batch report). UPI, E-Transfer, and
    Store Credit settle themselves outside both of those, so they
    contribute to neither.

    A split payment is allowed to add up to more than the total (the
    checkout route only rejects it if it's short) — e.g. $5 cash + $20
    card against a $16.94 sale, expecting $8.06 change. The card side is
    exact (a terminal can't hand back partial change), so any overpayment
    comes back out of the *cash* side. The net cash this transaction
    actually leaves in the drawer is what tendered minus what went back
    out as change — which can go negative for a single transaction (this
    one nets to -$3.06: the drawer needs $3.06 more than it took in just
    to make this sale's change), even though the day's total obviously
    can't.
    """
    if payment_method == "Cash":
        return round(total, 2), 0.0
    if payment_method.startswith("Split ("):
        change = max(0.0, round(cash_part + card_part - total, 2))
        net_cash = round(cash_part - change, 2)
        return net_cash, round(card_part, 2)
    if payment_method == "Card":
        return 0.0, round(total, 2)
    return 0.0, 0.0  # UPI, E-Transfer, Store Credit — nothing to reconcile physically


def payment_method_label(payment_method: str) -> str:
    """Collapses every distinct 'Split (Cash $x + Card $y)' string down to
    one label for grouping/display — each split sale otherwise carries its
    own exact dollar amounts baked into the string, so a raw group-by would
    produce one row per split sale instead of a single 'Split' bucket."""
    if payment_method.startswith("Split ("):
        return "Split (Cash + Card)"
    return payment_method


def next_invoice_number(db: Session):
    prefix = get_setting(db, "invoice_prefix", "INV")
    counter = int(get_setting(db, "invoice_counter", "1000"))
    set_setting(db, "invoice_counter", str(counter + 1))
    return f"{prefix}-{counter}"


def get_custom_phone_models(db: Session) -> dict:
    """Staff-added phone models, layered on top of the built-in
    PHONE_MODELS reference list in phone_const.py — a new phone (one
    that hasn't even shipped yet, like a next-gen iPhone or Galaxy S
    series announced ahead of launch) can be added once, permanently,
    right from the Bulk-Create Variants page — no code change or
    redeploy needed for something this routine. Stored as JSON under a
    single Setting key rather than a new table, since it's a small,
    simple, append-mostly structure that doesn't need one."""
    raw = get_setting(db, "custom_phone_models", "{}")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def merged_phone_models(db: Session) -> tuple:
    """PHONE_MODELS/PHONE_BRANDS with any staff-added models layered on
    top for display. "Other" (the free-text fallback, always empty)
    is kept last, matching its built-in position, regardless of where a
    brand new custom brand would otherwise land by insertion order."""
    custom = get_custom_phone_models(db)
    merged = {brand: list(models) for brand, models in PHONE_MODELS.items()}
    for brand, models in custom.items():
        merged.setdefault(brand, [])
        for m in models:
            if m not in merged[brand]:
                merged[brand].append(m)
    brands = [b for b in merged if b != "Other"]
    if "Other" in merged:
        brands.append("Other")
    return merged, brands


def next_layaway_number(db: Session):
    prefix = get_setting(db, "layaway_prefix", "LAY")
    counter = int(get_setting(db, "layaway_counter", "1000"))
    set_setting(db, "layaway_counter", str(counter + 1))
    return f"{prefix}-{counter}"


def next_trade_in_number(db: Session):
    counter = int(get_setting(db, "trade_in_counter", "1000"))
    set_setting(db, "trade_in_counter", str(counter + 1))
    return f"TR-{counter}"


def require_login(request: Request, db: Session):
    staff = get_current_staff(request, db)
    return staff


app.include_router(_build_report_router(templates, get_setting, require_login, get_db))
app.include_router(_build_messaging_router(templates, get_setting, require_login, get_db))
app.include_router(_build_security_router(templates, get_setting, set_setting, require_login, get_db))
app.include_router(_build_intake_router(templates, require_login, get_db, get_setting, set_setting, lambda db: get_shop_info(db)))
app.include_router(_build_scan_router(templates, require_login, get_db, CATEGORY_LABELS))
app.include_router(_build_expenses_router(templates, require_login, get_db))
app.include_router(_build_returns_router(templates, require_login, get_db, lambda db: get_shop_info(db), get_setting))
app.include_router(_build_repair_prices_router(templates, require_login, get_db, lambda db: get_custom_phone_models(db)))
app.include_router(_build_brand_router(templates, require_login, get_db))
app.include_router(_build_about_router(templates, require_login, get_db))
app.include_router(_build_quote_router(templates, require_login, get_db, get_setting, lambda *a, **k: _log_outgoing_sms(*a, **k), lambda request: _public_origin(request)))


# ── SMS WEBHOOK (incoming replies from Twilio) ──────────────────────────
# Deliberately outside any require_login check — Twilio calls this
# directly, not a logged-in staff member. Protected by the secret token
# in the URL path instead (see get_sms_webhook_secret's docstring for
# what this does and doesn't protect against).
@app.post("/webhooks/sms-reply/{secret}")
async def sms_reply_webhook(request: Request, secret: str, db: Session = Depends(get_db)):
    expected = get_sms_webhook_secret(db)
    if secret != expected:
        return Response(status_code=403)

    form = await request.form()
    from_phone = form.get("From", "")
    body = form.get("Body", "")
    if from_phone and body:
        _messaging.handle_inbound(db, from_phone, body)

    # Twilio expects a TwiML (XML) response acknowledging receipt. An
    # empty <Response> means "received, no auto-reply sent" — exactly
    # what we want, since replies get read by staff, not auto-answered.
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                     media_type="application/xml")


# ── PUBLIC REPAIR STATUS LOOKUP ─────────────────────────────────────
# Deliberately unauthenticated — meant to be shared with a customer
# (e.g. "check your repair status at [shop]/status") so they don't have
# to call in. Two ways in:
#   1. /status   — the exact ticket number AND the last 4 digits of the phone number on file.
#   2. /s/<code> — a private link staff copy from the repair screen (v48). The code is random and
#                  unguessable, so the customer needs no phone digits.
# Even on a match only status-relevant fields (device, status, promised-by date) are shown — never cost,
# customer name, or any other repair. Wrong/missing ticket and wrong phone digits return the exact same
# generic error, so this can't be used to enumerate which ticket numbers exist, and a device that keeps
# guessing is paused for a few minutes (v48).
def _phone_last4(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if len(digits) >= 4 else digits


def _public_status_result(repair, db=None, token: str = "") -> dict:
    """What a customer may see. Drop-off photos are only ever offered on the private /s/<code> link, only when the
    owner switched them on, and never on the ticket-number + last-4 lookup."""
    photos = []
    if db is not None and token and _brand.get_brand(db)["show_photos"]:
        photos = _brand.customer_photos(db, repair.id)
    return {
        "ticket_no": repair.ticket_no,
        "device": repair.device,
        "status": STATUS_LABELS.get(repair.status, repair.status),
        "status_badge": STATUS_BADGE.get(repair.status, "badge-gray"),
        "promised_by": repair.promised_by,
        "ready": repair.status in ("READY", "COMPLETED"),
        "progress": _brand.progress(repair.status),
        "photos": photos, "token": token if photos else "",
    }


def _public_status_page(request: Request, db: Session, result=None, error=None, code: int = 200):
    brand = _brand.get_brand(db)
    return templates.TemplateResponse(request, "public_status.html", {
        "result": result, "error": error, "shop_name": brand["shop_name"], "brand": brand,
    }, status_code=code)


_STATUS_NOT_FOUND = ("We couldn't find a matching ticket. Double-check your ticket number and the last 4 digits "
                     "of the phone number on file.")
_STATUS_TOO_MANY = "Too many tries from this device. Please wait a few minutes and try again."


@app.get("/status", response_class=HTMLResponse)
def public_repair_status(request: Request, db: Session = Depends(get_db)):
    return _public_status_page(request, db)


@app.post("/status", response_class=HTMLResponse)
def public_repair_status_lookup(request: Request, ticket_no: str = Form(...), phone_last4: str = Form(...),
                                 db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if _sec.status_lookup_blocked(db, request):
        return _public_status_page(request, db, error=_STATUS_TOO_MANY, code=429)

    try:
        ticket_int = int(re.sub(r"\D", "", ticket_no or "")[:12])
    except ValueError:
        ticket_int = None

    digits = re.sub(r"\D", "", phone_last4 or "")
    repair = db.query(Repair).filter(Repair.ticket_no == ticket_int).first() if ticket_int else None

    if repair and repair.customer and len(digits) == 4 and _phone_last4(repair.customer.phone) == digits:
        return _public_status_page(request, db, result=_public_status_result(repair, db))
    _sec.log_attempt(db, request, "status", False, note="status lookup: no match")
    return _public_status_page(request, db, error=_STATUS_NOT_FOUND)


@app.get("/s/{token}", response_class=HTMLResponse)
def public_repair_status_link(request: Request, token: str, db: Session = Depends(get_db)):
    if _sec.status_lookup_blocked(db, request):
        return _public_status_page(request, db, error=_STATUS_TOO_MANY, code=429)
    repair = db.query(Repair).filter(Repair.status_token == token).first() if 8 <= len(token) <= 64 else None
    if repair:
        return _public_status_page(request, db, result=_public_status_result(repair, db, token))
    _sec.log_attempt(db, request, "status", False, note="status link: unknown code")
    return _public_status_page(request, db, error="This link isn't valid any more. Please ask the shop for a new one.",
                               code=404)


def _public_photo(request: Request, db: Session, token: str, photo_id: str, want_thumb: bool):
    """A drop-off photo for the customer who holds the private link. Everything must line up: the code belongs to a
    repair, the owner has the photos switch on, the picture belongs to that repair and is one a customer may see."""
    no = Response("Not found.", status_code=404, headers={"Cache-Control": "no-store"})
    if _sec.status_lookup_blocked(db, request):
        return Response("Too many tries.", status_code=429, headers={"Cache-Control": "no-store"})
    repair = db.query(Repair).filter(Repair.status_token == token).first() if 8 <= len(token) <= 64 else None
    if not repair or not _brand.get_brand(db)["show_photos"]:
        _sec.log_attempt(db, request, "status", False, note="status photo: not allowed")
        return no
    photo = db.query(RepairPhoto).filter(RepairPhoto.id == photo_id, RepairPhoto.repair_id == repair.id,
                                         RepairPhoto.label.in_(_brand.CUSTOMER_PHOTO_LABELS)).first()
    if not photo:
        return no
    body, mime = photo.data, photo.mime
    if want_thumb and photo.thumb:
        info = _intake.sniff_image(bytes(photo.thumb))
        if info:
            body, mime = photo.thumb, info[0]
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        return no
    return Response(bytes(body), media_type=mime, headers={
        "Cache-Control": "private, no-store", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.get("/s/{token}/photo/{photo_id}")
def public_status_photo(request: Request, token: str, photo_id: str, db: Session = Depends(get_db)):
    return _public_photo(request, db, token, photo_id, False)


@app.get("/s/{token}/photo/{photo_id}/thumb")
def public_status_photo_thumb(request: Request, token: str, photo_id: str, db: Session = Depends(get_db)):
    return _public_photo(request, db, token, photo_id, True)


# ── LOGIN ────────────────────────────────────────────────────────────
_fmt_wait = _sec.format_wait


_PENDING_2FA_MINUTES = 5
_PENDING_2FA_MAX_TRIES = 5


def _clean_pin_input(pin: str) -> str:
    return re.sub(r"\s+", "", pin or "")


# ── FIRST-RUN SETUP (v51) ────────────────────────────────────────────
def _setup_min_pin(db: Session) -> int:
    """The owner's PIN protects everything, so a new shop starts at 6 digits even if the general minimum is lower."""
    return max(6, _sec.min_pin_length(db))


def _setup_form_page(request: Request, db: Session, error: str = "", form: dict | None = None, status: int = 200):
    return templates.TemplateResponse(request, "setup.html", {
        "error": error, "form": form or {}, "provinces": PROVINCE_LABELS, "min_pin": _setup_min_pin(db),
        "env_code": bool(_setup._env_code()),
    }, status_code=status)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if not _setup.refresh(db):                       # finished already (or this is an existing shop)
        return RedirectResponse("/login", status_code=303)
    return _setup_form_page(request, db)


@app.post("/setup")
def setup_submit(request: Request, setup_code: str = Form(""), shop_name: str = Form(""), province: str = Form(""),
                 shop_phone: str = Form(""), shop_address: str = Form(""), shop_gst: str = Form(""), shop_pst: str = Form(""),
                 owner_name: str = Form(""), pin: str = Form(""), pin_confirm: str = Form(""), remove_samples: str = Form(""),
                 db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not _setup.refresh(db):
        return RedirectResponse("/login", status_code=303)
    form = {"shop_name": shop_name.strip(), "province": province, "shop_phone": shop_phone.strip(), "shop_address": shop_address.strip(),
            "shop_gst": shop_gst.strip(), "shop_pst": shop_pst.strip(), "owner_name": owner_name.strip(),
            "remove_samples": remove_samples == "1"}

    def fail(message: str):
        return _setup_form_page(request, db, message, form, 400)

    if not _setup.code_ok(db, setup_code):
        return fail("That setup code is not right. It is in the server's log (look for “SETUP CODE”), or it is the SETUP_CODE variable if you set one.")
    if not (1 <= len(form["shop_name"]) <= 60):
        return fail("Enter the shop's name (up to 60 letters).")
    if province not in PROVINCE_LABELS:
        return fail("Choose the province the shop is in. It decides which sales taxes are charged.")
    if not (1 <= len(form["owner_name"]) <= 40):
        return fail("Enter the owner's name (up to 40 letters).")
    if len(form["shop_phone"]) > 30 or len(form["shop_address"]) > 120 or len(form["shop_gst"]) > 30 or len(form["shop_pst"]) > 30:
        return fail("One of the shop details is too long.")
    pin, pin_confirm = _clean_pin_input(pin), _clean_pin_input(pin_confirm)
    need = _setup_min_pin(db)
    if not re.fullmatch(r"[0-9]+", pin or "") or not (need <= len(pin) <= 8):
        return fail(f"The PIN must be {need} to 8 digits, numbers only.")
    if pin != pin_confirm:
        return fail("The two PINs don't match.")
    if _sec.is_weak_pin(pin):
        return fail("That PIN is too easy to guess (a repeat like 1111 or a run like 1234). Pick something random.")

    owner = _setup.complete(db, owner_name=form["owner_name"], pin=pin, shop_name=form["shop_name"], province=province,
                            phone=form["shop_phone"], address=form["shop_address"], gst=form["shop_gst"], pst=form["shop_pst"],
                            remove_samples=form["remove_samples"])
    start_session(request, db, owner)
    return RedirectResponse("/help?welcome=1", status_code=303)


@app.get("/scanner-test", response_class=HTMLResponse)
def scanner_test_page(request: Request, db: Session = Depends(get_db)):
    """v52: a page to open on the phone itself — checks the camera and the barcode reader and lets the owner scan a
    real barcode. (A server cannot prove that a camera works; only the phone can.) Any signed-in staff may use it."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "scanner_test.html", {"staff": staff, "shop_name": get_setting(db, "shop_name", "TechPro+")})


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request, welcome: str = Query(""), db: Session = Depends(get_db)):
    """v51: a short how-to for the staff. Every role may read it; the owner/manager sections are marked as such."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "help.html", {
        "staff": staff, "welcome": welcome == "1", "flash": request.session.pop("flash", None),
        "is_manager": staff.role in ("owner", "manager"), "shop_name": get_setting(db, "shop_name", "your shop"),
    })


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    wait = _sec.login_block_seconds(db, request)
    active = db.query(Staff).filter(Staff.active == True).all()  # noqa: E712
    return templates.TemplateResponse(request, "login.html", {
        "locked": wait > 0, "lock_seconds": wait,
        "error": request.session.pop("login_error", None),
        "pin_len": _sec.pin_length_for_keypad(active),
    })


@app.post("/login")
def login_submit(request: Request, pin: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    wait = _sec.login_block_seconds(db, request)
    if wait:
        request.session["login_error"] = f"Too many wrong tries. Try again in {_fmt_wait(wait)}."
        return RedirectResponse("/login", status_code=303)

    pin = _clean_pin_input(pin)
    staff = attempt_login(db, pin) if _sec.looks_like_pin(pin) else None
    if not staff:
        _sec.log_attempt(db, request, "pin", False, note="wrong PIN")
        wait = _sec.ip_lock_remaining(db, _sec.client_ip(request))
        if wait:
            add_audit(db, None, "SECURITY_BLOCK",
                      f"Sign-in paused for {wait}s on {_sec.device_label(request.headers.get('user-agent', ''))} "
                      f"({_sec.client_ip(request)}) after repeated wrong PINs")
        request.session["login_error"] = "Incorrect PIN."
        return RedirectResponse("/login", status_code=303)

    if staff.totp_enabled:
        # Right PIN, but this account also needs its authenticator code. The PIN step is NOT counted as a
        # success yet (otherwise guessing codes could reset the wrong-try counter forever).
        _sec.log_attempt(db, request, "pin_ok", True, staff=staff, note="PIN correct — waiting for authenticator code")
        request.session.clear()
        request.session["pending_2fa"] = staff.id
        request.session["pending_at"] = datetime.utcnow().isoformat()
        request.session["pending_fails"] = 0
        return RedirectResponse("/login/2fa", status_code=303)

    _sec.log_attempt(db, request, "pin", True, staff=staff, note="signed in")
    start_session(request, db, staff)
    if _sec.is_weak_pin(pin):          # known only now, while the PIN is in hand; the stored hash can't tell us
        request.session["weak_pin"] = True
    add_audit(db, staff, "LOGIN", f"Staff login: {staff.name}")
    return RedirectResponse("/", status_code=303)


def _pending_2fa_staff(request: Request, db: Session):
    """The staff member who has entered a correct PIN and is waiting to give the authenticator code."""
    staff_id = request.session.get("pending_2fa")
    if not staff_id:
        return None
    try:
        started = datetime.fromisoformat(request.session.get("pending_at") or "")
    except ValueError:
        return None
    if datetime.utcnow() - started > timedelta(minutes=_PENDING_2FA_MINUTES):
        return None
    staff = db.get(Staff, staff_id)
    return staff if (staff and staff.active and staff.totp_enabled) else None


@app.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, db: Session = Depends(get_db)):
    staff = _pending_2fa_staff(request, db)
    if not staff:
        request.session.clear()
        request.session["login_error"] = "That sign-in timed out. Enter your PIN again."
        return RedirectResponse("/login", status_code=303)
    wait = _sec.login_block_seconds(db, request)
    return templates.TemplateResponse(request, "login_2fa.html", {
        "name": staff.name, "locked": wait > 0, "lock_seconds": wait,
        "error": request.session.pop("login_error", None),
    })


@app.post("/login/2fa")
def login_2fa_submit(request: Request, code: str = Form(""), db: Session = Depends(get_db),
                     _csrf: None = Depends(csrf_protect)):
    staff = _pending_2fa_staff(request, db)
    if not staff:
        request.session.clear()
        request.session["login_error"] = "That sign-in timed out. Enter your PIN again."
        return RedirectResponse("/login", status_code=303)
    wait = _sec.login_block_seconds(db, request)
    if wait:
        request.session["login_error"] = f"Too many wrong tries. Try again in {_fmt_wait(wait)}."
        return RedirectResponse("/login/2fa", status_code=303)

    code = (code or "").strip()
    used_recovery = False
    step = None
    if re.fullmatch(r"\d{3}\s?\d{3}", code):
        step = _sec.verify_totp(decrypt_value(staff.totp_secret), code, staff.totp_last_step or 0)
        ok = step is not None
    else:
        ok = _sec.use_recovery_code(staff, code)
        used_recovery = ok

    if not ok:
        _sec.log_attempt(db, request, "2fa", False, staff=staff, note="wrong authenticator or recovery code")
        fails = int(request.session.get("pending_fails", 0)) + 1
        request.session["pending_fails"] = fails
        if fails >= _PENDING_2FA_MAX_TRIES:
            request.session.clear()
            request.session["login_error"] = "Too many wrong codes. Enter your PIN again."
            return RedirectResponse("/login", status_code=303)
        request.session["login_error"] = "That code isn't right. Check the app and try again."
        return RedirectResponse("/login/2fa", status_code=303)

    if used_recovery:
        db.commit()   # the used recovery code is now spent
    else:
        staff.totp_last_step = step
        db.commit()
    _sec.log_attempt(db, request, "recovery" if used_recovery else "2fa", True, staff=staff,
                     note="signed in with a recovery code" if used_recovery else "signed in")
    start_session(request, db, staff)
    if used_recovery:
        left = _sec.recovery_codes_left(staff)
        add_audit(db, staff, "LOGIN", f"Staff login with a recovery code: {staff.name} ({left} left)")
        request.session["flash"] = ("amber", f"You signed in with a recovery code — {left} left. "
                                             "Make a fresh set below, or set up your phone again.")
        return RedirectResponse("/security", status_code=303)
    add_audit(db, staff, "LOGIN", f"Staff login: {staff.name}")
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    staff = get_current_staff(request, db)
    if staff:
        add_audit(db, staff, "LOGOUT", f"Staff logout: {staff.name}")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── FORGOT PIN (Owner recovery only) ────────────────────────────────────
# Scoped deliberately narrow: a non-owner staff member who forgets their PIN can always be reset by an
# Owner/Manager via /staff while logged in. The one scenario that can lock the *entire shop* out is the
# Owner forgetting their own PIN with nobody else able to reach /staff at all — that's the only gap this
# recovers. It shares the same per-device wrong-try pause as the PIN pad, since this is an unauthenticated
# entry point and a security-question answer can be an easier brute-force target than a PIN. It resets the
# PIN only: an Owner who has turned on the authenticator step still needs their code (or a recovery code)
# afterwards, otherwise a guessable security answer would undo the second step.
@app.get("/forgot-pin", response_class=HTMLResponse)
def forgot_pin_page(request: Request, db: Session = Depends(get_db)):
    wait = _sec.login_block_seconds(db, request)
    security_question = get_setting(db, "security_question", "")
    security_configured = bool(get_setting(db, "security_answer_hash", ""))
    owners = db.query(Staff).filter(Staff.role == "owner", Staff.active == True).all()  # noqa: E712
    return templates.TemplateResponse(request, "forgot_pin.html", {
        "locked": wait > 0, "lock_seconds": wait, "min_pin": _sec.min_pin_length(db),
        "security_question": security_question, "security_configured": security_configured,
        "owners": owners, "error": request.session.pop("recovery_error", None),
    })


@app.post("/forgot-pin")
def forgot_pin_submit(request: Request, staff_id: str = Form(...), security_answer: str = Form(...),
                       new_pin: str = Form(...), confirm_pin: str = Form(...),
                       db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    wait = _sec.login_block_seconds(db, request)
    if wait:
        request.session["recovery_error"] = f"Too many wrong tries. Try again in {_fmt_wait(wait)}."
        return RedirectResponse("/forgot-pin", status_code=303)

    answer_hash = get_setting(db, "security_answer_hash", "")
    if not answer_hash:
        request.session["recovery_error"] = "Account recovery isn't set up for this shop yet."
        return RedirectResponse("/forgot-pin", status_code=303)

    target = db.get(Staff, staff_id)
    if not target or target.role != "owner" or not target.active:
        request.session["recovery_error"] = "That account can't be recovered this way."
        return RedirectResponse("/forgot-pin", status_code=303)

    if not verify_pin(security_answer.strip().lower(), answer_hash):
        _sec.log_attempt(db, request, "recovery", False, note="wrong security answer on Forgot PIN")
        add_audit(db, None, "SECURITY_BLOCK", f"Wrong security answer on Forgot PIN for: {target.name}")
        db.commit()
        request.session["recovery_error"] = "Incorrect answer."
        return RedirectResponse("/forgot-pin", status_code=303)

    pin_error = _sec.validate_new_pin(db, new_pin, target.id, request=request)
    if pin_error:
        request.session["recovery_error"] = pin_error
        return RedirectResponse("/forgot-pin", status_code=303)
    if new_pin != confirm_pin:
        request.session["recovery_error"] = "PINs don't match."
        return RedirectResponse("/forgot-pin", status_code=303)

    _sec.log_attempt(db, request, "recovery", True, staff=target, note="PIN reset with security answer")
    target.pin_hash = hash_pin(new_pin)
    target.pin_len = len(new_pin)
    add_audit(db, None, "PIN_CHANGE", f"PIN reset via Forgot PIN recovery for: {target.name}")
    db.commit()
    request.session["login_error"] = f"PIN reset for {target.name}. Log in with the new PIN."
    return RedirectResponse("/login", status_code=303)


# ── DASHBOARD ────────────────────────────────────────────────────────
def _sparkline_points(values, w=100, h=28):
    """Turn a list of numbers into an SVG polyline points string, scaled
    to fit a w×h box. Flat series (all-equal or single value) render as
    a flat mid-line rather than dividing by zero."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    n = len(values)
    step = w / max(n - 1, 1)
    pts = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(h - ((v - lo) / rng) * h, 1) if n > 1 else h / 2
        pts.append(f"{x},{y}")
    return " ".join(pts)


def _range_bounds(range_key: str, today):
    """Returns (start, end, prev_start, prev_end) dates for the chosen
    dashboard range, where prev_* is the immediately preceding period of
    equal length — used for the 'vs previous period' comparison."""
    if range_key == "yesterday":
        start = end = today - timedelta(days=1)
        prev_start = prev_end = today - timedelta(days=2)
    elif range_key == "7d":
        start, end = today - timedelta(days=6), today
        prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    elif range_key == "30d":
        start, end = today - timedelta(days=29), today
        prev_start, prev_end = start - timedelta(days=30), start - timedelta(days=1)
    else:  # "today" (default)
        range_key = "today"
        start = end = today
        prev_start = prev_end = today - timedelta(days=1)
    return start, end, prev_start, prev_end


RANGE_LABELS = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), date_range: str = Query("today", alias="range")):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if date_range not in RANGE_LABELS:
        date_range = "today"

    # "Today" is the shop's local day, and sales are NET of refunds on the day
    # the refund happened — the same rules the Report Center uses, so the
    # dashboard and the reports always agree. Only the dates on screen are
    # queried (column-only), not every invoice ever rung up.
    tz = get_shop_tz(db)
    today = local_today(tz)
    start, end, prev_start, prev_end = _range_bounds(date_range, today)
    series = daily_totals(db, tz, min(prev_start, today - timedelta(days=6)), today)
    _blank = {"total": 0.0, "count": 0, "refund_total": 0.0}

    def _net(d):
        x = series.get(d, _blank)
        return x["total"] - x["refund_total"]

    def _span(d1, d2):
        days = [d1 + timedelta(days=n) for n in range((d2 - d1).days + 1)]
        return sum(_net(d) for d in days), sum(series.get(d, _blank)["count"] for d in days)

    sales_total, count_total = _span(start, end)
    sales_prev, count_prev = _span(prev_start, prev_end)
    sales_total, sales_prev = round(sales_total, 2), round(sales_prev, 2)

    def _trend_pct(cur, prev):
        if prev > 0:
            return round((cur - prev) / prev * 100, 1)
        return 100.0 if cur > 0 else 0.0

    sales_trend_pct = _trend_pct(sales_total, sales_prev)
    count_trend_pct = _trend_pct(count_total, count_prev)

    # Daily series across the selected range, oldest first — powers both
    # the big 7-day chart (kept fixed at 7 days regardless of the range
    # picker, since it's a stable at-a-glance chart) and the sparklines
    # inside the Sales/Invoices cards (which follow the selected range,
    # capped at 30 points so a 30-day sparkline stays readable).
    span_days = (end - start).days + 1
    daily_sales, daily_counts = [], []
    for i in range(span_days - 1, -1, -1):
        d = end - timedelta(days=i)
        daily_sales.append(round(_net(d), 2))
        daily_counts.append(series.get(d, _blank)["count"])

    sales_spark = _sparkline_points(daily_sales)
    count_spark = _sparkline_points(daily_counts)

    trend_days, trend_totals = [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        day_total = round(_net(d), 2)
        trend_days.append(d.strftime("%a"))
        trend_totals.append(day_total)

    recent_invoices = db.query(Invoice).order_by(Invoice.date.desc()).limit(8).all()
    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).all()
    open_repairs_count = db.query(Repair).filter(Repair.status.notin_(["COMPLETED", "COLLECTED"])).count()

    backup_warning = None
    checklist = None
    if role_allowed(staff, "owner"):
        last_backup = get_setting(db, "last_backup", "")
        total_customers = db.query(Customer).count()
        total_invoices = db.query(Invoice).count()
        if not last_backup:
            if total_invoices > 0 or total_customers > 2:
                backup_warning = "You have live data but have never exported a backup."
        else:
            days_since = (datetime.utcnow() - datetime.fromisoformat(last_backup)).days
            if days_since >= 7:
                backup_warning = f"Last backup was {days_since} day{'s' if days_since != 1 else ''} ago."

        if not get_setting(db, "hide_setup_checklist", ""):
            items = [
                {"label": "Confirm your province & tax rate", "done": db.get(Setting, "province") is not None, "href": "/settings"},
                {"label": "Invite your team", "done": db.query(Staff).count() > 1, "href": "/staff"},
                {"label": "Set up email receipts", "done": bool(get_setting(db, "smtp_host", "")), "href": "/settings"},
                {"label": "Ring up your first sale", "done": total_invoices > 0, "href": "/pos"},
                {"label": "Export a backup", "done": bool(last_backup), "href": "/export/backup"},
            ]
            done_count = sum(1 for it in items if it["done"])
            if done_count < len(items):
                checklist = {"entries": items, "done_count": done_count, "total": len(items)}

    activity = (
        db.query(AuditLog)
        .order_by(AuditLog.ts.desc())
        .limit(8)
        .all()
    )

    # v53: "needs attention today" — plain counts that each link to the screen
    # where you act on them. Read-only; nothing here changes any record.
    attention = []
    today_iso = today.isoformat()
    open_states = ["COMPLETED", "COLLECTED"]
    ready_n = db.query(Repair).filter(Repair.status == "READY").count()
    if ready_n:
        attention.append({"n": ready_n, "tone": "go", "title": "Ready for pickup", "sub": "Text them or hand it over", "href": "/repairs"})
    late_n = db.query(Repair).filter(Repair.status.notin_(open_states), Repair.promised_by != "",
                                      Repair.promised_by.isnot(None), Repair.promised_by < today_iso).count()
    if late_n:
        attention.append({"n": late_n, "tone": "hot", "title": "Past promised date", "sub": "Repairs running late", "href": "/repairs?view=table"})
    try:
        waiting_q = len(_quotes.unseen_answers(db))
    except Exception:  # a quote problem must never break the home screen
        waiting_q = 0
        logging.getLogger("techpro").exception("dashboard quote count failed")
    if waiting_q:
        attention.append({"n": waiting_q, "tone": "wait", "title": "Quote answers", "sub": "Customer replied — open to review", "href": "/repairs"})
    late_lay = db.query(Layaway).filter(Layaway.status == "active", Layaway.due_date.isnot(None),
                                         Layaway.due_date != "", Layaway.due_date < today_iso).count()
    if late_lay:
        attention.append({"n": late_lay, "tone": "hot", "title": "Layaways past due", "sub": "Follow up with the customer", "href": "/layaway"})

    return templates.TemplateResponse(request, "dashboard.html", {
        "staff": staff,
        "range": date_range, "range_label": RANGE_LABELS[date_range], "range_labels": RANGE_LABELS,
        "sales_total": sales_total, "count_total": count_total,
        "sales_trend_pct": sales_trend_pct, "count_trend_pct": count_trend_pct,
        "sales_spark": sales_spark, "count_spark": count_spark,
        "trend_days": trend_days, "trend_totals": trend_totals,
        "open_repairs_count": open_repairs_count,
        "recent_invoices": recent_invoices, "low_stock": low_stock,
        "shop_name": get_setting(db, "shop_name", "TechPro+"),
        "backup_warning": backup_warning,
        "checklist": checklist,
        "activity": activity,
        "attention": attention,
    })


@app.post("/dashboard/dismiss-checklist")
def dismiss_checklist(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff or not role_allowed(staff, "owner"):
        return RedirectResponse("/", status_code=303)
    set_setting(db, "hide_setup_checklist", "1")
    return RedirectResponse("/", status_code=303)


@app.get("/api/notifications")
def api_notifications(request: Request, db: Session = Depends(get_db)):
    """Feeds the notification bell dropdown in the top bar. Lightweight
    and read-only so it can be polled from any page via fetch()."""
    staff = require_login(request, db)
    if not staff:
        return {"notifications": []}

    notes = []
    if staff.role == "owner" and not staff.totp_enabled:
        notes.append({"type": "warning", "icon": "🔐", "href": "/security",
                      "message": "Turn on 2-step sign-in for the Owner account"})
    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).all()
    if low_stock:
        notes.append({
            "type": "warning", "icon": "📦",
            "message": f"{len(low_stock)} product{'s' if len(low_stock) != 1 else ''} low on stock",
            "href": "/products/reorder",
        })

    overdue_cutoff = datetime.utcnow() - timedelta(days=5)
    overdue_repairs = db.query(Repair).filter(
        Repair.status.notin_(["COMPLETED", "COLLECTED"]),
        Repair.created_at < overdue_cutoff,
    ).count()
    if overdue_repairs:
        notes.append({
            "type": "danger", "icon": "🔧",
            "message": f"{overdue_repairs} repair{'s' if overdue_repairs != 1 else ''} open 5+ days",
            "href": "/repairs",
        })

    recent_replies = db.query(SmsMessage).filter(
        SmsMessage.direction == "in", SmsMessage.created_at >= datetime.utcnow() - timedelta(hours=24),
    ).count()
    if recent_replies:
        notes.append({
            "type": "warning", "icon": "💬",
            "message": f"{recent_replies} SMS repl{'ies' if recent_replies != 1 else 'y'} in the last 24h",
            "href": "/customers",
        })

    # v52: customers who answered a quote by text link and nobody has looked at yet
    try:
        for q in _quotes.unseen_answers(db):
            rep = db.get(Repair, q.repair_id)
            if rep is None:
                continue
            ok = q.status == "approved"
            notes.append({
                "type": "success" if ok else "warning", "icon": "✅" if ok else "✖️",
                "message": f"Ticket #{rep.ticket_no}: customer {'approved' if ok else 'declined'} the ${q.amount:.2f} quote",
                "href": f"/repairs/{rep.id}#quote"})
    except Exception:  # never let a quote problem break the bell
        logging.getLogger("techpro").exception("quote notifications failed")

    active_layaways = db.query(Layaway).filter(Layaway.status == "active").all()
    _today = local_today(get_shop_tz(db))
    today_str = _today.isoformat()
    overdue_layaways = [l for l in active_layaways if l.due_date and l.due_date < today_str]
    due_soon_layaways = [
        l for l in active_layaways
        if l.due_date and today_str <= l.due_date <= (_today + timedelta(days=3)).isoformat()
    ]
    if overdue_layaways:
        notes.append({
            "type": "danger", "icon": "📦",
            "message": f"{len(overdue_layaways)} layaway{'s' if len(overdue_layaways) != 1 else ''} past due date",
            "href": "/layaway",
        })
    elif due_soon_layaways:
        notes.append({
            "type": "warning", "icon": "📦",
            "message": f"{len(due_soon_layaways)} layaway{'s' if len(due_soon_layaways) != 1 else ''} due within 3 days",
            "href": "/layaway",
        })

    if role_allowed(staff, "owner"):
        last_backup = get_setting(db, "last_backup", "")
        if not last_backup:
            total_invoices = db.query(Invoice).count()
            if total_invoices > 0:
                notes.append({"type": "warning", "icon": "💾", "message": "No backup has ever been exported", "href": "/export/backup"})
        else:
            days_since = (datetime.utcnow() - datetime.fromisoformat(last_backup)).days
            if days_since >= 7:
                notes.append({"type": "warning", "icon": "💾", "message": f"Last backup was {days_since} days ago", "href": "/export/backup"})

    if role_allowed(staff, "owner", "manager"):
        try:
            for c in summarize_checks(day_end_checks(db, get_shop_tz(db), full=False)):
                notes.append({"type": "warning" if c["severity"] == "warn" else "danger", "icon": "💳", "message": c["message"], "href": c["href"]})
        except Exception:  # a checks problem must never break the dashboard
            logging.getLogger("techpro").exception("day-end checks failed")

    return {"notifications": notes}


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, db: Session = Depends(get_db), q: str = ""):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    customers, invoices, repairs = [], [], []
    q = q.strip()
    if q:
        like = f"%{q}%"
        _conds = [Customer.name.ilike(like), Customer.phone.ilike(like), Customer.email.ilike(like)]
        _pc = phone_search_clause(q)
        if _pc is not None:
            _conds.append(_pc)
        customers = db.query(Customer).filter(or_(*_conds)).limit(15).all()
        invoices = db.query(Invoice).filter(Invoice.number.ilike(like)).limit(15).all()
        repair_filters = [Repair.device.ilike(like), Repair.issue.ilike(like), Repair.imei.ilike(like)]
        if q.isdigit():
            repair_filters.append(Repair.ticket_no == int(q))
        repairs = db.query(Repair).filter(or_(*repair_filters)).limit(15).all()

        # An IMEI/serial search should also surface any invoice a device
        # was actually sold on — not just repair tickets — since that's
        # the other half of "trace this device's history" (warranty
        # claims, stolen-phone lookups).
        if len(q) >= 4:
            imei_invoice_ids = [row[0] for row in db.query(InvoiceLine.invoice_id)
                                 .filter(InvoiceLine.imei.ilike(like)).limit(15).all()]
            if imei_invoice_ids:
                extra = db.query(Invoice).filter(Invoice.id.in_(imei_invoice_ids)).all()
                seen = {i.id for i in invoices}
                invoices += [i for i in extra if i.id not in seen]

    return templates.TemplateResponse(request, "search.html", {
        "staff": staff, "q": q,
        "customers": customers, "invoices": invoices, "repairs": repairs,
        "shop_name": get_setting(db, "shop_name", "TechPro+"),
    })




# ── POS ──────────────────────────────────────────────────────────────
@app.get("/pos", response_class=HTMLResponse)
def pos_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    products = db.query(Product).all()
    all_brands = sorted({p.subcategory for p in products if p.subcategory})
    repair_ctx_id = request.session.get("pos_repair_id")
    repair_ctx = db.get(Repair, repair_ctx_id) if repair_ctx_id else None
    product_groups, ungrouped_products = group_products_by_variant(products)
    variant_group_data = {
        g["name"]: [
            {"pid": v.id, "name": v.name, "sku": v.sku, "price": v.price, "stock": v.stock,
             "reorder_threshold": v.reorder_threshold, "brand": v.subcategory or "",
             "phone_brand": v.phone_brand or "", "phone_model": v.phone_model or "", "color": v.color or ""}
            for v in g["variants"]
        ]
        for g in product_groups
    }
    phone_menu = {}
    for p in products:
        if p.phone_brand and p.phone_model:
            phone_menu.setdefault(p.phone_brand, set()).add(p.phone_model)
    phone_menu = {brand: sorted(models) for brand, models in phone_menu.items()}
    all_phone_brands = sorted(phone_menu.keys())
    return templates.TemplateResponse(request, "pos.html", {
        "staff": staff, "products": products,
        "product_groups": product_groups, "ungrouped_products": ungrouped_products,
        "variant_group_data": variant_group_data,
        "phone_menu": phone_menu, "all_phone_brands": all_phone_brands,
        "category_labels": CATEGORY_LABELS, "all_brands": all_brands,
        "cat_subcategories": CAT_SUBCATEGORIES,
        "repair_ctx": repair_ctx,
        **pos_cart_context(request, db),
    })


@app.post("/pos/scan")
def pos_scan(request: Request, sku: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Looks up a scanned/typed SKU and hands it to the same price-check
    modal a tapped product tile already uses — instead of adding straight
    to the cart, so staff get one chance to confirm the price or apply an
    on-the-spot discount before it's in the sale. Every barcode is
    inherently unambiguous (matches exactly one Product row), so this
    never needs the variant-picker modal a grouped tile can trigger.

    Only the AJAX (JS-enabled) path behaves this way. A non-AJAX request
    — JS failed, a stale tab, curl — has no way to show a modal at all,
    so it falls back to the original direct-add-to-cart behavior, same as
    every other cart-mutating route's no-JS safety net.
    """
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    needle = sku.strip().upper()
    is_ajax = request.headers.get("X-Pos-Ajax") == "1"

    if not needle:
        if is_ajax:
            return {"found": False, "error": ""}
        return pos_response(request, db)

    matches, how = _barcodes.find_products(db, needle)     # SKU first, then the manufacturer's barcode (any UPC/EAN spelling)
    product = matches[0] if matches else None
    if not product:
        product = db.query(Product).filter(Product.name.ilike(f"%{needle}%")).first()
        how = "name" if product else ""

    if not product:
        if is_ajax:
            return {"found": False, "error": f'No product found for "{sku}"', "code": _barcodes.clean(sku)}
        request.session["scan_error"] = f'No product found for "{sku}"'
        return pos_response(request, db)

    if is_ajax:
        return {"found": True, "how": how, "shared": len(matches) > 1, "product": {
            "pid": product.id, "name": product.name, "sku": product.sku,
            "price": product.price, "stock": product.stock,
        }}

    cart = cart_get(request)
    for item in cart:
        if item["product_id"] == product.id:
            item["qty"] += 1
            break
    else:
        cart.append({"product_id": product.id, "name": product.name, "sku": product.sku, "price": product.price, "qty": 1})
    request.session["cart"] = cart
    request.session["sub_override"] = None
    return pos_response(request, db)


@app.post("/pos/add/{product_id}")
def pos_add(request: Request, product_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    product = db.get(Product, product_id)
    if product:
        cart = cart_get(request)
        for item in cart:
            if item["product_id"] == product_id and item["price"] == product.price:
                item["qty"] += 1
                break
        else:
            cart.append({"product_id": product.id, "name": product.name, "sku": product.sku, "price": product.price, "qty": 1})
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return pos_response(request, db)


@app.post("/pos/add-custom")
def pos_add_custom(request: Request, product_id: str = Form(...),
                    custom_price: float = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Add a product to the cart at a user-specified price instead of the stored price.
    Each custom-price entry is always a separate line item (never merged with other
    entries for the same product, since the price may differ)."""
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    product = db.get(Product, product_id)
    if product and _inputs.money(custom_price):
        cart = cart_get(request)
        cart.append({
            "product_id": product.id,
            "name": product.name,
            "sku": product.sku,
            "price": round(custom_price, 2),
            "qty": 1,
        })
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return pos_response(request, db)


@app.post("/pos/qty/{idx}")
def pos_qty(request: Request, idx: int, qty: int = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if 0 <= idx < len(cart) and qty <= _inputs.QTY_MAX:
        if qty <= 0:
            cart.pop(idx)
        else:
            cart[idx]["qty"] = qty
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return pos_response(request, db)


@app.post("/pos/imei/{idx}")
def pos_imei(request: Request, idx: int, imei: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Attaches an IMEI/serial to a specific cart line before checkout —
    copied onto the InvoiceLine at checkout time so a sold device's
    serial is on permanent record against that sale."""
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if 0 <= idx < len(cart):
        cart[idx]["imei"] = _imei.clean(imei)
        request.session["cart"] = cart
    return pos_response(request, db)


@app.post("/pos/remove/{idx}")
def pos_remove(request: Request, idx: int, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if 0 <= idx < len(cart):
        cart.pop(idx)
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return pos_response(request, db)


@app.post("/pos/clear")
def pos_clear(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    request.session["pos_repair_id"] = None
    reset_customer_redemptions(request)
    return pos_response(request, db)


@app.post("/pos/subtotal")
def pos_subtotal(request: Request, value: float = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    if _inputs.money(value):
        request.session["sub_override"] = max(0, value)
    return pos_response(request, db)


@app.post("/pos/discount")
def pos_discount(request: Request, mode: str = Form(...), value: float = Form(0), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    if _inputs.money(value):
        request.session["disc_mode"] = mode if mode in ("$", "%") else "$"
        request.session["disc_value"] = max(0, value)
    return pos_response(request, db)


@app.post("/pos/customer")
def pos_customer(request: Request, customer_id: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["customer_id"] = customer_id or None
    reset_customer_redemptions(request)  # redemptions are tied to whoever was previously attached
    return pos_response(request, db)


@app.post("/pos/redeem-points")
def pos_redeem_points(request: Request, points: int = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None
    rate = float(get_setting(db, "points_redeem_rate", "100"))
    if customer:
        # Round down to a whole multiple of the redemption rate, same as the original
        step = max(1, int(rate))
        pts = (points // step) * step
        if 0 < pts <= (customer.points or 0):
            dollar_value = pts / max(1.0, rate)
            request.session["loyalty_discount"] = (request.session.get("loyalty_discount", 0) or 0) + dollar_value
    return pos_response(request, db)


@app.post("/pos/redeem-credit")
def pos_redeem_credit(request: Request, amount: float = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer and _inputs.money(amount) and 0 < amount <= (customer.store_credit or 0):
        request.session["store_credit_used"] = (request.session.get("store_credit_used", 0) or 0) + amount
    return pos_response(request, db)


@app.post("/pos/hold")
def pos_hold(request: Request, name: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if not cart:
        return pos_response(request, db)
    held = HeldCart(
        name=name.strip() or f"Hold {datetime.utcnow().strftime('%H:%M:%S')}",
        cart_json=json.dumps(cart),
        customer_id=request.session.get("customer_id"),
        disc_mode=request.session.get("disc_mode", "$"),
        disc_value=request.session.get("disc_value", 0) or 0,
    )
    db.add(held)
    add_audit(db, staff, "HOLD_CART", f"Cart held: {held.name}")
    db.commit()

    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    reset_customer_redemptions(request)
    return pos_response(request, db)


@app.post("/pos/recall/{held_id}")
def pos_recall(request: Request, held_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    held = db.get(HeldCart, held_id)
    if held:
        request.session["cart"] = json.loads(held.cart_json)
        request.session["sub_override"] = None
        request.session["customer_id"] = held.customer_id
        request.session["disc_mode"] = held.disc_mode
        request.session["disc_value"] = held.disc_value
        reset_customer_redemptions(request)
        add_audit(db, staff, "RECALL_CART", f"Cart recalled: {held.name}")
        db.delete(held)
        db.commit()
    return pos_response(request, db)


@app.post("/pos/held/{held_id}/delete")
def pos_held_delete(request: Request, held_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    held = db.get(HeldCart, held_id)
    if held:
        db.delete(held)
        db.commit()
    return pos_response(request, db)


@app.post("/pos/checkout")
def pos_checkout(request: Request, payment_method: str = Form("Cash"),
                  tendered: float = Form(0),
                  cash_part: float = Form(0),
                  card_part: float = Form(0),
                  card_reference: str = Form(""),
                  db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    cart = cart_get(request)
    if not cart:
        return RedirectResponse("/pos", status_code=303)

    totals = cart_totals(request, db)
    total = totals["total"]

    if not all(_inputs.money(x) for x in (tendered, cash_part, card_part)):
        return HTMLResponse(f"Enter the amounts received as numbers ({_inputs.money_text()}).", status_code=400)

    # ── Payment resolution ──────────────────────────────────────────
    # One place (app/money.py) works out the cash/card split, the 5c cash
    # rounding and the change, so the checkout, the receipts and Cash Up can
    # never disagree about what went into the drawer.
    plan = plan_payment(payment_method, total, tendered, cash_part, card_part, cash_rounding_on(db))
    if plan["error"]:
        return HTMLResponse(plan["error"], status_code=400)
    payment_method = plan["payment_method"]
    tendered = plan["tendered"]
    change_given = plan["change_given"]

    # The Moneris reference ties a CRM sale to a terminal transaction. A full
    # card number pasted here by mistake is masked to its last four digits —
    # the CRM never stores one.
    card_reference, pan_masked = scrub_card_numbers(card_reference.strip())
    card_reference = card_reference[:64]  # a receipt code, not free text
    if plan["card_amount"] > 0 and require_card_ref_on(db) and not card_reference:
        request.session["checkout_error"] = ("Enter the Moneris reference / approval code from the terminal receipt "
                                             "before saving a card sale — it is how this sale gets matched to the "
                                             "terminal batch. (Settings can switch this off.)")
        return RedirectResponse("/pos", status_code=303)

    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None

    # "Store Credit" as the way of paying: the sale comes out of the attached customer's balance. Before v52.2 it saved the sale
    # with nothing taken off any balance, and with no customer at all (free goods).
    if payment_method == "Store Credit":
        if not customer:
            request.session["scan_error"] = "Attach the customer first: Store Credit is paid from their balance."
            return RedirectResponse("/pos", status_code=303)
        if round(customer.store_credit or 0, 2) < round(total, 2):
            request.session["scan_error"] = (f"{customer.name} has ${(customer.store_credit or 0):.2f} store credit, less than the "
                                             f"${total:.2f} total. Pay with another method (or apply part of the credit with the Redeem box and pay the rest).")
            return RedirectResponse("/pos", status_code=303)

    loyalty_discount = totals["loyalty_discount"]
    store_credit_used = totals["store_credit_used"]
    rate = float(get_setting(db, "points_redeem_rate", "100"))
    loyalty_pts_used = round(loyalty_discount * rate)

    repair_ctx_id = request.session.get("pos_repair_id")
    repair_ctx = db.get(Repair, repair_ctx_id) if repair_ctx_id else None

    cash_amount, card_amount, cash_rounding = plan["cash_amount"], plan["card_amount"], plan["cash_rounding"]

    # Two checkouts completing at the exact same instant (two terminals, two
    # staff) could theoretically read the same counter value before either
    # writes it back. The database's unique constraint on `number` guarantees
    # a duplicate can never actually be saved — this retry loop just makes
    # sure that collision produces a fresh number instead of a failed sale.
    # A fresh Invoice object is built each attempt rather than reusing one
    # across a rollback, to avoid relying on SQLAlchemy's session-state
    # behavior for an object involved in a failed flush — this is money, so
    # the extra caution here is deliberate.
    invoice = None
    for attempt in range(5):
        candidate = Invoice(
            number=next_invoice_number(db),
            customer_id=customer.id if customer else None,
            staff_id=staff.id,
            repair_id=repair_ctx.id if repair_ctx else None,
            payment_method=payment_method,
            cash_amount=cash_amount,
            cash_rounding=cash_rounding,
            card_amount=card_amount,
            card_reference=card_reference,
            subtotal=totals["sub"],
            discount=totals["disc"],
            loyalty_pts_used=loyalty_pts_used,
            store_credit_used=store_credit_used,
            tendered=tendered,
            change_given=change_given,
            tax_breakdown=json.dumps(totals["tax"]["lines"]),
            tax_total=totals["tax"]["tax_total"],
            total=total,
        )
        db.add(candidate)
        try:
            db.flush()
            invoice = candidate
            break
        except IntegrityError:
            db.rollback()
            if attempt == 4:
                raise
    if invoice is None:
        raise RuntimeError("Could not generate a unique invoice number after 5 attempts")

    for item in cart:
        product = db.get(Product, item["product_id"]) if item["product_id"] else None
        # unit_cost/category are frozen onto the line at the moment of sale, so
        # later cost edits or a deleted product can't rewrite past profit.
        db.add(InvoiceLine(invoice_id=invoice.id, product_id=item["product_id"],
                            name=item["name"], sku=item.get("sku", ""), qty=item["qty"], price=item["price"],
                            imei=item.get("imei", ""),
                            unit_cost=(product.cost or 0) if product else None,
                            category=product.category if product else None))
        if product:
            product.stock = max(0, product.stock - item["qty"])

    if customer:
        # Spend redemptions first (mirrors the original's order of operations)
        if store_credit_used > 0:
            customer.store_credit = round((customer.store_credit or 0) - store_credit_used, 2)
            add_audit(db, staff, "STORE_CREDIT", f"Redeemed ${store_credit_used:.2f} store credit for {customer.name}")
        if payment_method == "Store Credit" and total > 0:
            customer.store_credit = round((customer.store_credit or 0) - total, 2)
            add_audit(db, staff, "STORE_CREDIT", f"Paid ${total:.2f} of invoice {invoice.number} from {customer.name}'s store credit")
        if loyalty_pts_used > 0:
            customer.points = max(0, (customer.points or 0) - loyalty_pts_used)
            add_audit(db, staff, "LOYALTY", f"Redeemed {loyalty_pts_used} points for ${loyalty_discount:.2f} — {customer.name}")
        # Earn new points on the final total
        points_per_dollar = float(get_setting(db, "points_per_dollar", "1"))
        customer.points = (customer.points or 0) + int(total * points_per_dollar)
        customer.spent = round((customer.spent or 0) + total, 2)
        customer.last_visit = datetime.utcnow().isoformat()

    add_audit(db, staff, "INVOICE_CREATE", f"Invoice {invoice.number} — ${total:.2f} — {payment_method}")

    collected_note = ""
    if repair_ctx and repair_ctx.status not in ("COLLECTED",):
        repair_ctx.status = "COLLECTED"
        if repair_ctx.final_cost is None:
            # The ticket's price is what the repair itself was charged, before tax and before any whole-sale
            # discount: that is the number comparable to the quote and to the parts cost. (The invoice total
            # includes tax, which would overstate the margin and read as "came in higher than the quote".)
            _tag = f"RPR-{repair_ctx.ticket_no}"
            _line = sum(float(l.get("price") or 0) * int(l.get("qty") or 1) for l in cart if l.get("sku") == _tag)
            repair_ctx.final_cost = round(_line if _line > 0 else totals["sub"], 2)
        repair_ctx.updated_at = datetime.utcnow()
        rhistory = json.loads(repair_ctx.status_history) if repair_ctx.status_history else []
        rhistory.append({"status": "COLLECTED", "note": f"Paid via {payment_method}, Invoice {invoice.number}",
                          "date": datetime.utcnow().isoformat()})
        repair_ctx.status_history = json.dumps(rhistory)
        add_audit(db, staff, "REPAIR_STATUS", f"#{repair_ctx.ticket_no} → Collected (Invoice {invoice.number})")
        collected_note = f" Repair ticket #{repair_ctx.ticket_no} marked Collected."
        _messaging_hook(_messaging.on_repair_collected, db, repair_ctx)

    db.commit()

    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    request.session["pos_repair_id"] = None
    reset_customer_redemptions(request)
    due_note = f" Cash due ${cash_amount:.2f} (rounded to 5¢)." if cash_rounding else ""
    pan_note = " A full card number was entered in the reference box — only its last four digits were kept." if pan_masked else ""
    request.session["flash"] = ("green", f"✓ Sale complete — Invoice {invoice.number} for ${total:.2f}.{due_note}"
                                          f"{collected_note}{pan_note} Use the buttons below to email or print the receipt.")
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


# ── INVOICES ─────────────────────────────────────────────────────────
@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoices = db.query(Invoice).order_by(Invoice.date.desc()).limit(100).all()
    tz = get_shop_tz(db)
    today = local_today(tz)
    today_total = round(sum(i.total for i in invoices if to_local_date(i.date, tz) == today and not i.refunded), 2)
    today_count = sum(1 for i in invoices if to_local_date(i.date, tz) == today)
    _s, _e = day_bounds_utc(today, today, tz)
    today_returned = round(_returns.sum_total(db, _s, _e), 2)      # v52: items taken back from sales today
    today_total = round(today_total - today_returned, 2)
    return templates.TemplateResponse(request, "invoices.html", {
        "staff": staff, "invoices": invoices, "today_total": today_total, "today_count": today_count,
        "today_returned": today_returned,
    })


def get_shop_info(db: Session):
    return {
        "name": get_setting(db, "shop_name", "TechPro+"),
        "address": get_setting(db, "shop_address", ""),
        "phone": get_setting(db, "shop_phone", ""),
        "email": get_setting(db, "shop_email", ""),
        "gst": get_setting(db, "shop_gst", ""),
        "pst": get_setting(db, "shop_pst", ""),
    }


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    tax_lines = json.loads(invoice.tax_breakdown) if invoice.tax_breakdown else []
    can_refund = role_allowed(staff, "owner", "manager")
    flash = request.session.pop("flash", None)
    province = get_setting(db, "province", "ON")
    all_products = db.query(Product).order_by(Product.name).all() if can_refund else []
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "staff": staff, "invoice": invoice, "shop": get_shop_info(db),
        "province_label": PROVINCE_LABELS.get(province, province),
        "tax_lines": tax_lines, "can_refund": can_refund, "flash": flash,
        "all_products": all_products,
        "ret_state": _returns.state(invoice), "ret_block": _returns.can_start(invoice),
        "ret_just_saved": request.session.pop("return_just_saved", None),
    })


@app.get("/invoices/{invoice_id}/thermal", response_class=HTMLResponse)
def invoice_thermal(request: Request, invoice_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    tax_lines = json.loads(invoice.tax_breakdown) if invoice.tax_breakdown else []
    return templates.TemplateResponse(request, "invoice_thermal.html", {
        "staff": staff, "invoice": invoice, "shop": get_shop_info(db), "tax_lines": tax_lines,
    })


# ── LAYAWAY ──────────────────────────────────────────────────────────
@app.post("/pos/layaway/new")
def pos_layaway_new(request: Request, deposit: float = Form(0), due_date: str = Form(""),
                     db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Converts the current cart into a layaway instead of a straight
    sale. Stock is deducted immediately (reserved for this customer, so
    the same item can't be sold to someone else while it's being paid
    off) — the same way pos_checkout() deducts stock, just without
    creating an Invoice yet."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    cart = cart_get(request)
    if not cart:
        return RedirectResponse("/pos", status_code=303)

    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None
    if not customer:
        request.session["scan_error"] = "A layaway needs a customer attached to the sale — select or add one first."
        return RedirectResponse("/pos", status_code=303)

    if not _inputs.money(deposit):
        request.session["scan_error"] = f"The deposit must be a number from {_inputs.money_text()}. No layaway was created."
        return RedirectResponse("/pos", status_code=303)
    totals = cart_totals(request, db)
    if totals["store_credit_used"] > 0 or totals["loyalty_discount"] > 0:
        # a layaway is paid in instalments; credit or points taken off up front would never be taken from the customer's balance
        request.session["scan_error"] = "Remove the store credit / points from this sale first: they can't be used on a layaway."
        return RedirectResponse("/pos", status_code=303)
    deposit = round(max(0, deposit), 2)

    layaway = Layaway(
        number=next_layaway_number(db),
        customer_id=customer.id,
        cart_json=json.dumps(cart),
        subtotal=totals["sub"],
        tax_breakdown=json.dumps(totals["tax"]["lines"]),
        tax_total=totals["tax"]["tax_total"],
        total=totals["total"],
        paid_total=0,
        due_date=due_date.strip(),
        staff_id=staff.id,
    )
    db.add(layaway)
    db.flush()

    for item in cart:
        product = db.get(Product, item["product_id"]) if item.get("product_id") else None
        if product:
            product.stock = max(0, product.stock - item["qty"])

    if deposit > 0:
        db.add(LayawayPayment(layaway_id=layaway.id, amount=deposit, method="Cash", staff_id=staff.id, staff_name=staff.name))
        layaway.paid_total = deposit

    add_audit(db, staff, "LAYAWAY_CREATE", f"{layaway.number} — {customer.name} — total ${layaway.total:.2f}, deposit ${deposit:.2f}")
    db.commit()

    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    reset_customer_redemptions(request)
    return RedirectResponse(f"/layaway/{layaway.id}", status_code=303)


@app.get("/layaway", response_class=HTMLResponse)
def layaway_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    layaways = db.query(Layaway).order_by(Layaway.created_at.desc()).all()
    active_total_owed = round(sum(l.total - l.paid_total for l in layaways if l.status == "active"), 2)
    return templates.TemplateResponse(request, "layaway_list.html", {
        "staff": staff, "layaways": layaways, "active_total_owed": active_total_owed,
    })


@app.get("/layaway/{layaway_id}", response_class=HTMLResponse)
def layaway_detail(request: Request, layaway_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    layaway = db.get(Layaway, layaway_id)
    if not layaway:
        return RedirectResponse("/layaway", status_code=303)
    cart = json.loads(layaway.cart_json or "[]")
    tax_lines = json.loads(layaway.tax_breakdown or "[]")
    balance = round(layaway.total - layaway.paid_total, 2)
    return templates.TemplateResponse(request, "layaway_detail.html", {
        "staff": staff, "layaway": layaway, "cart": cart, "tax_lines": tax_lines,
        "balance": max(0, balance), "flash": request.session.pop("flash", None),
    })


@app.post("/layaway/{layaway_id}/payment")
def layaway_payment(request: Request, layaway_id: str, amount: float = Form(...), method: str = Form("Cash"),
                     db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    layaway = db.get(Layaway, layaway_id)
    if not layaway or layaway.status != "active":
        return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)

    if not _inputs.money(amount, lo=-_inputs.MONEY_MAX):
        request.session["flash"] = ("red", f"Enter a payment amount greater than $0 and no more than ${_inputs.MONEY_MAX:,.0f}.")
        return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)
    amount = round(amount, 2)
    if amount <= 0:
        request.session["flash"] = ("red", "Enter a payment amount greater than $0.")
        return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)

    # Only what the shop keeps is recorded. A $100 note handed over for a $16.25 balance is a $16.25 payment and $83.75 back as
    # change; before v52.2 the full $100 was recorded, so Cash Up expected $83.75 more in the drawer than there was. A card or
    # e-transfer can't be partly refunded on the spot, so those must not be more than what is owed.
    owing = round((layaway.total or 0) - (layaway.paid_total or 0), 2)
    change_back = 0.0
    if amount > owing + 0.005:
        if method != "Cash":
            request.session["flash"] = ("red", f"That is more than the ${owing:.2f} still owed. Enter ${owing:.2f} or less (only a cash payment can be over, so change can be handed back).")
            return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)
        change_back = round(amount - owing, 2)
        amount = owing
    db.add(LayawayPayment(layaway_id=layaway.id, amount=amount, method=method, staff_id=staff.id, staff_name=staff.name))
    layaway.paid_total = round((layaway.paid_total or 0) + amount, 2)
    layaway.updated_at = datetime.utcnow()
    add_audit(db, staff, "LAYAWAY_PAYMENT", f"{layaway.number} — ${amount:.2f} via {method}")

    completed_note = ""
    if layaway.paid_total >= layaway.total - 0.005:
        # Fully paid off — convert to a real invoice. Stock was already
        # deducted at creation, so this does NOT touch product.stock again.
        cart = json.loads(layaway.cart_json or "[]")
        invoice = None
        for attempt in range(5):
            candidate = Invoice(
                number=next_invoice_number(db),
                customer_id=layaway.customer_id,
                staff_id=staff.id,
                payment_method=f"Layaway ({layaway.number})",
                # cash_amount/card_amount stay at their column default (0) here
                # on purpose: this one invoice represents the *whole* layaway,
                # but the actual cash/card money arrived across however many
                # separate LayawayPayment installments, each on its own day.
                # Cash Up counts those individually by their own created_at
                # date — attributing the full total to today (the payoff day)
                # would double-count every earlier installment and wreck the
                # reconciliation on both days.
                subtotal=layaway.subtotal,
                discount=0,
                tendered=layaway.paid_total,
                change_given=max(0, round(layaway.paid_total - layaway.total, 2)),
                tax_breakdown=layaway.tax_breakdown,
                tax_total=layaway.tax_total,
                total=layaway.total,
            )
            db.add(candidate)
            try:
                db.flush()
                invoice = candidate
                break
            except IntegrityError:
                db.rollback()
                if attempt == 4:
                    raise
        for item in cart:
            _lp = db.get(Product, item.get("product_id")) if item.get("product_id") else None
            db.add(InvoiceLine(invoice_id=invoice.id, product_id=item.get("product_id"),
                                name=item["name"], sku=item.get("sku", ""), qty=item["qty"], price=item["price"],
                                imei=item.get("imei", ""),
                                unit_cost=(_lp.cost or 0) if _lp else None,
                                category=_lp.category if _lp else None))
        customer = db.get(Customer, layaway.customer_id) if layaway.customer_id else None
        if customer:
            points_per_dollar = float(get_setting(db, "points_per_dollar", "1"))
            customer.points = (customer.points or 0) + int(layaway.total * points_per_dollar)
            customer.spent = round((customer.spent or 0) + layaway.total, 2)
            customer.last_visit = datetime.utcnow().isoformat()
        layaway.status = "completed"
        layaway.invoice_id = invoice.id
        add_audit(db, staff, "LAYAWAY_COMPLETE", f"{layaway.number} paid off — converted to Invoice {invoice.number}")
        completed_note = f" Fully paid — converted to Invoice {invoice.number}."

    db.commit()
    change_note = f" Give ${change_back:.2f} back as change." if change_back else ""
    request.session["flash"] = ("green", f"Payment of ${amount:.2f} recorded.{change_note}{completed_note}")
    return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)


@app.post("/layaway/{layaway_id}/cancel")
def layaway_cancel(request: Request, layaway_id: str, outcome: str = Form("cancelled"),
                    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Ends an active layaway without completing the sale. Restocks the
    reserved items either way — 'cancelled' vs 'forfeited' only changes
    the record of what happened to the deposit already paid (forfeited =
    shop keeps it; cancelled = implies it gets refunded to the customer
    outside this system — refund the cash/store-credit manually)."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — cancelling a layaway requires manager or owner role.", status_code=403)

    layaway = db.get(Layaway, layaway_id)
    if layaway and layaway.status == "active":
        cart = json.loads(layaway.cart_json or "[]")
        for item in cart:
            product = db.get(Product, item.get("product_id")) if item.get("product_id") else None
            if product:
                product.stock = product.stock + item["qty"]
        layaway.status = "forfeited" if outcome == "forfeited" else "cancelled"
        layaway.updated_at = datetime.utcnow()
        add_audit(db, staff, "LAYAWAY_EDIT", f"{layaway.number} marked {layaway.status} — ${layaway.paid_total:.2f} paid so far, items restocked")
        db.commit()
        request.session["flash"] = ("green", f"Layaway {layaway.status}. Items have been restocked.")
    return RedirectResponse(f"/layaway/{layaway_id}", status_code=303)


REFUND_METHODS = ("Cash", "Card", "Store Credit", "E-Transfer")


@app.post("/invoices/{invoice_id}/refund")
def invoice_refund(request: Request, invoice_id: str, refund_method: str = Form(""), refund_reason: str = Form(""),
                    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — refunds require manager or owner role.", status_code=403)

    invoice = db.get(Invoice, invoice_id)
    if invoice and not invoice.refunded and invoice.returns:
        request.session["flash"] = ("red", "Some items from this invoice were already returned, so it can't be refunded in full. "
                                           "Use Return items to refund what's left.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    if invoice and not invoice.refunded:
        method = refund_method if refund_method in REFUND_METHODS else ""
        if method == "Store Credit" and not invoice.customer_id:
            request.session["flash"] = ("red", "Attach a customer to this invoice before refunding to store credit — nothing was refunded.")
            return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
        invoice.refunded = True
        # Dated, attributed and categorised, so reports can put the refund in the
        # period it happened (not the sale's) and Cash Up knows where the money went.
        invoice.refunded_at = datetime.utcnow()
        invoice.refunded_by = staff.name
        invoice.refund_method = method
        invoice.refund_reason = (refund_reason or "").strip()[:200]
        for line in invoice.lines:
            product = db.get(Product, line.product_id) if line.product_id else None
            if product:
                product.stock += line.qty
        if invoice.customer_id:
            customer = db.get(Customer, invoice.customer_id)
            if customer:
                customer.spent = round((customer.spent or 0) - invoice.total, 2)
                # v52.2: the points this sale earned come back off, and the points / store credit the customer spent on it go back to them
                # (the money refunded is only what they paid on top of those).
                earned = _returns_mod.points_for(db, invoice.total)
                customer.points = max(0, (customer.points or 0) - earned) + int(invoice.loyalty_pts_used or 0)
                if (invoice.store_credit_used or 0) > 0:
                    customer.store_credit = round((customer.store_credit or 0) + invoice.store_credit_used, 2)
                    add_audit(db, staff, "STORE_CREDIT", f"Returned ${invoice.store_credit_used:.2f} store credit used on {invoice.number} to {customer.name}")
                if method == "Store Credit":
                    customer.store_credit = round((customer.store_credit or 0) + invoice.total, 2)
                    add_audit(db, staff, "STORE_CREDIT", f"Issued ${invoice.total:.2f} store credit (refund of {invoice.number}) to {customer.name}")
        detail = f"Refunded {invoice.number} — ${invoice.total:.2f}"
        if method:
            detail += f" via {method}"
        if invoice.refund_reason:
            detail += f" — {invoice.refund_reason}"
        add_audit(db, staff, "REFUND", detail)
        db.commit()
        if method == "Cash":
            payout = cash_refund_amount(db, invoice.total)
            note = f" (${invoice.total:.2f} rounded to the nearest 5¢)" if abs(payout - invoice.total) > 0.001 else ""
            request.session["flash"] = ("green", f"Refund recorded — hand back ${payout:.2f} in cash{note}.")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/exchange-line/{line_id}")
def invoice_exchange_line(request: Request, invoice_id: str, line_id: str,
                           new_product_id: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Swaps one line item for a different product — the "exchange, not
    refund" flow shops with a no-refund policy actually need. Restocks
    the returned item, deducts the replacement from stock, and
    recalculates the invoice total, since price/tax may differ. Doesn't
    try to auto-process a second card/cash transaction for any price
    difference — that's simpler and more reliable handled at the
    register by the flash message telling staff the exact amount."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exchanges require manager or owner role.", status_code=403)

    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.refunded:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    if invoice.returns:
        request.session["flash"] = ("red", "Items from this invoice were already returned, so it can't be exchanged any more.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    new_product = db.get(Product, new_product_id)
    if not new_product:
        request.session["flash"] = ("red", "That replacement product couldn't be found.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    if new_product.stock < line.qty:
        request.session["flash"] = ("red", f"Only {new_product.stock} of {new_product.name} in stock — need {line.qty}.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    old_product = db.get(Product, line.product_id) if line.product_id else None
    if old_product:
        old_product.stock += line.qty  # returned item goes back on the shelf
    new_product.stock -= line.qty

    old_name, old_price = line.name, line.price
    line.exchange_note = (line.exchange_note + " | " if line.exchange_note else "") + \
        f"Exchanged from: {old_name} (${old_price:.2f}) on {datetime.utcnow().strftime('%b %d, %Y')}"
    line.product_id = new_product.id
    line.name = new_product.name
    line.sku = new_product.sku
    line.price = new_product.price
    line.unit_cost = new_product.cost or 0
    line.category = new_product.category

    # Recalculate the invoice total from scratch — price/tax may differ
    # between the old and new item.
    new_subtotal = round(sum(l.price * l.qty for l in invoice.lines), 2)
    # store credit spent on the sale is a payment, so tax is on the price before it (see cart_totals)
    _credit = float(invoice.store_credit_used or 0)
    taxable = max(0, new_subtotal - max(0.0, invoice.discount - _credit))
    province = get_setting(db, "province", "ON")
    tax = calc_canadian_tax(taxable, province)
    price_diff = round((new_product.price - old_price) * line.qty, 2)
    old_total = invoice.total
    invoice.subtotal = new_subtotal
    invoice.tax_breakdown = json.dumps(tax["lines"])
    invoice.tax_total = tax["tax_total"]
    invoice.total = round(max(0.0, taxable + tax["tax_total"] - _credit), 2)

    add_audit(db, staff, "EXCHANGE",
              f"{invoice.number}: exchanged {old_name} for {new_product.name} (${price_diff:+.2f} difference)")
    db.commit()

    diff_vs_old_total = round(invoice.total - old_total, 2)
    _till_note = (" Cash Up does not see this difference, so for an exact till use Return items and ring the new item up as a "
                  "separate sale instead.")
    if diff_vs_old_total > 0:
        msg = f"Exchange complete. Collect an additional ${diff_vs_old_total:.2f} from the customer." + _till_note
    elif diff_vs_old_total < 0:
        msg = f"Exchange complete. Refund ${abs(diff_vs_old_total):.2f} to the customer (cash, card, or store credit)." + _till_note
    else:
        msg = "Exchange complete. No price difference to collect or refund."
    request.session["flash"] = ("green", msg)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/email")
def invoice_email(request: Request, invoice_id: str, to_email: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    recipient = to_email or (invoice.customer.email if invoice.customer else "")
    if not recipient:
        request.session["flash"] = ("red", "No email address on file for this customer.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    ok, msg = send_email_receipt(
        db, invoice, recipient, get_setting,
        province_label=PROVINCE_LABELS.get(get_setting(db, "province", "ON"), ""),
        base_url=str(request.base_url),
    )
    if ok:
        add_audit(db, staff, "EMAIL_RECEIPT", f"Receipt emailed to {recipient} for invoice {invoice.number}")
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/sms")
def invoice_sms(request: Request, invoice_id: str, to_phone: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    recipient = to_phone or (invoice.customer.phone if invoice.customer else "")
    if not recipient:
        request.session["flash"] = ("red", "No phone number on file for this customer.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    cust = invoice.customer
    if cust is not None and cust.sms_opt_out and _messaging.normalize_phone(recipient) == _messaging.normalize_phone(cust.phone):
        request.session["flash"] = ("red", f"{cust.name} replied STOP / asked not to be texted — no receipt text was sent. Email or print it instead.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    shop_name = get_setting(db, "shop_name", "the shop")
    message = (f"Receipt from {shop_name} — Invoice {invoice.number}, total ${invoice.total:.2f}. "
               f"Thanks for your business!")
    ok, msg = send_sms(db, recipient, message, get_setting)
    if ok:
        add_audit(db, staff, "SMS_RECEIPT", f"Receipt SMS sent to {recipient} for invoice {invoice.number}")
        _log_outgoing_sms(db, recipient, message, staff.name, invoice.customer_id)
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# ── PRODUCTS ─────────────────────────────────────────────────────────
def group_products_by_variant(products):
    """Groups products sharing a variant_group together (e.g. one 'Clear
    Silicone Case' entry covering many phone models/colors), leaving
    anything without a variant_group as standalone. Shared by the
    Products page and POS so both browse the same way. Tracks each
    group's full set of categories/brands across its variants, since a
    single case type can span multiple phone brands — the POS chip
    filters need to know a group matches if ANY variant inside it does,
    not just its first item."""
    groups = {}
    ungrouped = []
    for p in products:
        if p.variant_group:
            g = groups.setdefault(p.variant_group, {
                "name": p.variant_group, "variants": [], "total_stock": 0, "low_stock": False,
                "categories": set(), "brands": set(), "phone_brands": set(), "min_price": None, "max_price": None,
            })
            g["variants"].append(p)
            g["total_stock"] += p.stock
            if p.stock <= p.reorder_threshold:
                g["low_stock"] = True
            if p.category:
                g["categories"].add(p.category)
            if p.subcategory:
                g["brands"].add(p.subcategory)
            if p.phone_brand:
                g["phone_brands"].add(p.phone_brand)
            g["min_price"] = p.price if g["min_price"] is None else min(g["min_price"], p.price)
            g["max_price"] = p.price if g["max_price"] is None else max(g["max_price"], p.price)
        else:
            ungrouped.append(p)
    return sorted(groups.values(), key=lambda g: g["name"]), ungrouped


@app.get("/products", response_class=HTMLResponse)
def products_list(request: Request, db: Session = Depends(get_db),
                   page: int = Query(1, ge=1), q: str = Query(""),
                   brand: str = Query(""), model: str = Query("")):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    flash = request.session.pop("flash", None)
    last_generated_group = request.session.pop("last_generated_group", None)

    # Low stock count, stock value, and the phone-brand picker always
    # reflect the WHOLE catalog — computed as lightweight SQL queries
    # (COUNT/SUM/DISTINCT on just the columns needed) rather than loading
    # every full Product row just to add things up or list distinct
    # values. These stay fast no matter how large the catalog gets.
    low_stock_count = db.query(Product).filter(Product.stock <= Product.reorder_threshold).count()
    stock_value = None
    if role_allowed(staff, "owner", "manager"):
        stock_value = round(db.query(func.coalesce(func.sum(Product.cost * Product.stock), 0.0)).scalar(), 2)
    total_products = db.query(Product).count()

    phone_menu = {}
    for b, m in (db.query(Product.phone_brand, Product.phone_model)
                 .filter(Product.phone_brand.isnot(None), Product.phone_brand != "",
                         Product.phone_model.isnot(None), Product.phone_model != "")
                 .distinct().all()):
        phone_menu.setdefault(b, set()).add(m)
    phone_menu = {b: sorted(models) for b, models in phone_menu.items()}
    all_phone_brands = sorted(phone_menu.keys())

    # Three ways to land on this page: search for something specific,
    # drill into one phone's cases, or just browse everything. The first
    # two are both already a narrowing/filtered view — inherently a
    # bounded result, so both run as their own real database query and
    # show every match, no pagination needed. Only "browse everything"
    # can actually be the whole catalog, so that's the one that's
    # genuinely paginated — groups need every member to build correctly
    # (a group split across two page loads would look incomplete on
    # each), so this still fetches every product to build them, then
    # paginates the RESULT, treating one group as a single browsable
    # item no matter how many variants it contains. What gets heavy as
    # the catalog grows is the browser rendering everything at once —
    # this is what actually bounds that.
    q = q.strip()
    brand = brand.strip()
    model = model.strip()
    PAGE_SIZE = 50
    total_pages = 1

    if q:
        like = f"%{q}%"
        matched = (db.query(Product)
                   .filter(or_(Product.name.ilike(like), Product.sku.ilike(like),
                               Product.subcategory.ilike(like), Product.phone_model.ilike(like),
                               Product.variant_group.ilike(like), Product.color.ilike(like),
                               Product.barcode.in_(_barcodes.variants(q) or [q])))
                   .order_by(Product.name).limit(500).all())
        product_groups, ungrouped = group_products_by_variant(matched)
        page = 1
    elif brand:
        query = db.query(Product).filter(Product.phone_brand == brand)
        if model:
            query = query.filter(Product.phone_model == model)
        matched = query.order_by(Product.name).limit(500).all()
        product_groups, ungrouped = group_products_by_variant(matched)
        page = 1
    else:
        all_products = db.query(Product).order_by(Product.name).all()
        all_groups, all_ungrouped = group_products_by_variant(all_products)
        browsable = [("group", g) for g in all_groups] + [("single", p) for p in all_ungrouped]
        total_pages = max(1, (len(browsable) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        page_slice = browsable[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        product_groups = [item for kind, item in page_slice if kind == "group"]
        ungrouped = [item for kind, item in page_slice if kind == "single"]

    return templates.TemplateResponse(request, "products.html", {
        "staff": staff, "flash": flash,
        "last_generated_group": last_generated_group,
        "category_labels": CATEGORY_LABELS, "cat_subcategories": CAT_SUBCATEGORIES,
        "low_stock_count": low_stock_count, "stock_value": stock_value,
        "total_products": total_products,
        "product_groups": product_groups, "ungrouped_products": ungrouped,
        "phone_menu": phone_menu, "all_phone_brands": all_phone_brands,
        "page": page, "total_pages": total_pages, "q": q, "brand": brand, "model": model,
    })


@app.post("/products/import")
async def products_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — bulk import requires manager or owner role.", status_code=403)

    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    # Tolerant of different header casing/spacing, and matches our own CSV export columns
    def norm(d):
        return {k.strip().lower(): v for k, v in d.items()}

    existing_skus = {p.sku for p in db.query(Product).all() if p.sku}
    # every code that already points at a product: its barcode (in every UPC/EAN spelling) and its SKU
    taken_codes = {}
    for p in db.query(Product.sku, Product.barcode).all():
        for code in _barcodes.variants(p.barcode or ""):
            taken_codes[code] = True
        if p.sku:
            taken_codes[p.sku.lower()] = True
    added, skipped, errors, barcode_skipped = 0, 0, 0, 0
    for row in reader:
        row = norm(row)
        sku = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        if not sku or not name:
            errors += 1
            continue
        if sku in existing_skus:
            skipped += 1
            continue
        try:
            price = float(row.get("price") or 0)
            cost = float(row.get("cost") or 0)
            stock = int(float(row.get("stock") or 0))
        except ValueError:
            errors += 1
            continue
        barcode = _barcodes.clean((row.get("barcode") or "").lstrip("'=\"")).strip('"')
        if barcode:
            forms = _barcodes.variants(barcode)
            damaged = bool(re.fullmatch(r"\d(\.\d+)?[eE]\+?\d+", barcode))      # a spreadsheet turned 012345678905 into 1.23457E+11
            if damaged or any(f in taken_codes for f in forms) or barcode.lower() in taken_codes:
                barcode_skipped += 1
                barcode = ""
            else:
                for f in forms:
                    taken_codes[f] = True
        db.add(Product(sku=sku, name=name, category=(row.get("category") or "").strip(),
                        subcategory=(row.get("brand") or row.get("subcategory") or "").strip(),
                        variant_group=(row.get("variant group") or row.get("variant_group") or "").strip(),
                        price=price, cost=cost, stock=stock, barcode=barcode))
        existing_skus.add(sku)
        taken_codes[sku.lower()] = True
        added += 1

    add_audit(db, staff, "PRODUCT_IMPORT", f"CSV import: {added} added, {skipped} skipped (duplicate SKU), {errors} invalid rows"
                                           + (f", {barcode_skipped} barcodes left blank (already used or damaged)" if barcode_skipped else ""))
    db.commit()
    request.session["flash"] = ("green" if errors == 0 and not barcode_skipped else "amber",
                                 f"Imported {added} new products. {skipped} skipped as duplicates. {errors} rows had errors."
                                 + (f" {barcode_skipped} barcodes were left blank because another product already has them or the spreadsheet had damaged them." if barcode_skipped else ""))
    return RedirectResponse("/products", status_code=303)


def _product_numbers_error(price, cost, stock, reorder_threshold, reorder_qty) -> str:
    """A product's price, cost and stock levels must be real, sensible numbers (see inputs.py for why)."""
    if not _inputs.money(price):
        return f"the price must be {_inputs.money_text()}"
    if not _inputs.money(cost):
        return f"the cost must be {_inputs.money_text()}"
    for label, v in (("stock", stock), ("reorder level", reorder_threshold), ("reorder quantity", reorder_qty)):
        if not _inputs.whole(v):
            return f"the {label} must be a whole number from 0 to {_inputs.QTY_MAX:,}"
    return ""


@app.post("/products/add")
def product_add(request: Request, sku: str = Form(...), name: str = Form(...),
                 category: str = Form(""), subcategory: str = Form(""), variant_group: str = Form(""),
                 price: float = Form(0), cost: float = Form(0), stock: int = Form(0),
                 reorder_threshold: int = Form(5), reorder_qty: int = Form(10), barcode: str = Form(""),
                 db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — adding products requires manager or owner role.", status_code=403)
    barcode = _barcodes.clean(barcode)
    clash = _barcodes.conflict(db, barcode) if barcode else None
    if clash:
        request.session["flash"] = ("red", f"Not added: the barcode {barcode} is already on “{clash.name}” ({clash.sku}). Use a different barcode or leave it empty.")
        return RedirectResponse("/products", status_code=303)
    sku, name = sku.strip(), name.strip()
    if not sku or not name:
        request.session["flash"] = ("red", "Not added: a product needs a SKU and a name.")
        return RedirectResponse("/products", status_code=303)
    bad_number = _product_numbers_error(price, cost, stock, reorder_threshold, reorder_qty)
    if bad_number:
        request.session["flash"] = ("red", f"Not added: {bad_number}.")
        return RedirectResponse("/products", status_code=303)
    same = db.query(Product).filter(Product.sku == sku).first()
    if same:
        request.session["flash"] = ("red", f"Not added: the SKU {sku} is already used by “{same.name}”. Use a different SKU.")
        return RedirectResponse("/products", status_code=303)
    db.add(Product(sku=sku, name=name, category=category, subcategory=subcategory, variant_group=variant_group.strip(),
                    price=price, cost=cost, stock=stock,
                    reorder_threshold=reorder_threshold, reorder_qty=reorder_qty, barcode=barcode))
    add_audit(db, staff, "PRODUCT_ADD", f"Added product: {name}")
    try:
        db.commit()
    except IntegrityError:          # two people saving the same SKU at once
        db.rollback()
        request.session["flash"] = ("red", f"Not added: the SKU {sku} is already used. Use a different SKU.")
    return RedirectResponse("/products", status_code=303)


# ── BULK VARIANT GENERATOR ──────────────────────────────────────────
# The actual pain point this solves: a case style like "Hard Ring Case"
# needs its own SKU for every phone model x color combination it comes
# in — that's easily 15 models x 6 colors = 90 near-identical rows to
# create by hand through the one-at-a-time Add Product form. This lets
# staff pick the case style once, check off which models and colors it
# comes in, and generates every SKU in one pass.

_SKU_PART_RE = re.compile(r"[^A-Z0-9]+")


def _sku_part(text: str) -> str:
    """Slugifies free text into an uppercase SKU fragment — 'iPhone 15
    Pro Max' -> 'IPHONE15PROMAX', 'Rose Gold' -> 'ROSEGOLD'."""
    return _SKU_PART_RE.sub("", text.upper())


@app.get("/products/bulk-variants", response_class=HTMLResponse)
def bulk_variants_form(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — bulk-creating products requires manager or owner role.", status_code=403)
    merged_models, merged_brands = merged_phone_models(db)
    return templates.TemplateResponse(request, "bulk_variants.html", {
        "staff": staff, "category_labels": CATEGORY_LABELS, "cat_subcategories": CAT_SUBCATEGORIES,
        "phone_brands": merged_brands, "phone_models": merged_models, "case_colors": CASE_COLORS,
        "custom_phone_models": get_custom_phone_models(db),
        "common_case_styles": COMMON_CASE_STYLES,
        "laptop_models": LAPTOP_MODELS, "console_models": CONSOLE_MODELS,
        "common_laptop_gaming_styles": COMMON_LAPTOP_GAMING_STYLES,
        "flash": request.session.pop("flash", None),
    })


@app.post("/products/bulk-variants/add-model")
def bulk_variants_add_model(request: Request, brand: str = Form(""), model: str = Form(""),
                             db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Permanently adds one model to the checklist, so a phone that's
    about to launch — or just wasn't on the original list — is a real
    checkbox from now on, not something re-typed into the free-text box
    every single time a batch of cases gets created for it."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — bulk-creating products requires manager or owner role.", status_code=403)

    brand = brand.strip()
    model = model.strip()
    if not brand or not model:
        request.session["flash"] = ("red", "Enter both a brand and a model name.")
        return RedirectResponse("/products/bulk-variants", status_code=303)

    custom = get_custom_phone_models(db)
    already_there = any(
        m.lower() == model.lower()
        for m in PHONE_MODELS.get(brand, []) + custom.get(brand, [])
    )
    if already_there:
        request.session["flash"] = ("red", f'"{model}" is already on the {brand} list.')
        return RedirectResponse("/products/bulk-variants", status_code=303)

    custom.setdefault(brand, []).append(model)
    set_setting(db, "custom_phone_models", json.dumps(custom))
    add_audit(db, staff, "PHONE_MODEL_ADD", f'Added "{model}" to the {brand} checklist')
    request.session["flash"] = ("green", f'Added "{model}" to {brand} — it\'s checkable now, including next time.')
    return RedirectResponse("/products/bulk-variants", status_code=303)


@app.post("/products/bulk-variants/remove-model")
def bulk_variants_remove_model(request: Request, brand: str = Form(""), model: str = Form(""),
                                db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Undoes add-model — only ever applies to staff-added entries. The
    built-in list is code, not data, and was never meant to be editable
    from here."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)

    custom = get_custom_phone_models(db)
    if brand in custom and model in custom[brand]:
        custom[brand] = [m for m in custom[brand] if m != model]
        if not custom[brand]:
            del custom[brand]
        set_setting(db, "custom_phone_models", json.dumps(custom))
        add_audit(db, staff, "PHONE_MODEL_REMOVE", f'Removed "{model}" from the {brand} checklist')
        request.session["flash"] = ("green", f'Removed "{model}" from {brand}.')
    return RedirectResponse("/products/bulk-variants", status_code=303)


@app.post("/products/bulk-variants/generate")
def bulk_variants_generate(
    request: Request,
    variant_group: str = Form(...),
    category: str = Form("CASE"),
    subcategory: str = Form(""),
    sku_prefix: str = Form(""),
    price: float = Form(...),
    cost: float = Form(0),
    stock: int = Form(0),
    reorder_threshold: int = Form(5),
    reorder_qty: int = Form(10),
    phone_selections: list = Form([]),  # each entry: "Brand||Model", from checked checkboxes
    extra_models: str = Form(""),  # freeform textarea, one "Brand: Model" per line — for anything not in the checklist
    colors: list = Form([]),
    extra_colors: str = Form(""),  # freeform comma-separated — for shades not in the checklist
    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect),
):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — bulk-creating products requires manager or owner role.", status_code=403)

    variant_group = variant_group.strip()

    phone_pairs = []
    for entry in phone_selections:
        if "||" in entry:
            brand, model = entry.split("||", 1)
            phone_pairs.append((brand.strip(), model.strip()))
    for line in extra_models.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            brand, model = line.split(":", 1)
            phone_pairs.append((brand.strip(), model.strip()))
        else:
            phone_pairs.append(("Other", line))

    color_list = [c.strip() for c in colors if c.strip()]
    color_list += [c.strip() for c in extra_colors.split(",") if c.strip()]

    if not variant_group or not phone_pairs or not color_list:
        request.session["flash"] = ("red", "Pick a case style, at least one phone model, and at least one color before generating.")
        return RedirectResponse("/products/bulk-variants", status_code=303)
    bad_number = _product_numbers_error(price, cost, stock, reorder_threshold, reorder_qty)
    if bad_number:
        request.session["flash"] = ("red", f"Nothing was created: {bad_number}.")
        return RedirectResponse("/products/bulk-variants", status_code=303)

    prefix = _sku_part(sku_prefix or variant_group)[:14]
    existing_skus = {p.sku for p in db.query(Product).all() if p.sku}

    created, skipped = 0, 0
    for brand, model in phone_pairs:
        for color in color_list:
            sku = f"{prefix}-{_sku_part(model)[:16]}-{_sku_part(color)[:8]}"
            if sku in existing_skus:
                # Extremely similar names (e.g. "iPhone 15" and "iPhone
                # 15 Pro" slugging to overlapping fragments) can collide
                # — disambiguate rather than silently dropping a variant.
                suffix = 2
                while f"{sku}-{suffix}" in existing_skus:
                    suffix += 1
                sku = f"{sku}-{suffix}"
            name = f"{variant_group} — {model} ({color})"
            db.add(Product(
                sku=sku, name=name, category=category, subcategory=subcategory.strip(),
                variant_group=variant_group, phone_brand=brand, phone_model=model, color=color,
                price=price, cost=cost, stock=stock,
                reorder_threshold=reorder_threshold, reorder_qty=reorder_qty,
            ))
            existing_skus.add(sku)
            created += 1

    add_audit(db, staff, "PRODUCT_BULK_ADD",
              f"Bulk-generated {created} variants of \"{variant_group}\" across {len(phone_pairs)} models x {len(color_list)} colors")
    db.commit()

    request.session["flash"] = ("green", f'Created {created} new variants of "{variant_group}".')
    request.session["last_generated_group"] = variant_group
    return RedirectResponse("/products", status_code=303)


# ── BARCODE LABEL PRINTING ──────────────────────────────────────────
@app.get("/products/labels/print", response_class=HTMLResponse)
def product_labels_print(request: Request, db: Session = Depends(get_db),
                          group: str = "", ids: str = "", copies: int = 1):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    products = []
    if group:
        products = db.query(Product).filter(Product.variant_group == group).order_by(Product.name).all()
    elif ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip()]
        # Preserve the order IDs were requested in, rather than whatever
        # order the DB happens to return them — matters when printing a
        # hand-picked set of labels one at a time.
        found = {p.id: p for p in db.query(Product).filter(Product.id.in_(id_list)).all()}
        products = [found[i] for i in id_list if i in found]

    copies = max(1, min(copies, 50))  # sane ceiling — a fat-fingered "500" shouldn't render 500 barcodes

    # Generate once per unique SKU, not once per copy — with copies=10
    # on a 20-variant batch that's the difference between 20 and 200
    # barcode generations for the exact same visual output.
    barcode_by_sku = {}
    for p in products:
        if p.sku not in barcode_by_sku:
            svg, width_mm = generate_barcode_svg(p.sku)
            barcode_by_sku[p.sku] = {"svg": svg, "width_mm": width_mm}

    return templates.TemplateResponse(request, "product_labels.html", {
        "staff": staff, "products": products, "group": group, "ids": ids, "copies": copies,
        "shop": get_shop_info(db), "barcode_by_sku": barcode_by_sku,
    })


@app.get("/products/reorder", response_class=HTMLResponse)
def reorder_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — the reorder list requires manager or owner role.", status_code=403)
    low_stock = (db.query(Product)
                 .filter(Product.stock <= Product.reorder_threshold)
                 .order_by(Product.stock).all())
    total_cost = round(sum(p.cost * p.reorder_qty for p in low_stock), 2)
    return templates.TemplateResponse(request, "reorder_list.html", {
        "staff": staff, "products": low_stock, "total_cost": total_cost,
    })


@app.get("/products/reorder/print", response_class=HTMLResponse)
def reorder_list_print(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — the reorder list requires manager or owner role.", status_code=403)
    low_stock = (db.query(Product)
                 .filter(Product.stock <= Product.reorder_threshold)
                 .order_by(Product.category, Product.name).all())
    total_cost = round(sum(p.cost * p.reorder_qty for p in low_stock), 2)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    return templates.TemplateResponse(request, "reorder_print.html", {
        "staff": staff, "products": low_stock, "total_cost": total_cost, "shop_name": shop_name,
    })


@app.get("/export/csv/reorder")
def export_reorder_csv(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).order_by(Product.name).all()
    rows = [(p.sku, p.name, p.category, p.subcategory, p.stock, p.reorder_threshold, p.reorder_qty, round(p.cost * p.reorder_qty, 2)) for p in low_stock]
    return _csv_response(rows, ["SKU", "Name", "Category", "Brand", "Current Stock", "Reorder At", "Suggested Qty", "Est. Cost"], "reorder_list.csv")


# ── SUPPLIERS ────────────────────────────────────────────────────────────
@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — suppliers require manager or owner role.", status_code=403)
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse(request, "suppliers.html", {"staff": staff, "suppliers": suppliers})


@app.post("/suppliers/add")
def supplier_add(request: Request, name: str = Form(...), contact_name: str = Form(""),
                  email: str = Form(""), phone: str = Form(""), lead_time_days: int = Form(7),
                  notes: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    if not _inputs.whole(lead_time_days, 0, 365):
        request.session["flash"] = ("red", "Supplier not added: lead time must be 0 to 365 days.")
        return RedirectResponse("/suppliers", status_code=303)
    db.add(Supplier(name=name, contact_name=contact_name, email=email, phone=phone,
                     lead_time_days=lead_time_days, notes=notes, active=True))
    add_audit(db, staff, "SUPPLIER_ADD", f"Added supplier: {name}")
    db.commit()
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/edit")
def supplier_edit(request: Request, supplier_id: str, name: str = Form(...), contact_name: str = Form(""),
                   email: str = Form(""), phone: str = Form(""), lead_time_days: int = Form(7),
                   notes: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    if not _inputs.whole(lead_time_days, 0, 365):
        request.session["flash"] = ("red", "Supplier not saved: lead time must be 0 to 365 days.")
        return RedirectResponse("/suppliers", status_code=303)
    supplier = db.get(Supplier, supplier_id)
    if supplier:
        supplier.name, supplier.contact_name = name, contact_name
        supplier.email, supplier.phone = email, phone
        supplier.lead_time_days, supplier.notes = lead_time_days, notes
        add_audit(db, staff, "SUPPLIER_EDIT", f"Edited supplier: {name}")
        db.commit()
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/toggle")
def supplier_toggle(request: Request, supplier_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    supplier = db.get(Supplier, supplier_id)
    if supplier:
        supplier.active = not supplier.active
        add_audit(db, staff, "SUPPLIER_TOGGLE", f"{'Activated' if supplier.active else 'Deactivated'} supplier: {supplier.name}")
        db.commit()
    return RedirectResponse("/suppliers", status_code=303)


# ── PURCHASE ORDERS ──────────────────────────────────────────────────────
def _next_po_number(db: Session) -> str:
    count = db.query(PurchaseOrder).count()
    return f"PO-{count + 1:04d}"


@app.get("/purchase-orders", response_class=HTMLResponse)
def po_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — purchase orders require manager or owner role.", status_code=403)
    pos = db.query(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).all()
    po_totals = {po.id: round(sum(l.qty * l.unit_cost for l in po.lines), 2) for po in pos}
    return templates.TemplateResponse(request, "purchase_orders.html", {"staff": staff, "pos": pos, "po_totals": po_totals})


@app.get("/purchase-orders/new", response_class=HTMLResponse)
def po_new(request: Request, from_reorder: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — purchase orders require manager or owner role.", status_code=403)
    suppliers = db.query(Supplier).filter(Supplier.active == True).order_by(Supplier.name).all()  # noqa: E712
    prefill = []
    if from_reorder:
        low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).order_by(Product.name).all()
        prefill = [{"id": p.id, "sku": p.sku, "name": p.name, "qty": p.reorder_qty, "cost": p.cost} for p in low_stock]
    all_products = db.query(Product).order_by(Product.name).all()
    return templates.TemplateResponse(request, "po_new.html", {
        "staff": staff, "suppliers": suppliers, "prefill": prefill, "all_products": all_products,
    })


@app.post("/purchase-orders/add")
async def po_add(request: Request, supplier_id: str = Form(""), notes: str = Form(""),
                  db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    form = await request.form()
    product_ids = form.getlist("product_id")
    qtys = form.getlist("qty")
    costs = form.getlist("unit_cost")

    po = PurchaseOrder(number=_next_po_number(db), supplier_id=supplier_id or None,
                        status="draft", created_by_name=staff.name, notes=notes)
    db.add(po)
    db.flush()

    line_count = 0
    for pid, qty_raw, cost_raw in zip(product_ids, qtys, costs):
        if not pid or not qty_raw:
            continue
        try:
            qty, cost = int(qty_raw), float(cost_raw or 0)
        except ValueError:
            continue
        if qty <= 0 or qty > _inputs.QTY_MAX or not _inputs.money(cost):
            continue
        product = db.get(Product, pid)
        if not product:
            continue
        db.add(PurchaseOrderLine(po_id=po.id, product_id=product.id, name=product.name,
                                  sku=product.sku, qty=qty, unit_cost=cost))
        line_count += 1

    if line_count == 0:
        db.rollback()
        request.session["flash"] = ("red", "No valid line items — purchase order was not created.")
        return RedirectResponse("/purchase-orders/new", status_code=303)

    add_audit(db, staff, "PO_CREATE", f"Created {po.number} with {line_count} line item(s)")
    db.commit()
    return RedirectResponse(f"/purchase-orders/{po.id}", status_code=303)


@app.get("/purchase-orders/{po_id}", response_class=HTMLResponse)
def po_detail(request: Request, po_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    po = db.get(PurchaseOrder, po_id)
    if not po:
        return RedirectResponse("/purchase-orders", status_code=303)
    total_cost = round(sum(l.qty * l.unit_cost for l in po.lines), 2)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "po_detail.html", {
        "staff": staff, "po": po, "total_cost": total_cost, "flash": flash,
    })


@app.post("/purchase-orders/{po_id}/send")
def po_send(request: Request, po_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    po = db.get(PurchaseOrder, po_id)
    if po and po.status == "draft":
        po.status = "sent"
        po.sent_at = datetime.utcnow()
        add_audit(db, staff, "PO_SEND", f"Marked {po.number} as sent")

        email_note = ""
        if po.supplier and po.supplier.email:
            lines_text = "\n".join(f"{l.sku} — {l.name} x{l.qty} @ ${l.unit_cost:.2f}" for l in po.lines)
            body = (f"Purchase Order {po.number}\n\n{lines_text}\n\n"
                    f"Total: ${sum(l.qty * l.unit_cost for l in po.lines):.2f}\n\n{po.notes}")
            ok, detail = send_plain_email(db, po.supplier.email, f"Purchase Order {po.number}", body, get_setting)
            email_note = f" Email to supplier: {detail}" if ok else f" Email failed: {detail}"
        db.commit()
        request.session["flash"] = ("green", f"{po.number} marked as sent.{email_note}")
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@app.post("/purchase-orders/{po_id}/cancel")
def po_cancel(request: Request, po_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """An order made by mistake (or one the supplier can't fill) can be cancelled. Stock is untouched: nothing had arrived."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    po = db.get(PurchaseOrder, po_id)
    if po and po.status in ("draft", "sent"):
        po.status = "cancelled"
        add_audit(db, staff, "PO_CANCEL", f"Cancelled {po.number}")
        db.commit()
        request.session["flash"] = ("green", f"{po.number} cancelled. No stock was changed.")
    elif po:
        request.session["flash"] = ("red", f"{po.number} is {po.status}, so it can't be cancelled.")
    return RedirectResponse(f"/purchase-orders/{po_id}" if po else "/purchase-orders", status_code=303)


@app.post("/purchase-orders/{po_id}/receive")
async def po_receive(request: Request, po_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    po = db.get(PurchaseOrder, po_id)
    if not po or po.status not in ("draft", "sent"):      # a received or cancelled order can't add stock again
        return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)

    form = await request.form()
    received_in = 0
    for line in po.lines:
        field = f"received_{line.id}"
        raw = form.get(field, "")
        try:
            received_qty = int(raw) if raw else line.qty  # default: assume full qty received if left blank
        except ValueError:
            received_qty = 0
        received_qty = max(0, min(received_qty, line.qty))
        line.received_qty = received_qty
        if line.product_id and received_qty > 0:
            product = db.get(Product, line.product_id)
            if product:
                product.stock += received_qty
                received_in += received_qty

    po.status = "received"
    po.received_at = datetime.utcnow()
    add_audit(db, staff, "PO_RECEIVE", f"Received {po.number} — {received_in} unit(s) added to stock")
    db.commit()
    request.session["flash"] = ("green", f"Stock updated — {received_in} unit(s) received into inventory.")
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_page(request: Request, product_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — editing products requires manager or owner role.", status_code=403)
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=303)
    return templates.TemplateResponse(request, "product_edit.html", {
        "staff": staff, "product": product, "flash": request.session.pop("flash", None),
        "category_labels": CATEGORY_LABELS, "cat_subcategories": CAT_SUBCATEGORIES,
    })


@app.post("/products/{product_id}/edit")
def product_edit(request: Request, product_id: str, name: str = Form(...), sku: str = Form(...),
                  category: str = Form(""), subcategory: str = Form(""), variant_group: str = Form(""),
                  price: float = Form(0), cost: float = Form(0), stock: int = Form(0),
                  reorder_threshold: int = Form(5), reorder_qty: int = Form(10), barcode: str | None = Form(None),
                  has_barcode: str = Form(""),
                  db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — editing products requires manager or owner role.", status_code=403)
    product = db.get(Product, product_id)
    if product:
        bad_number = _product_numbers_error(price, cost, stock, reorder_threshold, reorder_qty)
        if bad_number:
            request.session["flash"] = ("red", f"Not saved: {bad_number}.")
            return RedirectResponse(f"/products/{product_id}/edit", status_code=303)
        # The edit form sends has_barcode=1 with the field. FastAPI reads an empty form field as "not sent", so that
        # marker is what tells "clear the barcode" apart from "an old form that never had the field" (left alone).
        if barcode is not None or has_barcode == "1":
            barcode = _barcodes.clean(barcode or "")
            clash = _barcodes.conflict(db, barcode, product.id) if barcode else None
            if clash:
                request.session["flash"] = ("red", f"Not saved: the barcode {barcode} is already on “{clash.name}” ({clash.sku}).")
                return RedirectResponse(f"/products/{product_id}/edit", status_code=303)
            product.barcode = barcode
        sku, name = sku.strip(), name.strip()
        same = db.query(Product).filter(Product.sku == sku, Product.id != product.id).first() if sku else None
        if not sku or not name or same:
            msg = (f"Not saved: the SKU {sku} is already used by “{same.name}”." if same else "Not saved: a product needs a SKU and a name.")
            request.session["flash"] = ("red", msg)
            return RedirectResponse(f"/products/{product_id}/edit", status_code=303)
        product.name, product.sku = name, sku
        product.category, product.subcategory, product.variant_group = category, subcategory, variant_group.strip()
        product.price, product.cost, product.stock = price, cost, stock
        product.reorder_threshold, product.reorder_qty = reorder_threshold, reorder_qty
        add_audit(db, staff, "PRODUCT_EDIT", f"Edited product: {name}")
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            request.session["flash"] = ("red", f"Not saved: the SKU {sku} is already used.")
            return RedirectResponse(f"/products/{product_id}/edit", status_code=303)
    return RedirectResponse("/products", status_code=303)


@app.post("/products/{product_id}/delete")
def product_delete(request: Request, product_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    product = db.get(Product, product_id)
    if product:
        add_audit(db, staff, "PRODUCT_DELETE", f"Deleted product: {product.name}")
        db.delete(product)
        db.commit()
    return RedirectResponse("/products", status_code=303)


# ── CUSTOMERS ────────────────────────────────────────────────────────
@app.get("/customers", response_class=HTMLResponse)
def customers_list(request: Request, db: Session = Depends(get_db),
                    page: int = Query(1, ge=1), q: str = Query("")):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    # Two different problems, two different fixes. Store credit/points
    # outstanding and the total count must always reflect every customer
    # that has ever existed — computed with SQL aggregates (COUNT/SUM),
    # which stay fast at any table size, rather than loading every row
    # into Python just to add them up. The actual TABLE below is what
    # gets genuinely heavy as the shop's customer base grows — a browser
    # rendering thousands of rows is the real cost, not the query — so
    # that's what's paginated. A search overrides pagination entirely and
    # checks every customer, not just the current page, since narrowing
    # results to "whatever happens to be on this page" would silently
    # hide the person you're actually looking for.
    total_customers = db.query(Customer).count()
    total_store_credit = round(db.query(func.coalesce(func.sum(Customer.store_credit), 0.0)).scalar(), 2)
    total_points = int(db.query(func.coalesce(func.sum(Customer.points), 0)).scalar())

    q = q.strip()
    PAGE_SIZE = 50
    if q:
        like = f"%{q}%"
        _conds = [Customer.name.ilike(like), Customer.phone.ilike(like), Customer.email.ilike(like)]
        _pc = phone_search_clause(q)
        if _pc is not None:
            _conds.append(_pc)
        customers = (db.query(Customer)
                     .filter(or_(*_conds))
                     .order_by(Customer.name).limit(500).all())
        total_pages = 1
        page = 1
    else:
        total_pages = max(1, (total_customers + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        customers = (db.query(Customer).order_by(Customer.name)
                     .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())

    return templates.TemplateResponse(request, "customers.html", {
        "flash": request.session.pop("flash", None),
        "staff": staff, "customers": customers,
        "total_store_credit": total_store_credit, "total_points": total_points,
        "total_customers": total_customers, "page": page, "total_pages": total_pages, "q": q,
        "page_size": PAGE_SIZE,
    })


def _customer_input_error(name: str, phone: str, email: str) -> str:
    """A customer needs a real name; the other boxes just can't be absurdly long. Empty string = fine."""
    if not (name or "").strip():
        return "Enter the customer's name."
    if len(name.strip()) > 120 or len(phone or "") > 40 or len(email or "") > 160:
        return "That name, phone or email is too long."
    return ""


@app.post("/customers/add")
def customer_add(request: Request, name: str = Form(...), phone: str = Form(""),
                  email: str = Form(""), notes: str = Form(""),
                  marketing_consent: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    _err = _customer_input_error(name, phone, email)
    if _err:
        request.session["flash"] = ("red", f"Customer not added: {_err}")
        return RedirectResponse("/customers", status_code=303)
    name, phone, email = name.strip(), phone.strip(), email.strip()
    consented = marketing_consent == "on"
    db.add(Customer(name=name, phone=phone, email=email, notes=notes,
                     marketing_consent=consented, consent_date=datetime.utcnow() if consented else None))
    add_audit(db, staff, "CUSTOMER_ADD", f"Added customer: {name}")
    db.commit()
    return RedirectResponse("/customers", status_code=303)


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(request: Request, customer_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers", status_code=303)
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer_id).order_by(Invoice.date.desc()).all()
    repairs = db.query(Repair).filter(Repair.customer_id == customer_id).order_by(Repair.created_at.desc()).all()
    repair_warranties = {r.id: get_warranty_status(r) for r in repairs}
    sms_thread = db.query(SmsMessage).filter(SmsMessage.customer_id == customer_id).order_by(SmsMessage.created_at).all()
    trade_ins = db.query(TradeIn).filter(TradeIn.customer_id == customer_id).order_by(TradeIn.created_at.desc()).all()
    layaways = db.query(Layaway).filter(Layaway.customer_id == customer_id).order_by(Layaway.created_at.desc()).all()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "customer_detail.html", {
        "staff": staff, "customer": customer, "invoices": invoices,
        "repairs": repairs, "repair_warranties": repair_warranties,
        "status_labels": STATUS_LABELS, "status_badge": STATUS_BADGE,
        "sms_thread": sms_thread, "flash": flash,
        "trade_ins": trade_ins, "layaways": layaways,
    })


@app.post("/customers/{customer_id}/sms")
def customer_send_sms(request: Request, customer_id: str, message: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    if not customer or not customer.phone:
        request.session["flash"] = ("red", "No phone number on file for this customer.")
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)

    if customer.sms_opt_out:
        request.session["flash"] = ("red", f"{customer.name} replied STOP / asked not to be texted — no message was sent.")
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    ok, msg = send_sms(db, customer.phone, message, get_setting)
    if ok:
        _log_outgoing_sms(db, customer.phone, message, staff.name, customer.id)
        add_audit(db, staff, "SMS_SENT", f"SMS sent to {customer.name}")
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/edit")
def customer_edit(request: Request, customer_id: str, name: str = Form(...),
                   phone: str = Form(""), email: str = Form(""),
                   marketing_consent: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    _err = _customer_input_error(name, phone, email)
    if customer and _err:
        request.session["flash"] = ("red", f"Not saved: {_err}")
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    if customer:
        name, phone, email = name.strip(), phone.strip(), email.strip()
        customer.name, customer.phone, customer.email = name, phone, email
        newly_consented = marketing_consent == "on"
        if newly_consented and not customer.marketing_consent:
            customer.consent_date = datetime.utcnow()  # only stamp the date when consent is newly given
        customer.marketing_consent = newly_consented
        add_audit(db, staff, "CUSTOMER_EDIT", f"Edited customer: {name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/notes")
def customer_notes(request: Request, customer_id: str, notes: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    if customer:
        customer.notes = notes
        add_audit(db, staff, "CUSTOMER_EDIT", f"Updated notes for {customer.name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/credit")
def customer_credit(request: Request, customer_id: str, amount: float = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — issuing store credit requires manager or owner role.", status_code=403)
    customer = db.get(Customer, customer_id)
    if customer and not _inputs.money(amount, lo=0.01):
        request.session["flash"] = ("red", f"Store credit was not issued: enter an amount from $0.01 to ${_inputs.MONEY_MAX:,.0f}.")
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)
    if customer:
        customer.store_credit = round((customer.store_credit or 0) + amount, 2)
        add_audit(db, staff, "STORE_CREDIT", f"Issued ${amount:.2f} store credit to {customer.name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


# ── TRADE-INS ────────────────────────────────────────────────────────
@app.get("/trade-ins", response_class=HTMLResponse)
def trade_ins_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    trade_ins = db.query(TradeIn).order_by(TradeIn.created_at.desc()).all()
    total_paid_out = round(sum(t.offered_amount for t in trade_ins), 2)
    customers = db.query(Customer).order_by(Customer.name).all()
    return templates.TemplateResponse(request, "trade_ins.html", {
        "staff": staff, "trade_ins": trade_ins, "total_paid_out": total_paid_out,
        "customers": customers, "flash": request.session.pop("flash", None),
        "last_trade_in_id": request.session.pop("last_trade_in_id", None),
        "device_catalog": _devices.payload(get_custom_phone_models(db)),
    })


@app.post("/trade-ins/add")
def trade_in_add(request: Request, customer_id: str = Form(""), new_customer_name: str = Form(""),
                  new_customer_phone: str = Form(""), device: str = Form(...), imei: str = Form(""),
                  condition: str = Form(""), offered_amount: float = Form(...),
                  payout_method: str = Form("store_credit"), notes: str = Form(""),
                  db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager", "cashier"):
        return HTMLResponse("Forbidden.", status_code=403)

    # The browser form already limits the amount, but this is money leaving the till (or credit being issued), so the
    # server checks it too: a negative "payout" would otherwise count as cash coming IN to the drawer.
    if not device.strip():
        request.session["flash"] = ("red", "Trade-in not recorded: enter the device being traded in.")
        return RedirectResponse("/trade-ins", status_code=303)
    if not math.isfinite(offered_amount) or round(offered_amount, 2) < 0 or offered_amount > TRADE_IN_MAX:
        request.session["flash"] = ("red", f"Trade-in not recorded: the offered amount must be between $0 and ${TRADE_IN_MAX:,.0f}.")
        return RedirectResponse("/trade-ins", status_code=303)
    if payout_method not in ("store_credit", "cash"):
        payout_method = "store_credit"

    customer = db.get(Customer, customer_id) if customer_id else None
    if not customer and new_customer_name.strip():
        customer = find_customer_by_phone(db, new_customer_phone) if new_customer_phone.strip() else None
        if customer is None:
            customer = Customer(name=new_customer_name.strip(), phone=new_customer_phone.strip())
            db.add(customer)
            db.flush()

    imei = _imei.clean(imei)
    trade_in = TradeIn(
        number=next_trade_in_number(db),
        customer_id=customer.id if customer else None,
        device=device.strip(), imei=imei, condition=condition.strip(),
        offered_amount=round(offered_amount, 2), payout_method=payout_method,
        notes=notes.strip(), staff_id=staff.id, staff_name=staff.name,
    )
    db.add(trade_in)

    if customer and payout_method == "store_credit" and offered_amount > 0:
        customer.store_credit = round((customer.store_credit or 0) + offered_amount, 2)
        add_audit(db, staff, "STORE_CREDIT", f"Issued ${offered_amount:.2f} store credit for trade-in ({device}) — {customer.name}")

    add_audit(db, staff, "TRADE_IN", f"Accepted trade-in: {device}" + (f" (IMEI {imei})" if imei else "") + f" — ${offered_amount:.2f} via {payout_method.replace('_', ' ')}")
    db.commit()

    request.session["last_trade_in_id"] = trade_in.id
    if payout_method == "cash":
        message = f"Trade-in recorded. Pay ${offered_amount:.2f} cash out from the till."
    else:
        message = f"Trade-in recorded — ${offered_amount:.2f} store credit issued{' to ' + customer.name if customer else ''}."
    imei_warn = _imei.warning(imei)
    request.session["flash"] = ("amber", f"{message} {imei_warn}") if imei_warn else ("green", message)
    return RedirectResponse("/trade-ins", status_code=303)


@app.post("/trade-ins/{trade_in_id}/status")
def trade_in_status(request: Request, trade_in_id: str, status: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    trade_in = db.get(TradeIn, trade_in_id)
    if trade_in and status in ("accepted", "resold", "scrapped"):
        trade_in.status = status
        add_audit(db, staff, "TRADE_IN_EDIT", f"{trade_in.device} marked {status}")
        db.commit()
    return RedirectResponse("/trade-ins", status_code=303)


@app.get("/trade-ins/{trade_in_id}/print", response_class=HTMLResponse)
def trade_in_print(request: Request, trade_in_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    trade_in = db.get(TradeIn, trade_in_id)
    if not trade_in:
        return RedirectResponse("/trade-ins", status_code=303)
    return templates.TemplateResponse(request, "trade_in_receipt.html", {
        "staff": staff, "trade_in": trade_in, "shop": get_shop_info(db),
    })


# ── REPAIRS ──────────────────────────────────────────────────────────
@app.get("/repairs", response_class=HTMLResponse)
def repairs_list(request: Request, view: str = "kanban", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repairs = db.query(Repair).order_by(Repair.created_at.desc()).all()
    columns = {s: [] for s in STATUS_ORDER}
    for r in repairs:
        columns.setdefault(r.status, []).append(r)
    technicians = db.query(Staff).filter(Staff.active == True).all()  # noqa: E712
    customers = db.query(Customer).order_by(Customer.name).all()
    return templates.TemplateResponse(request, "repairs.html", {
        "flash": request.session.pop("flash", None),
        "staff": staff, "repairs": repairs, "columns": columns, "view": view,
        "status_labels": STATUS_LABELS, "status_order": STATUS_ORDER, "status_badge": STATUS_BADGE,
        "issue_types": list(ISSUE_TYPES) + _repair_prices.repair_names(db), "technicians": technicians, "customers": customers,
        "menu_lines": _repair_prices.count_lines(db), "can_edit_prices": role_allowed(staff, "owner"), "can_see_prices": role_allowed(staff, "owner", "manager"),
        "today": local_today(get_shop_tz(db)).isoformat(), **_intake_form_context(), "quote_flags": _quotes.badge_map(db),
        "device_catalog": _devices.payload(get_custom_phone_models(db)),
    })


@app.post("/repairs/add")
def repair_add(request: Request, phone: str = Form(...), name: str = Form(...),
                device: str = Form(...), imei: str = Form(""), issue: str = Form(...), description: str = Form(""),
                estimated_cost: str = Form(""), warranty_days: int = Form(90),
                promised_by: str = Form(""), technician_id: str = Form(""),
                checkin: dict = Depends(_intake_fields),
                db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    if not name.strip() or not phone.strip() or not device.strip():
        request.session["flash"] = ("red", "Ticket not created: enter the customer's name, their phone number and the device.")
        return RedirectResponse("/repairs", status_code=303)
    cost_val, _cost_err = _inputs.parse_money(estimated_cost)
    if _cost_err or not _inputs.whole(warranty_days, 0, 3650):
        request.session["flash"] = ("red", "Ticket not created: " + (f"the estimate {_cost_err}" if _cost_err else "warranty must be 0 to 3650 days") + ".")
        return RedirectResponse("/repairs", status_code=303)

    customer = find_customer_by_phone(db, phone)
    if not customer:
        customer = Customer(name=name, phone=phone)
        db.add(customer)
        db.flush()

    last = db.query(Repair).order_by(Repair.ticket_no.desc()).first()
    next_ticket = (last.ticket_no + 1) if last else 1001

    imei = _imei.clean(imei)
    history = [{"status": "RECEIVED", "note": "Ticket created", "date": datetime.utcnow().isoformat()}]

    repair = Repair(
        ticket_no=next_ticket, customer_id=customer.id, device=device, imei=imei, issue=issue,
        description=description, status="RECEIVED", estimated_cost=cost_val,
        warranty_days=warranty_days, promised_by=promised_by,
        technician_id=technician_id or None, status_history=json.dumps(history),
    )
    checklist = _intake.normalise(checkin, staff.name)
    if checklist:
        repair.intake_json = json.dumps(checklist, separators=(",", ":"))
    db.add(repair)
    add_audit(db, staff, "REPAIR_CREATE", f"Ticket #{next_ticket} — {device} ({issue})" + (f" — IMEI {imei.strip()}" if imei.strip() else "")
              + (" — check-in recorded" if checklist else ""))
    db.commit()
    imei_warn = _imei.warning(imei)
    if imei_warn:
        request.session["flash"] = ("amber", f"Ticket #{next_ticket} created. {imei_warn} Fix it in the IMEI box below if needed, then take photos of the device.")
    else:
        request.session["flash"] = ("green", f"Ticket #{next_ticket} created. Take photos of the device now, while the customer is here.")
    return RedirectResponse(f"/repairs/{repair.id}#photos", status_code=303)


@app.post("/repairs/{repair_id}/imei")
def repair_imei(request: Request, repair_id: str, imei: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if repair:
        imei = _imei.clean(imei)
        repair.imei = imei
        add_audit(db, staff, "REPAIR_EDIT", f"#{repair.ticket_no} — IMEI/serial set to {imei or '(cleared)'}")
        db.commit()
        warn = _imei.warning(imei)
        request.session["flash"] = ("amber", f"Saved. {warn}") if warn else ("green", "IMEI / serial saved.")
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


def _public_origin(request: Request) -> str:
    """https://shop-address — the address a customer would open. Behind Railway the app sees plain http, so a
    hosted deployment always says https."""
    host = request.headers.get("host") or request.url.netloc
    return f"{'https' if _IS_HOSTED else request.url.scheme}://{host}"


@app.get("/repairs/{repair_id}", response_class=HTMLResponse)
def repair_detail(request: Request, repair_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if not repair:
        return RedirectResponse("/repairs", status_code=303)
    history = json.loads(repair.status_history) if repair.status_history else []
    n_status = next_status(repair.status)
    cur_idx = STATUS_ORDER.index(repair.status) if repair.status in STATUS_ORDER else 0
    technicians = db.query(Staff).filter(Staff.active == True).all()  # noqa: E712
    flash = request.session.pop("flash", None)
    linked_invoices = db.query(Invoice).filter(Invoice.repair_id == repair_id).order_by(Invoice.date.desc()).all()

    parts = db.query(RepairPart).filter(RepairPart.repair_id == repair_id).order_by(RepairPart.added_at).all()
    parts_cost = round(sum(p.qty * p.unit_cost for p in parts), 2)
    margin = None
    if role_allowed(staff, "owner", "manager"):
        charge = repair.final_cost if repair.final_cost is not None else (repair.estimated_cost or 0)
        margin = round(charge - parts_cost, 2)
    all_products = db.query(Product).order_by(Product.name).all() if role_allowed(staff, "owner", "manager", "technician") else []
    # v53.1: the Add Part picker lists repair parts only (screens, batteries, parts), the ones that fit this phone first.
    # ?parts=all brings back the full catalogue for the odd job that needs a charger or a case.
    show_all_parts = request.query_params.get("parts") == "all"
    part_picker = _repair_parts.build_picker(all_products, repair.device, show_all_parts)
    # v54: the repair price menu line for this phone + issue (if the owner has one) and the part it normally uses.
    menu_line = menu_part = None
    if all_products and repair.status not in ("COMPLETED", "COLLECTED"):
        try:
            menu_line = _repair_prices.lookup(db, repair.device, repair.issue)
            menu_part = _repair_prices.part_info(db, menu_line.product_id) if menu_line else None
        except Exception:                      # the price menu must never stop a ticket from opening
            menu_line = menu_part = None
    menu_part_added = bool(menu_part and any(p.product_id == menu_part["id"] for p in parts))
    warranty = get_warranty_status(repair)
    status_link = f"{_public_origin(request)}/s/{_sec.ensure_status_token(db, repair)}"
    quote_ctx = _quote_detail_context(db, repair, _public_origin(request))
    if quote_ctx["quote"] is not None and quote_ctx["quote"].responded_via == "link" and quote_ctx["quote"].seen_at is None:
        _quotes.mark_seen(db, repair.id)   # opening the ticket is "seeing" the answer; clears the bell
    db.commit()                            # also saves any quote that just passed its expiry date

    return templates.TemplateResponse(request, "repair_detail.html", {
        **quote_ctx,
        "status_link": status_link,
        "staff": staff, "repair": repair, "history": list(reversed(history)),
        "next_status": n_status, "cur_idx": cur_idx,
        "today": local_today(get_shop_tz(db)).isoformat(),   # v53: for the "promised … · late" flag in the ticket header
        "status_labels": STATUS_LABELS, "status_order": STATUS_ORDER, "status_badge": STATUS_BADGE,
        "technicians": technicians, "flash": flash, "linked_invoices": linked_invoices, "warranty": warranty,
        "parts": parts, "parts_cost": parts_cost, "margin": margin, "all_products": all_products,
        "part_picker": part_picker, "show_all_parts": show_all_parts,
        "menu_line": menu_line, "menu_part": menu_part, "menu_part_added": menu_part_added,
        "auto_msgs": _messaging.repair_activity(db, repair_id), "msg_on": _messaging.load_config(db).enabled,
        **_intake_detail_context(db, repair, staff),
    })


@app.post("/repairs/{repair_id}/parts/add")
def repair_part_add(request: Request, repair_id: str, product_id: str = Form(...),
                     qty: int = Form(1), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager", "technician"):
        return HTMLResponse("Forbidden — adding parts requires technician, manager, or owner role.", status_code=403)
    repair = db.get(Repair, repair_id)
    product = db.get(Product, product_id)
    if not repair or not product:
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)
    if qty <= 0:
        request.session["flash"] = ("red", "Quantity must be at least 1.")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)
    if product.stock < qty:
        request.session["flash"] = ("red", f"Only {product.stock} of {product.name} in stock — can't use {qty}.")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)

    db.add(RepairPart(repair_id=repair_id, product_id=product.id, name=product.name,
                       qty=qty, unit_cost=product.cost))
    product.stock -= qty  # deducted immediately, same as a POS sale — parts don't silently vanish from counts
    add_audit(db, staff, "REPAIR_PART_ADD", f"Used {qty}x {product.name} on ticket #{repair.ticket_no}")
    db.commit()
    request.session["flash"] = ("green", f"Added {qty}x {product.name} — stock updated.")
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/parts/{part_id}/remove")
def repair_part_remove(request: Request, repair_id: str, part_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager", "technician"):
        return HTMLResponse("Forbidden", status_code=403)
    part = db.get(RepairPart, part_id)
    if part and part.repair_id == repair_id:
        if part.product_id:
            product = db.get(Product, part.product_id)
            if product:
                product.stock += part.qty  # restore stock — this part wasn't actually used after all
        add_audit(db, staff, "REPAIR_PART_REMOVE", f"Removed {part.qty}x {part.name} from ticket, stock restored")
        db.delete(part)
        db.commit()
        request.session["flash"] = ("green", "Part removed and stock restored.")
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/advance")
def repair_advance(request: Request, repair_id: str, note: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if repair:
        n = next_status(repair.status)
        if n:
            old = repair.status
            repair.status = n
            repair.updated_at = datetime.utcnow()
            history = json.loads(repair.status_history) if repair.status_history else []
            history.append({"status": n, "note": note or f"Moved from {STATUS_LABELS.get(old, old)}", "date": datetime.utcnow().isoformat()})
            repair.status_history = json.dumps(history)
            add_audit(db, staff, "REPAIR_STATUS", f"#{repair.ticket_no} → {STATUS_LABELS.get(n, n)}")
            db.commit()
            if n == "READY":
                _messaging_hook(_messaging.on_repair_ready, db, repair)
            elif n == "COLLECTED":
                _messaging_hook(_messaging.on_repair_collected, db, repair)
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/notify")
def repair_notify(request: Request, repair_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if not repair or not repair.customer or not repair.customer.phone:
        request.session["flash"] = ("red", "No customer phone number on file for this ticket.")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)

    if repair.customer.sms_opt_out:
        request.session["flash"] = ("red", f"{repair.customer.name} replied STOP / asked not to be texted — please call or tell them in person.")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)
    shop_name = get_setting(db, "shop_name", "the shop")
    message = (f"Hi {repair.customer.name}, great news! Your {repair.device} repair is "
               f"complete and ready for pickup at {shop_name}. Ticket #{repair.ticket_no}. See you soon!")
    ok, msg = send_sms(db, repair.customer.phone, message, get_setting)
    if ok:
        add_audit(db, staff, "REPAIR_NOTIFY", f"Ready-for-pickup SMS sent for ticket #{repair.ticket_no}")
        _log_outgoing_sms(db, repair.customer.phone, message, staff.name, repair.customer_id)
        _messaging.record_manual_ready(db, repair, repair.customer.phone, message, staff.name)
        db.commit()
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/cost")
def repair_cost(request: Request, repair_id: str, estimated_cost: str = Form(""),
                 final_cost: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    est, est_err = _inputs.parse_money(estimated_cost)
    fin, fin_err = _inputs.parse_money(final_cost)
    if repair and (est_err or fin_err):
        request.session["flash"] = ("red", "Costs not saved: the " + (f"estimate {est_err}" if est_err else f"final cost {fin_err}") + ".")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)
    if repair:
        repair.estimated_cost = est if est is not None else repair.estimated_cost
        repair.final_cost = fin if fin is not None else repair.final_cost
        repair.updated_at = datetime.utcnow()
        add_audit(db, staff, "REPAIR_EDIT", f"Updated costs for ticket #{repair.ticket_no}")
        db.commit()
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/charge")
def repair_charge(request: Request, repair_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Send a repair ticket to the POS register to collect payment. Clears
    whatever's currently in the cart, adds a single line item for this
    repair (using the final cost if set, otherwise the estimate), attaches
    the customer, and remembers the repair via session so /pos/checkout
    can link the resulting invoice back to this ticket."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if not repair:
        return RedirectResponse("/repairs", status_code=303)

    price = repair.final_cost if repair.final_cost is not None else (repair.estimated_cost or 0)
    request.session["cart"] = [{
        "product_id": "",
        "name": f"Repair #{repair.ticket_no} — {repair.device} ({repair.issue})",
        "sku": f"RPR-{repair.ticket_no}",
        "price": round(price, 2),
        "qty": 1,
    }]
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = repair.customer_id
    reset_customer_redemptions(request)
    request.session["pos_repair_id"] = repair.id
    return RedirectResponse("/pos", status_code=303)


# ── STAFF ────────────────────────────────────────────────────────────
@app.get("/staff", response_class=HTMLResponse)
def staff_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — staff management requires manager or owner role.", status_code=403)
    all_staff = db.query(Staff).all()
    security_question = get_setting(db, "security_question", "")
    security_configured = bool(get_setting(db, "security_answer_hash", ""))
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "staff.html", {
        "staff": staff, "all_staff": all_staff, "flash": flash, "min_pin": _sec.min_pin_length(db),
        "security_question": security_question, "security_configured": security_configured,
    })


STAFF_ROLES = ("owner", "manager", "cashier", "technician")
STAFF_NAME_MAX = 80


def _staff_input_error(name: str, role: str) -> str:
    """A staff member needs a real name and one of the four roles. The Add/Edit forms already guarantee both; this stops a
    hand-made request from saving a blank name (an invisible row on the Staff list and in every report) or a made-up role."""
    if not (name or "").strip():
        return "Enter the person's name."
    if len(name.strip()) > STAFF_NAME_MAX:
        return f"The name is too long (most {STAFF_NAME_MAX} characters)."
    if role not in STAFF_ROLES:
        return "Pick one of the roles in the list."
    return ""


@app.post("/staff/add")
def staff_add(request: Request, name: str = Form(...), pin: str = Form(...),
              role: str = Form("cashier"), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    if role == "owner" and current.role != "owner":
        add_audit(db, current, "SECURITY_BLOCK", f"Manager attempted to create an Owner account: {name}")
        db.commit()
        return HTMLResponse("Forbidden — only an Owner can create another Owner account.", status_code=403)
    input_error = _staff_input_error(name, role)
    if input_error:
        request.session["flash"] = ("red", f"{input_error} Nobody was added.")
        return RedirectResponse("/staff", status_code=303)
    name = name.strip()
    pin = _clean_pin_input(pin)
    pin_error = _sec.validate_new_pin(db, pin, request=request, actor=current)
    if pin_error:
        return HTMLResponse(pin_error, status_code=400)
    db.add(Staff(name=name, role=role, pin_hash=hash_pin(pin), pin_len=len(pin), active=True))
    add_audit(db, current, "STAFF_ADD", f"Added staff member: {name}")
    db.commit()
    return RedirectResponse("/staff", status_code=303)


@app.post("/staff/{staff_id}/edit")
def staff_edit(request: Request, staff_id: str, name: str = Form(...),
                role: str = Form(...), new_pin: str = Form(""),
                security_answer: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    target = db.get(Staff, staff_id)
    if not target:
        return RedirectResponse("/staff", status_code=303)

    if current.role != "owner" and target.role == "owner":
        add_audit(db, current, "SECURITY_BLOCK", f"Manager attempted to edit Owner account: {target.name}")
        db.commit()
        return HTMLResponse("Forbidden — only an Owner can edit an Owner account.", status_code=403)
    if current.role != "owner" and role == "owner":
        add_audit(db, current, "SECURITY_BLOCK", f"Manager attempted to grant Owner role to: {name}")
        db.commit()
        return HTMLResponse("Forbidden — only an Owner can grant the Owner role.", status_code=403)

    input_error = _staff_input_error(name, role)
    if input_error:
        request.session["flash"] = ("red", f"{input_error} Nothing was changed.")
        return RedirectResponse("/staff", status_code=303)
    name = name.strip()
    if target.role == "owner" and role != "owner" and target.active:
        # same protection as deactivating: with no Owner left nobody could open Settings, backups or the Owner-only controls
        other_active_owners = db.query(Staff).filter(
            Staff.role == "owner", Staff.active == True, Staff.id != target.id  # noqa: E712
        ).count()
        if other_active_owners == 0:
            request.session["flash"] = ("red", "This is the last active Owner, so the role can't be changed. "
                                               "Make another person an Owner first. Nothing was changed.")
            return RedirectResponse("/staff", status_code=303)

    new_pin = _clean_pin_input(new_pin)
    if new_pin:
        pin_error = _sec.validate_new_pin(db, new_pin, target.id, request=request, actor=current)
        if pin_error:
            request.session["flash"] = ("red", f"{pin_error} Nothing was changed.")
            return RedirectResponse("/staff", status_code=303)
    pin_changing = bool(new_pin)
    answer_hash = get_setting(db, "security_answer_hash", "")
    if pin_changing and answer_hash:
        if not verify_pin(security_answer.strip().lower(), answer_hash):
            add_audit(db, current, "SECURITY_BLOCK", f"Wrong security answer while changing PIN for: {target.name}")
            db.commit()
            request.session["flash"] = ("red", "Incorrect security answer — PIN was not changed.")
            return RedirectResponse("/staff", status_code=303)

    target.name, target.role = name, role
    if pin_changing:
        target.pin_hash = hash_pin(new_pin)
        target.pin_len = len(new_pin)
        add_audit(db, current, "PIN_CHANGE", f"Changed PIN for: {target.name}")
        if target.id == current.id:     # changing your own PIN must not sign you out of this device
            request.session["pk"] = pin_fingerprint(target.pin_hash)
            if _sec.is_weak_pin(new_pin):
                request.session["weak_pin"] = True
            else:
                request.session.pop("weak_pin", None)
    add_audit(db, current, "STAFF_EDIT", f"Edited staff member: {name}")
    db.commit()
    return RedirectResponse("/staff", status_code=303)


@app.post("/staff/{staff_id}/toggle")
def staff_toggle(request: Request, staff_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    target = db.get(Staff, staff_id)
    if not target:
        return RedirectResponse("/staff", status_code=303)

    if current.role != "owner" and target.role == "owner":
        add_audit(db, current, "SECURITY_BLOCK", f"Manager attempted to deactivate Owner account: {target.name}")
        db.commit()
        return HTMLResponse("Forbidden — only an Owner can deactivate an Owner account.", status_code=403)
    if target.role == "owner" and target.active:
        other_active_owners = db.query(Staff).filter(
            Staff.role == "owner", Staff.active == True, Staff.id != target.id  # noqa: E712
        ).count()
        if other_active_owners == 0:
            return HTMLResponse("Forbidden — you can't deactivate the last active Owner account. "
                                 "Promote another staff member to Owner first.", status_code=403)

    target.active = not target.active
    add_audit(db, current, "STAFF_TOGGLE", f"{'Activated' if target.active else 'Deactivated'} {target.name}")
    db.commit()
    return RedirectResponse("/staff", status_code=303)


# ── SETTINGS ─────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — settings require owner role.", status_code=403)
    settings = {s.key: s.value for s in db.query(Setting).all()}
    flash = request.session.pop("flash", None)
    webhook_secret = get_sms_webhook_secret(db)
    sms_webhook_url = f"{str(request.base_url).rstrip('/')}/webhooks/sms-reply/{webhook_secret}"
    return templates.TemplateResponse(request, "settings.html", {
        "staff": staff, "settings": settings, "flash": flash,
        "security_question_set": bool(settings.get("security_answer_hash")),
        "sms_webhook_url": sms_webhook_url,
        "timezone_choices": TIMEZONE_CHOICES,
        "effective_timezone": zone_name_for(settings.get("timezone", ""), settings.get("province", "")),
        **_intake_settings_context(db),
    })


@app.post("/settings")
def settings_save(request: Request, shop_name: str = Form(""), province: str = Form(""),
                   invoice_prefix: str = Form(""), shop_address: str = Form(""),
                   shop_phone: str = Form(""), shop_email: str = Form(""),
                   shop_gst: str = Form(""), shop_pst: str = Form(""),
                   points_per_dollar: float = Form(1), points_redeem_rate: float = Form(100),
                   email_method: str = Form("smtp"),
                   smtp_host: str = Form(""), smtp_port: str = Form(""), smtp_user: str = Form(""),
                   smtp_password: str = Form(""), smtp_from: str = Form(""),
                   brevo_api_key: str = Form(""), brevo_from_email: str = Form(""), brevo_from_name: str = Form(""),
                   twilio_sid: str = Form(""), twilio_token: str = Form(""), twilio_from: str = Form(""),
                   digest_enabled: str = Form(""), digest_email: str = Form(""), digest_hour: int = Form(21),
                   backup_email_enabled: str = Form(""), backup_email: str = Form(""),
                   security_question: str = Form(""), security_answer: str = Form(""),
                   timezone: str = Form(None),
                   db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)

    # These three are load-bearing (shop name on every receipt/label,
    # province drives tax calculation on every sale) — rather than
    # either 422-crashing the whole save on a framework validation
    # error, or silently persisting a blank shop name, catch it here
    # and bounce back with a clear message, leaving whatever was
    # already saved untouched.
    if not shop_name.strip() or not province.strip() or not invoice_prefix.strip():
        request.session["flash"] = ("red", "Shop Name, Province, and Invoice Prefix can't be blank — nothing was saved.")
        return RedirectResponse("/settings", status_code=303)

    if not (_inputs.money(points_per_dollar, 0, 100) and _inputs.money(points_redeem_rate, 1, 1_000_000)
            and _inputs.whole(digest_hour, 0, 23)):
        request.session["flash"] = ("red", "Nothing was saved: points per dollar must be 0 to 100, points per $1 off 1 or more, and the digest hour 0 to 23.")
        return RedirectResponse("/settings", status_code=303)

    set_setting(db, "shop_name", shop_name)
    set_setting(db, "province", province)
    # "" = work it out from the province; otherwise one of the listed zones
    if timezone is not None:  # a form that doesn't send the field leaves the setting alone
        set_setting(db, "timezone", timezone if timezone in {z for z, _ in TIMEZONE_CHOICES} else "")
    set_setting(db, "invoice_prefix", invoice_prefix)
    set_setting(db, "shop_address", shop_address)
    set_setting(db, "shop_phone", shop_phone)
    set_setting(db, "shop_email", shop_email)
    set_setting(db, "shop_gst", shop_gst)
    set_setting(db, "shop_pst", shop_pst)
    set_setting(db, "points_per_dollar", str(points_per_dollar))
    set_setting(db, "points_redeem_rate", str(points_redeem_rate))
    set_setting(db, "email_method", "brevo_api" if email_method == "brevo_api" else "smtp")
    set_setting(db, "smtp_host", smtp_host)
    set_setting(db, "smtp_port", smtp_port)
    set_setting(db, "smtp_user", smtp_user)
    if smtp_password:  # only overwrite if a new one was actually typed
        set_setting(db, "smtp_password", encrypt_value(smtp_password))
    set_setting(db, "smtp_from", smtp_from)
    if brevo_api_key:  # only overwrite if a new one was actually typed
        set_setting(db, "brevo_api_key", encrypt_value(brevo_api_key))
    set_setting(db, "brevo_from_email", brevo_from_email)
    set_setting(db, "brevo_from_name", brevo_from_name)
    set_setting(db, "twilio_sid", twilio_sid)
    if twilio_token:
        set_setting(db, "twilio_token", encrypt_value(twilio_token))
    set_setting(db, "twilio_from", twilio_from)
    set_setting(db, "digest_enabled", "true" if digest_enabled == "on" else "false")
    set_setting(db, "digest_email", digest_email)
    set_setting(db, "digest_hour", str(digest_hour))
    set_setting(db, "backup_email_enabled", "true" if backup_email_enabled == "on" else "false")
    set_setting(db, "backup_email", backup_email)
    if security_question.strip():
        set_setting(db, "security_question", security_question.strip())
    if security_answer.strip():  # only overwrite if a new answer was actually typed
        set_setting(db, "security_answer_hash", hash_pin(security_answer.strip().lower()))
    add_audit(db, staff, "SETTINGS", "Updated shop settings")
    get_shop_tz(db)  # refresh the zone templates use for display
    _schedule_background_jobs()  # pick up a new digest_hour immediately, not just on next restart
    return RedirectResponse("/settings", status_code=303)


# ── CASH SESSIONS ────────────────────────────────────────────────────
def todays_cash_card_totals(db: Session, today_str: str) -> dict:
    """The numbers Cash Up reconciles against physical reality: how much
    cash should be in the drawer and how much should show on the card
    terminal's batch report, for the shop-local day `today_str`.

    The rules live in reporting.cash_card_totals() so Cash Up, the Daily
    Report and the Z-report can never disagree. In short: invoice
    cash/card portions (UPI/E-Transfer/store credit touch neither),
    layaway installments on the day they were collected, cash trade-in
    payouts subtracted, and refunds paid back out of the drawer/terminal
    on the day they were paid. Day boundaries are the shop's local
    midnight, not UTC (an evening sale used to land on tomorrow's Cash Up).
    """
    return cash_card_totals(db, get_shop_tz(db), _date_cls.fromisoformat(today_str))


@app.get("/cashup", response_class=HTMLResponse)
def cashup_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    today_str = local_today(get_shop_tz(db)).isoformat()
    totals = todays_cash_card_totals(db, today_str)

    cash_float = float(get_setting(db, "cash_float", "200"))
    expected = round(cash_float + totals["cash_net"], 2)
    today_session = db.query(CashSession).filter(CashSession.date == today_str).first()
    history = db.query(CashSession).order_by(CashSession.date.desc()).limit(10).all()
    # v52: paid-outs entered AFTER today's Cash Up was closed are not in the figure that was saved
    late_paid_out = 0.0
    if today_session is not None:
        late_paid_out = round(totals["cash_paid_out"] - float(today_session.paid_out or 0), 2)
    paid_outs = []
    if role_allowed(staff, "owner", "manager"):
        s_utc, e_utc = day_bounds_utc(_date_cls.fromisoformat(today_str), None, get_shop_tz(db))
        paid_outs = [x for x in _expenses_mod.in_window(db, s_utc, e_utc) if x.paid_from == "drawer"]

    return templates.TemplateResponse(request, "cashup.html", {
        "staff": staff, "cash_float": cash_float, "expected": expected, "flash": request.session.pop("flash", None),
        "today_session": today_session, "history": history,
        "late_paid_out": late_paid_out, "paid_outs": paid_outs,
        **totals,
    })


@app.post("/cashup/close")
def cashup_close(request: Request, open_float: float = Form(...), actual: float = Form(...),
                  card_batch: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    # Typed counts are checked here as well as in the browser: a "nan" or a minus sign saved as the opening float would
    # be carried into every later day's expected-cash figure.
    _bad = [x for x in (open_float, actual) if not math.isfinite(x) or x < 0 or x > 10_000_000]
    if not _bad and card_batch.strip():
        try:
            _cb = float(card_batch.replace("$", "").replace(",", "").strip())
            if not math.isfinite(_cb) or abs(_cb) > 10_000_000:
                _bad = [_cb]
        except ValueError:
            _bad = [card_batch]
    if _bad:
        request.session["flash"] = ("red", "Cash Up was not saved: the float and the counted cash must be amounts of $0 or more, and the card batch a number.")
        return RedirectResponse("/cashup", status_code=303)

    today_str = local_today(get_shop_tz(db)).isoformat()
    totals = todays_cash_card_totals(db, today_str)
    cash_sales = totals["cash_net"]            # v52: sales minus cash taken out of the till for expenses
    card_sales = totals["card_sales"]
    expected = round(open_float + cash_sales, 2)
    diff = round(actual - expected, 2)

    card_batch_val = float(card_batch) if card_batch.strip() else round(card_sales, 2)
    card_diff = round(card_batch_val - card_sales, 2)

    # Replace any existing session for today, same as the original
    existing = db.query(CashSession).filter(CashSession.date == today_str).first()
    if existing:
        db.delete(existing)
        db.flush()

    db.add(CashSession(
        date=today_str, open_float=open_float, expected=expected, actual=actual,
        difference=diff, card_expected=round(card_sales, 2), card_batch=card_batch_val,
        card_difference=card_diff, notes=notes, closed_by_id=staff.id, closed_by_name=staff.name,
        paid_out=round(totals["cash_paid_out"], 2),
    ))
    if card_batch.strip():
        # a batch the owner actually typed counts as "entered" for the Moneris check
        # (a blank field only ever meant "it matches", which is not a confirmation)
        _save_card_batch(db, today_str, card_batch_val, "")
    set_setting(db, "cash_float", str(open_float))
    audit_msg = f"Cash-up closed: cash expected ${expected:.2f}, actual ${actual:.2f}, diff ${diff:.2f}"
    if totals["cash_paid_out"]:
        audit_msg += f" | taken out of till ${totals['cash_paid_out']:.2f}"
    if abs(card_diff) > 0.01:
        audit_msg += f" | card terminal mismatch ${card_diff:.2f}"
    add_audit(db, staff, "CASH_UP", audit_msg)
    db.commit()
    return RedirectResponse("/cashup", status_code=303)


# ── MONERIS CHECK (v47) ──────────────────────────────────────────────
def _save_card_batch(db: Session, day_str: str, amount: float, note: str) -> "CardBatch":
    """Upsert the Moneris batch total for a shop-local day and keep that day's
    Cash Up record (if one exists) in step with it."""
    row = db.query(CardBatch).filter(CardBatch.date == day_str).first()
    if row is None:
        row = CardBatch(date=day_str)
        db.add(row)
    row.amount = round(float(amount), 2)
    if note is not None and (note or row.note is None):
        row.note = note
    row.entered_at = datetime.utcnow()
    sess = db.query(CashSession).filter(CashSession.date == day_str).first()
    if sess is not None:
        crm = cash_card_totals(db, get_shop_tz(db), _date_cls.fromisoformat(day_str))["card_sales"]
        sess.card_expected = round(crm, 2)
        sess.card_batch = row.amount
        sess.card_difference = round(row.amount - crm, 2)
    return row


def _dollars(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        r["amount"] = round(r["amount_c"] / 100, 2)
        r["signed"] = r["amount"] if r["kind"] == "sale" else -r["amount"]
        out.append(r)
    return out


def _recon_context(request: Request, db: Session, staff, d, bank=None, matched=None, csv_text="", all_dates=False) -> dict:
    tz = get_shop_tz(db)
    today = local_today(tz)
    rows = crm_card_rows(db, tz, d)
    totals = cash_card_totals(db, tz, d)
    crm_net = totals["card_sales"]
    batch = db.query(CardBatch).filter(CardBatch.date == d.isoformat()).first()
    diff = round((batch.amount or 0) - crm_net, 2) if batch else None
    checks = day_end_checks(db, tz, full=True)
    res = None
    if matched is not None:
        res = {
            "pairs": [dict(p, crm=_dollars([p["crm"]])[0], bank=_dollars([p["bank"]])[0]) for p in matched["pairs"]],
            "crm_only": _dollars(matched["crm_only"]), "bank_only": _dollars(matched["bank_only"]),
            "bank_net": round(matched["bank_net_c"] / 100, 2), "crm_net": round(matched["crm_net_c"] / 100, 2),
            "clean": matched["clean"],
        }
    return {
        "staff": staff, "d": d, "d_iso": d.isoformat(), "is_today": d == today, "today_iso": today.isoformat(),
        "prev_d": (d - timedelta(days=1)).isoformat(), "next_d": (d + timedelta(days=1)).isoformat() if d < today else None,
        "rows": _dollars(rows), "crm_net": crm_net, "batch": batch, "diff": diff, "checks": checks,
        "can_edit": role_allowed(staff, "owner", "manager"), "bank": bank, "res": res, "csv_text": csv_text,
        "all_dates": all_dates, "require_ref": require_card_ref_on(db),
        "flash": request.session.pop("flash", None),
    }


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(request: Request, date: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    tz = get_shop_tz(db)
    today = local_today(tz)
    d = min(parse_date(date, today), today)
    return templates.TemplateResponse(request, "reconcile.html", _recon_context(request, db, staff, d))


@app.post("/reconcile/batch")
def reconcile_save_batch(request: Request, date: str = Form(""), amount: str = Form(""), note: str = Form(""),
                          db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — entering a terminal batch requires manager or owner role.", status_code=403)
    tz = get_shop_tz(db)
    today = local_today(tz)
    d = min(parse_date(date, today), today)
    try:
        amt = float(amount.replace("$", "").replace(",", "").strip())
        if amt != amt or abs(amt) > 10_000_000:
            raise ValueError
    except ValueError:
        request.session["flash"] = ("red", "Enter the batch total as a number, e.g. 1234.56 (use a minus sign if refunds exceeded sales).")
        return RedirectResponse(f"/reconcile?date={d.isoformat()}", status_code=303)
    row = _save_card_batch(db, d.isoformat(), amt, note.strip()[:300])
    crm = cash_card_totals(db, tz, d)["card_sales"]
    diff = round(row.amount - crm, 2)
    add_audit(db, staff, "CARD_BATCH", f"Moneris batch for {d.isoformat()}: ${row.amount:.2f} vs CRM ${crm:.2f}"
                                       + (f" — off by ${diff:+.2f}" if abs(diff) > 0.009 else " — matches"))
    db.commit()
    if abs(diff) > 0.009:
        request.session["flash"] = ("amber", f"Saved. The terminal batch is ${abs(diff):.2f} {'above' if diff > 0 else 'below'} the CRM's card total — "
                                              f"paste the terminal's transaction list below to find which sale.")
    else:
        request.session["flash"] = ("green", "Saved — the terminal batch matches the CRM's card total.")
    return RedirectResponse(f"/reconcile?date={d.isoformat()}", status_code=303)


@app.post("/reconcile/resolve")
def reconcile_resolve(request: Request, date: str = Form(""), resolved: str = Form(""), note: str = Form(""),
                       db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — requires manager or owner role.", status_code=403)
    tz = get_shop_tz(db)
    d = min(parse_date(date, local_today(tz)), local_today(tz))
    row = db.query(CardBatch).filter(CardBatch.date == d.isoformat()).first()
    if row:
        row.resolved = resolved == "on"
        if note.strip():
            row.note = note.strip()[:300]
        add_audit(db, staff, "CARD_BATCH", f"Moneris difference for {d.isoformat()} marked {'explained' if row.resolved else 'open again'}"
                                           + (f" — {row.note}" if row.note else ""))
        db.commit()
    return RedirectResponse(f"/reconcile?date={d.isoformat()}", status_code=303)


@app.post("/reconcile/match", response_class=HTMLResponse)
async def reconcile_match(request: Request, date: str = Form(""), csv_text: str = Form(""), all_dates: str = Form(""),
                           col_amount: str = Form(""), col_type: str = Form(""), col_date: str = Form(""),
                           file: UploadFile = File(None),
                           db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    tz = get_shop_tz(db)
    today = local_today(tz)
    d = min(parse_date(date, today), today)
    text = csv_text or ""
    if file is not None and getattr(file, "filename", ""):
        raw = await file.read(1_000_001)
        if len(raw) > 1_000_000:
            request.session["flash"] = ("red", "That file is too large to be a transaction list (over 1 MB).")
            return RedirectResponse(f"/reconcile?date={d.isoformat()}", status_code=303)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    text = text[:1_000_000]
    override = {"amount": col_amount, "type": col_type, "date": col_date}
    bank = parse_bank_csv(text, day=d, all_dates=(all_dates == "on"), cols_override=override)
    matched = None
    if not bank["error"]:
        matched = match_transactions(crm_card_rows(db, tz, d), bank["rows"])
    ctx = _recon_context(request, db, staff, d, bank=bank, matched=matched, csv_text=text, all_dates=(all_dates == "on"))
    return templates.TemplateResponse(request, "reconcile.html", ctx)


# ── CARD FEES & STATEMENTS (v47) ─────────────────────────────────────
def _card_fees_guard(request: Request, db: Session):
    staff = require_login(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return None, HTMLResponse("Forbidden — card fees require manager or owner role.", status_code=403)
    return staff, None


@app.get("/card-fees", response_class=HTMLResponse)
def card_fees_page(request: Request, db: Session = Depends(get_db)):
    staff, deny = _card_fees_guard(request, db)
    if deny:
        return deny
    tz = get_shop_tz(db)
    today = local_today(tz)
    rows = []
    cur = _date_cls(today.year, today.month, 1)
    for _ in range(12):
        st = db.query(CardStatement).filter(CardStatement.month == month_key(cur)).first()
        crm = crm_card_month(db, tz, cur)
        base = (st.volume if st and st.volume else crm["net"]) if st else crm["net"]
        rows.append({
            "key": month_key(cur), "label": cur.strftime("%B %Y"), "crm": crm, "st": st,
            "diff": round(st.volume - crm["net"], 2) if st and st.volume is not None else None,
            "rate": round((st.fees or 0) / base * 100, 2) if st and base else None,
        })
        cur = _date_cls(cur.year - (cur.month == 1), 12 if cur.month == 1 else cur.month - 1, 1)
    rate = fee_rate(db)
    return templates.TemplateResponse(request, "card_fees.html", {
        "staff": staff, "rows": rows, "this_month": month_key(today),
        "fee_rate": rate, "cash_rounding": cash_rounding_on(db), "require_ref": require_card_ref_on(db),
        "flash": request.session.pop("flash", None),
    })


@app.post("/card-fees/save")
def card_fees_save(request: Request, month: str = Form(""), volume: str = Form(""), fees: str = Form(""), note: str = Form(""),
                    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff, deny = _card_fees_guard(request, db)
    if deny:
        return deny
    first = parse_month_key(month)
    today = local_today(get_shop_tz(db))
    if first is None or first > _date_cls(today.year, today.month, 1):
        request.session["flash"] = ("red", "Pick a month (not a future one).")
        return RedirectResponse("/card-fees", status_code=303)
    try:
        fees_v = float(fees.replace("$", "").replace(",", "").strip())
        vol_v = float(volume.replace("$", "").replace(",", "").strip()) if volume.strip() else None
        if fees_v < 0 or fees_v > 1_000_000 or (vol_v is not None and abs(vol_v) > 100_000_000) or fees_v != fees_v:
            raise ValueError
    except ValueError:
        request.session["flash"] = ("red", "Enter the fees as a positive number, e.g. 87.42 (the volume is optional).")
        return RedirectResponse("/card-fees", status_code=303)
    row = db.query(CardStatement).filter(CardStatement.month == month_key(first)).first()
    if row is None:
        row = CardStatement(month=month_key(first))
        db.add(row)
    row.fees, row.volume, row.note, row.entered_at = round(fees_v, 2), (round(vol_v, 2) if vol_v is not None else None), note.strip()[:300], datetime.utcnow()
    add_audit(db, staff, "CARD_FEES", f"Moneris statement {month_key(first)}: fees ${fees_v:.2f}" + (f", volume ${vol_v:.2f}" if vol_v is not None else ""))
    db.commit()
    request.session["flash"] = ("green", f"Saved the {first.strftime('%B %Y')} statement.")
    return RedirectResponse("/card-fees", status_code=303)


@app.post("/card-fees/delete")
def card_fees_delete(request: Request, month: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff, deny = _card_fees_guard(request, db)
    if deny:
        return deny
    first = parse_month_key(month)
    row = db.query(CardStatement).filter(CardStatement.month == month_key(first)).first() if first else None
    if row:
        db.delete(row)
        add_audit(db, staff, "CARD_FEES", f"Removed Moneris statement entry for {month_key(first)}")
        db.commit()
    return RedirectResponse("/card-fees", status_code=303)


@app.post("/card-fees/settings")
def card_fees_settings(request: Request, cash_rounding: str = Form(""), require_card_ref: str = Form(""), fee_rate_pct: str = Form(""),
                        db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff, deny = _card_fees_guard(request, db)
    if deny:
        return deny
    rate_txt = fee_rate_pct.strip().replace("%", "")
    if rate_txt:
        try:
            rv = float(rate_txt)
            if not (0 < rv < 20):
                raise ValueError
        except ValueError:
            request.session["flash"] = ("red", "The estimated fee rate must be a percentage between 0 and 20, e.g. 2.65 — nothing was changed.")
            return RedirectResponse("/card-fees", status_code=303)
        rate_txt = f"{rv:g}"
    set_setting(db, "cash_rounding", "true" if cash_rounding == "on" else "false")
    set_setting(db, "require_card_ref", "true" if require_card_ref == "on" else "false")
    set_setting(db, "card_fee_rate", rate_txt)
    add_audit(db, staff, "SETTINGS", f"Payment settings: cash rounding {'on' if cash_rounding == 'on' else 'off'}, "
                                     f"Moneris reference {'required' if require_card_ref == 'on' else 'optional'}, "
                                     f"estimated fee rate {rate_txt + '%' if rate_txt else 'none'}")
    db.commit()
    request.session["flash"] = ("green", "Payment settings saved.")
    return RedirectResponse("/card-fees", status_code=303)


def build_daily_digest(db: Session) -> tuple[str, str]:
    """Returns (subject, body) for the daily owner digest email."""
    tz = get_shop_tz(db)
    today = local_today(tz)
    today_str = today.strftime("%Y-%m-%d")
    shop_name = get_setting(db, "shop_name", "Your Shop")
    day_start, day_end = day_bounds_utc(today, today, tz)

    today_invoices = db.query(Invoice).filter(Invoice.date >= day_start, Invoice.date < day_end).all()
    paid = [i for i in today_invoices if not i.refunded]
    # refunds processed today — including refunds of earlier days' sales
    refunded = (db.query(Invoice).filter(Invoice.refunded.is_(True),
                or_((Invoice.refunded_at >= day_start) & (Invoice.refunded_at < day_end),
                    Invoice.refunded_at.is_(None) & (Invoice.date >= day_start) & (Invoice.date < day_end))).all())
    revenue = round(sum(i.total for i in paid), 2)
    tax = round(sum(i.tax_total for i in paid), 2)
    day_totals = SalesWindow(db, tz, today, today).totals()

    repairs_opened = db.query(Repair).filter(Repair.created_at >= day_start, Repair.created_at < day_end).count()
    repairs_closed_today = db.query(Repair).filter(Repair.status.in_(["COMPLETED", "COLLECTED"]),
                                                    Repair.updated_at >= day_start, Repair.updated_at < day_end).count()

    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).all()

    cash_session = db.query(CashSession).filter(CashSession.date == today_str).first()

    lines = [
        f"Daily summary for {shop_name} — {today.strftime('%A, %B')} {today.day}, {today.year}",
        "",
        f"💰 Revenue: ${revenue:.2f} ({len(paid)} sale{'s' if len(paid) != 1 else ''})",
        f"🧾 Tax collected: ${tax:.2f}",
        f"📈 Gross profit: ${day_totals['gross_profit']:.2f} ({day_totals['margin']:.0f}% margin, net of refunds)",
    ]
    if refunded:
        lines.append(f"↩️ Refunds today: {len(refunded)} (${sum(i.total for i in refunded):.2f})")
    _rets = _returns.in_window(db, day_start, day_end)
    if _rets:
        lines.append(f"↩️ Items returned from sales today: {len(_rets)} (${sum(r.total for r in _rets):.2f} paid back)")
    _out = _expenses_mod.drawer_out(db, day_start, day_end)
    if _out:
        lines.append(f"💸 Taken out of the till today: ${_out:.2f} (see Expenses)")
    _texts_sent = db.query(OutboundMessage).filter(OutboundMessage.status == "sent", OutboundMessage.source != "manual",
                                                   OutboundMessage.sent_at >= day_start, OutboundMessage.sent_at < day_end).count()
    _texts_failed = db.query(OutboundMessage).filter(OutboundMessage.status == "failed",
                                                     OutboundMessage.created_at >= datetime.utcnow() - timedelta(days=7)).count()
    if _texts_sent or _texts_failed:
        lines.append(f"📱 Automatic customer texts today: {_texts_sent} sent"
                     + (f" — ⚠️ {_texts_failed} failed in the last 7 days (see Automatic texts)" if _texts_failed else ""))
    lines += [
        "",
        f"🔧 Repair tickets opened today: {repairs_opened}",
        f"✅ Repair tickets closed today: {repairs_closed_today}",
        "",
    ]
    if low_stock:
        lines.append(f"📦 Low stock — {len(low_stock)} item(s) need reordering:")
        for p in low_stock[:10]:
            lines.append(f"   • {p.name} ({p.sku}) — {p.stock} left, reorder {p.reorder_qty}")
        if len(low_stock) > 10:
            lines.append(f"   ...and {len(low_stock) - 10} more. Full list: /products/reorder")
    else:
        lines.append("📦 Stock levels are fine — nothing needs reordering.")
    lines.append("")
    if cash_session:
        diff = cash_session.difference
        lines.append(f"🔒 Cash-up was closed today — difference: {'+'if diff>=0 else ''}{diff:.2f}")
    else:
        lines.append("🔒 Cash-up has not been closed yet today.")
    _yday = today - timedelta(days=1)
    _ys, _ye = day_bounds_utc(_yday, _yday, tz)
    if (db.query(Invoice).filter(Invoice.date >= _ys, Invoice.date < _ye).count()
            and not db.query(CashSession).filter(CashSession.date == _yday.isoformat()).first()):
        lines.append(f"⚠️ Cash-up was never closed for {_yday.strftime('%A, %B')} {_yday.day} — the drawer for that day is unreconciled.")

    return f"{shop_name} — Daily Summary ({today_str})", "\n".join(lines)


@app.post("/settings/send-test-email")
def send_test_email(request: Request, to_email: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    method = get_setting(db, "email_method", "smtp")
    method_label = "Brevo's HTTPS API" if method == "brevo_api" else "SMTP"
    ok, msg = send_plain_email(
        db, to_email,
        f"Test email from {shop_name}",
        f"This is a test email from {shop_name}'s CRM. If you're reading this, {method_label} is working correctly.",
        get_setting,
    )
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/send-test-sms")
def send_test_sms(request: Request, to_phone: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    ok, msg = send_sms(
        db, to_phone,
        f"Test message from {shop_name}'s CRM. If you're reading this, Twilio is working correctly.",
        get_setting,
    )
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/smtp-diagnose", response_class=HTMLResponse)
def smtp_diagnose_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    return templates.TemplateResponse(request, "smtp_diagnose.html", {
        "staff": staff, "steps": None, "to_email": "",
        "settings": {s.key: s.value for s in db.query(Setting).all()},
    })


@app.post("/settings/smtp-diagnose", response_class=HTMLResponse)
def smtp_diagnose_run(request: Request, to_email: str = Form(...), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Runs SMTP connection step-by-step and reports exactly which phase
    fails — lets you see the precise error rather than a generic message."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)

    import socket as _socket

    host = get_setting(db, "smtp_host", "")
    port_str = get_setting(db, "smtp_port", "587")
    user = get_setting(db, "smtp_user", "")
    password = decrypt_value(get_setting(db, "smtp_password", ""))
    from_addr = get_setting(db, "smtp_from", "") or user
    shop_name = get_setting(db, "shop_name", "TechPro+")

    steps = []

    def s_ok(label, detail=""): steps.append(("ok", label, detail))
    def s_fail(label, detail=""): steps.append(("fail", label, detail))
    def s_warn(label, detail=""): steps.append(("warn", label, detail))

    ctx = {"settings": {s.key: s.value for s in db.query(Setting).all()},
           "staff": staff, "to_email": to_email, "steps": steps}

    def render(): return templates.TemplateResponse(request, "smtp_diagnose.html", ctx)

    # Step 1: settings completeness
    if not (host and port_str and user and password):
        s_fail("Settings check", "SMTP host, port, username, or password is missing in Settings.")
        return render()
    s_ok("Settings check", f"Host: {host}, Port: {port_str}, User: {user}, From: {from_addr}")

    # Step 2: TCP connectivity
    port = int(port_str)
    try:
        sock = _socket.create_connection((host, port), timeout=10)
        sock.close()
        s_ok("TCP connection", f"Reached {host}:{port}")
    except Exception as e:
        s_fail("TCP connection", f"Cannot connect to {host}:{port} — {e}. Your ISP or Windows firewall may be blocking outbound port {port}.")
        return render()

    # Step 3: TLS handshake
    import smtplib, ssl as _ssl
    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=_ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls(context=_ssl.create_default_context())
            server.ehlo()
        s_ok("TLS / STARTTLS", f"Encryption established")
    except Exception as e:
        s_fail("TLS / STARTTLS", str(e))
        return render()

    # Step 4: SMTP login
    try:
        server.login(user, password)
        s_ok("SMTP authentication", f"Logged in as {user}")
    except smtplib.SMTPAuthenticationError as e:
        s_fail("SMTP authentication",
               f"Credentials rejected — {e}. "
               f"For Brevo: the password must be your SMTP Key "
               f"(Brevo dashboard → SMTP & API → SMTP → Generate a new SMTP key). "
               f"It is NOT your Brevo account password.")
        server.quit()
        return render()
    except Exception as e:
        s_fail("SMTP authentication", str(e))
        server.quit()
        return render()

    # Step 5: Brevo-specific verified sender check
    is_brevo = "brevo" in host.lower() or "sendinblue" in host.lower()
    if is_brevo:
        s_warn("Brevo verified sender check",
               f"Brevo requires the From address '{from_addr}' to be added and verified in your "
               f"Brevo account before mail will actually be delivered. "
               f"Even if this test passes, if '{from_addr}' is not a verified sender, "
               f"Brevo accepts it at the SMTP level but silently drops it. "
               f"→ Go to: app.brevo.com → Senders & IPs → Senders → Add a sender.")
    else:
        s_ok("From address", f"Sending as {from_addr}")

    # Step 6: actually send the test message
    from email.mime.text import MIMEText
    from email.utils import formatdate, make_msgid
    try:
        msg = MIMEText(
            f"SMTP diagnostic test from {shop_name} CRM.\n\n"
            f"Connection details:\n"
            f"  Server: {host}:{port}\n"
            f"  Auth user: {user}\n"
            f"  From address: {from_addr}\n"
            f"  To: {to_email}\n\n"
            f"If you are reading this, all 6 steps passed successfully.\n\n"
            f"If you are using Brevo and this message did NOT arrive:\n"
            f"  → The From address '{from_addr}' is not yet verified in your Brevo account.\n"
            f"  → Go to app.brevo.com → Senders & IPs → Senders → Add a sender.\n"
            f"  → Then try sending a receipt again."
        )
        msg["Subject"] = f"[SMTP Diagnostic] {shop_name} CRM — step-by-step test"
        msg["From"] = from_addr
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        refused = server.send_message(msg)
        server.quit()

        if refused:
            s_fail("Message accepted by server",
                   f"Server refused the recipient address: {refused}. "
                   f"Double-check the customer's email address.")
        else:
            s_ok("Message accepted by server",
                 f"✓ {host} accepted the message for delivery to {to_email}. "
                 f"Check inbox and spam folder. "
                 + ("If it doesn't arrive, the Brevo verified-sender issue above is most likely the cause."
                    if is_brevo else
                    "If it doesn't arrive within 2 minutes, check spam/junk."))
    except Exception as e:
        s_fail("Sending message", str(e))

    return render()


@app.post("/settings/send-test-digest")
def send_test_digest(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    recipient = get_setting(db, "digest_email", "")
    if not recipient:
        request.session["flash"] = ("red", "Set a digest email address first.")
        return RedirectResponse("/settings", status_code=303)
    subject, body = build_daily_digest(db)
    ok, msg = send_plain_email(db, recipient, subject, body, get_setting)
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/send-test-backup")
def send_test_backup(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Sends a backup right now, on demand — lets an owner confirm the
    whole pipeline (attachment building, whichever email path is
    configured, actual delivery) actually works, without waiting up to a
    week for the scheduled job to prove it one way or the other."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    recipient = get_setting(db, "backup_email", "") or get_setting(db, "digest_email", "")
    if not recipient:
        request.session["flash"] = ("red", "Set a backup email address (or a digest email) first.")
        return RedirectResponse("/settings", status_code=303)
    backup_bytes, filename, sealed = current_backup(db)
    if len(backup_bytes) > BACKUP_EMAIL_MAX_BYTES:
        request.session["flash"] = ("red", f"This backup is {len(backup_bytes) / 1_000_000:.1f} MB — too big to email reliably "
                                           f"(limit {BACKUP_EMAIL_MAX_BYTES / 1_000_000:.1f} MB). Use Download Full Backup instead.")
        return RedirectResponse("/settings", status_code=303)
    ok, msg = send_backup_email(db, recipient, backup_bytes, filename, get_setting, sealed=sealed)
    if ok:
        set_setting(db, "last_backup", datetime.utcnow().isoformat())
        add_audit(db, staff, "BACKUP_EXPORT", f"Test backup emailed to {recipient}" + (" (password-locked)" if sealed else " (no password)"))
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


BACKUP_EMAIL_MAX_BYTES = int(os.environ.get("BACKUP_EMAIL_MAX_BYTES", "3500000"))   # Brevo: attachments under 4 MB


def run_daily_digest_job():
    """Called by the scheduler — opens its own DB session since it runs
    outside any request."""
    db = SessionLocal()
    try:
        enabled = get_setting(db, "digest_enabled", "false") == "true"
        recipient = get_setting(db, "digest_email", "")
        if not (enabled and recipient):
            return
        subject, body = build_daily_digest(db)
        send_plain_email(db, recipient, subject, body, get_setting)
    finally:
        db.close()


def run_weekly_backup_email_job():
    """Called by the scheduler — opens its own DB session since it runs
    outside any request. Runs every Sunday at the same hour the daily
    digest already uses, so there's no second time-of-day setting to
    configure just for this."""
    db = SessionLocal()
    try:
        enabled = get_setting(db, "backup_email_enabled", "false") == "true"
        recipient = get_setting(db, "backup_email", "") or get_setting(db, "digest_email", "")
        if not (enabled and recipient):
            return
        backup_bytes, filename, sealed = current_backup(db)
        if len(backup_bytes) > BACKUP_EMAIL_MAX_BYTES:
            add_audit(db, None, "BACKUP_FAILED", f"Weekly backup NOT emailed: the file is {len(backup_bytes) / 1_000_000:.1f} MB, "
                                                 f"over the {BACKUP_EMAIL_MAX_BYTES / 1_000_000:.1f} MB email limit. Download a backup by hand.")
            return
        ok, detail = send_backup_email(db, recipient, backup_bytes, filename, get_setting, sealed=sealed)
        if ok:
            set_setting(db, "last_backup", datetime.utcnow().isoformat())
            add_audit(db, None, "BACKUP_EXPORT", f"Automatic weekly backup emailed to {recipient}" + (" (password-locked)" if sealed else " (no password)"))
        else:
            add_audit(db, None, "BACKUP_FAILED", f"Weekly backup email failed: {str(detail)[:200]}")
    finally:
        db.close()


def run_messaging_job():
    """Every 5 minutes: send the automatic texts that are due and still wanted."""
    db = SessionLocal()
    try:
        _messaging.process_due(db)
    except Exception as e:  # noqa: BLE001 — a scheduler job must never raise
        import logging
        logging.getLogger("techpro.messaging").exception("messaging run failed: %s", e)
    finally:
        db.close()


def run_winback_enqueue_job():
    """Daily: queue today's win-back texts (does nothing unless the owner switched them on)."""
    db = SessionLocal()
    try:
        _messaging.enqueue_winback(db)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("techpro.messaging").exception("win-back queueing failed: %s", e)
    finally:
        db.close()


_scheduler = BackgroundScheduler()
def _schedule_background_jobs():
    """Registers both scheduled jobs — the daily digest and the weekly
    backup email — and re-registers them (via replace_existing=True)
    whenever settings change, so a new digest_hour takes effect
    immediately instead of waiting for the next server restart."""
    db = SessionLocal()
    try:
        hour = int(get_setting(db, "digest_hour", "21"))
        job_tz = tz_label(get_shop_tz(db))  # "9pm" means 9pm in the shop, not on the server
    finally:
        db.close()
    _scheduler.add_job(run_daily_digest_job, "cron", hour=hour, timezone=job_tz, id="daily_digest", replace_existing=True)
    _scheduler.add_job(run_weekly_backup_email_job, "cron", day_of_week="sun", hour=hour, timezone=job_tz,
                        id="weekly_backup_email", replace_existing=True)
    if "pytest" not in sys.modules:      # tests call the job functions directly; a background timer would race them
        _scheduler.add_job(run_messaging_job, "interval", minutes=5, id="messaging_queue", replace_existing=True,
                           max_instances=1, coalesce=True)
        _scheduler.add_job(run_winback_enqueue_job, "cron", hour=10, minute=7, timezone=job_tz,
                           id="messaging_winback", replace_existing=True, max_instances=1, coalesce=True)
_schedule_background_jobs()
_scheduler.start()


# ── REPORTS ──────────────────────────────────────────────────────────
def _net_factor(inv) -> float:
    """Share of an invoice's line prices that was actually charged (1.0 with no discount). Manual discounts, redeemed
    loyalty points and store credit all come off the sale, so revenue, profit and category figures are scaled by this to
    agree with the Report Center (which shares discounts out across the lines the same way)."""
    try:
        sub, disc = float(inv.subtotal or 0), float(inv.discount or 0)
    except (TypeError, ValueError):
        return 1.0
    if sub <= 0 or disc <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - disc / sub))


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, tax_month: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — reports require manager or owner role.", status_code=403)

    tz = get_shop_tz(db)
    today_local = local_today(tz)
    first_this = today_local.replace(day=1)
    first_last = (first_this - timedelta(days=1)).replace(day=1)
    month_start = day_bounds_utc(first_this, first_this, tz)[0]      # UTC instant the local month began
    last_month_start = day_bounds_utc(first_last, first_last, tz)[0]
    last_month_end = month_start - timedelta(seconds=1)

    all_invoices = (db.query(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.staff))
                    .filter(Invoice.refunded == False).all())  # noqa: E712
    _products_by_id = {p.id: p for p in db.query(Product).all()}
    this_month = [i for i in all_invoices if i.date >= month_start]
    last_month = [i for i in all_invoices if last_month_start <= i.date <= last_month_end]

    # v52: items taken back from a sale (partial returns) come off the month they were paid back in
    _rets = db.query(SaleReturn).options(selectinload(SaleReturn.lines)).all()
    month_revenue = round(sum(i.total for i in this_month) - sum(r.total for r in _rets if r.refunded_at and r.refunded_at >= month_start), 2)
    last_month_revenue = round(sum(i.total for i in last_month)
                               - sum(r.total for r in _rets if r.refunded_at and last_month_start <= r.refunded_at <= last_month_end), 2)
    month_tax = round(sum(i.tax_total for i in this_month) - sum(r.tax_total for r in _rets if r.refunded_at and r.refunded_at >= month_start), 2)
    revenue_trend_pct = round(((month_revenue - last_month_revenue) / last_month_revenue) * 100) if last_month_revenue else None

    # Profit: (price - product cost) * qty, across all (non-refunded) invoices.
    # Repair charges are the one line type with no product attached (so
    # nothing above catches their cost automatically) — repair_detail.html
    # already computes a correct per-ticket margin (charge minus parts
    # used), via RepairPart; this pre-fetch brings that same real cost
    # into the roll-up here too, instead of every repair charge counting
    # as 100% pure profit the way it silently did before.
    repair_parts_cost_by_repair = {}
    for p in db.query(RepairPart).all():
        repair_parts_cost_by_repair[p.repair_id] = repair_parts_cost_by_repair.get(p.repair_id, 0) + p.qty * p.unit_cost

    profit = 0.0
    cogs = 0.0
    by_category = {}
    by_payment = {}
    prod_sales = {}
    staff_perf = {}
    for inv in all_invoices:
        pm_label = payment_method_label(inv.payment_method)
        by_payment[pm_label] = by_payment.get(pm_label, 0) + inv.total
        sname = inv.staff.name if inv.staff else "Unknown"
        sp = staff_perf.setdefault(sname, {"name": sname, "invoice_count": 0, "revenue": 0.0})
        sp["invoice_count"] += 1
        sp["revenue"] += inv.total

        # Parts cost is tied to the repair itself, not any one InvoiceLine —
        # applied once per invoice here, not inside the line loop below, so
        # a repair ticket with more than one charge line never has its
        # parts cost subtracted more than once.
        if inv.repair_id and inv.repair_id in repair_parts_cost_by_repair:
            repair_cost = repair_parts_cost_by_repair[inv.repair_id]
            profit -= repair_cost
            cogs += repair_cost

        _nf = _net_factor(inv)
        for line in inv.lines:
            product = _products_by_id.get(line.product_id) if line.product_id else None
            line_total = line.price * line.qty * _nf          # what was actually charged for the line, after discounts
            if product:
                _unit_cost = line.unit_cost if line.unit_cost is not None else product.cost  # cost at time of sale
                profit += line_total - _unit_cost * line.qty
                cogs += _unit_cost * line.qty
                cat = product.category or "Uncategorized"
                prod_sales.setdefault(line.product_id, {"name": line.name, "qty": 0, "rev": 0})
                prod_sales[line.product_id]["qty"] += line.qty
                prod_sales[line.product_id]["rev"] += line_total
            else:
                profit += line_total
                cat = "Repair / Service"
            by_category[cat] = by_category.get(cat, 0) + line_total

    for r in _rets:
        pm_label = payment_method_label(r.invoice.payment_method if r.invoice else "")
        by_payment[pm_label] = by_payment.get(pm_label, 0) - r.total
        _rnf = _net_factor(r.invoice) if r.invoice else 1.0
        if r.invoice is not None and r.invoice.staff is not None and r.invoice.staff.name in staff_perf:
            staff_perf[r.invoice.staff.name]["revenue"] -= r.total          # money paid back comes off the seller's revenue too
        for line in r.lines:
            line_total = line.price * line.qty * _rnf
            keep = (line.unit_cost or 0) * line.qty if line.restocked else 0.0   # cost only comes back if it went back on the shelf
            if line.product_id:
                profit -= line_total - keep
                cogs -= keep
                cat = (_products_by_id[line.product_id].category if line.product_id in _products_by_id else line.category) or "Uncategorized"
                if line.product_id in prod_sales:
                    prod_sales[line.product_id]["qty"] -= line.qty
                    prod_sales[line.product_id]["rev"] -= line_total
            else:
                profit -= line_total
                cat = "Repair / Service"
            by_category[cat] = by_category.get(cat, 0) - line_total
    top_products = sorted(prod_sales.values(), key=lambda p: p["qty"], reverse=True)[:8]
    staff_performance = sorted(staff_perf.values(), key=lambda s: s["revenue"], reverse=True)

    # Sales by day, last 14 days — for the Reports bar chart. Built the
    # same way the dashboard's sparkline already is (no charting
    # library — plain data the template turns into SVG bars).
    _chart_totals = {}
    chart_first = today_local - timedelta(days=13)
    chart_start = day_bounds_utc(chart_first, chart_first, tz)[0]
    for inv in all_invoices:
        if inv.date >= chart_start:
            day_key = to_local_date(inv.date, tz).isoformat()
            _chart_totals[day_key] = _chart_totals.get(day_key, 0) + inv.total
    for r in _rets:
        if r.refunded_at and r.refunded_at >= chart_start:
            day_key = to_local_date(r.refunded_at, tz).isoformat()
            _chart_totals[day_key] = _chart_totals.get(day_key, 0) - r.total
    sales_by_day = []
    for i in range(14):
        day = chart_first + timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        # NOT using %-d here — it's Linux-only and crashes on Windows
        # (same reason the custom `datefmt` Jinja filter exists elsewhere).
        sales_by_day.append({"label": f"{day.strftime('%b')} {day.day}",
                              "total": round(_chart_totals.get(key, 0), 2)})

    # Repairs closed per technician, this month — completes the staff performance picture
    repairs_this_month = db.query(Repair).filter(
        Repair.status.in_(["COMPLETED", "COLLECTED"]), Repair.updated_at >= month_start
    ).all()
    tech_repairs = {}
    for r in repairs_this_month:
        tname = r.technician.name if r.technician else "Unassigned"
        tech_repairs[tname] = tech_repairs.get(tname, 0) + 1

    # Trade-ins this month — deliberately kept separate from revenue/profit
    # above rather than netted into them (see the TradeIn model's own
    # docstring: a trade-in isn't a sale, it's a distinct cash/liability
    # movement). Cash paid out here is the same figure Cash Up now
    # subtracts from expected drawer cash; store credit issued isn't a
    # cash movement, but is a real future liability worth a bookkeeper
    # seeing alongside everything else.
    trade_ins_this_month = [t for t in db.query(TradeIn).all() if t.created_at >= month_start]
    trade_in_count = len(trade_ins_this_month)
    trade_in_cash_paid = round(sum(t.offered_amount for t in trade_ins_this_month if t.payout_method == "cash"), 2)
    trade_in_credit_issued = round(sum(t.offered_amount for t in trade_ins_this_month if t.payout_method == "store_credit"), 2)
    trade_in_total_paid = round(trade_in_cash_paid + trade_in_credit_issued, 2)

    # Tax summary for a selectable month (defaults to current month) — for CRA filing periods
    ty, tm = parse_month(tax_month, today_local)
    tax_month = f"{ty:04d}-{tm:02d}"
    tax_month_invoices = [i for i in all_invoices
                          if (lambda d: d.year == ty and d.month == tm)(to_local_date(i.date, tz))]
    tax_summary = {}
    for inv in tax_month_invoices:
        try:
            for line in json.loads(inv.tax_breakdown or "[]"):
                tax_summary[line["label"]] = tax_summary.get(line["label"], 0) + line["amount"]
        except (ValueError, KeyError, TypeError):
            continue
    for r in _rets:
        d_ = to_local_date(r.refunded_at, tz) if r.refunded_at else None
        if d_ and d_.year == ty and d_.month == tm:
            try:
                for line in json.loads(r.tax_breakdown or "[]"):
                    tax_summary[line["label"]] = tax_summary.get(line["label"], 0) - line["amount"]
            except (ValueError, KeyError, TypeError):
                continue
    tax_summary_total = round(sum(tax_summary.values()), 2)
    available_tax_months = sorted({to_local(i.date, tz).strftime("%Y-%m") for i in all_invoices}, reverse=True)

    return templates.TemplateResponse(request, "reports.html", {
        "staff": staff, "month_revenue": month_revenue, "last_month_revenue": last_month_revenue,
        "revenue_trend_pct": revenue_trend_pct, "month_tax": month_tax,
        "month_invoice_count": len(this_month), "profit": round(profit, 2), "cogs": round(cogs, 2),
        "by_category": sorted(by_category.items(), key=lambda x: x[1], reverse=True),
        "by_payment": sorted(by_payment.items(), key=lambda x: x[1], reverse=True),
        "top_products": top_products, "staff_performance": staff_performance,
        "sales_by_day": sales_by_day,
        "tech_repairs": sorted(tech_repairs.items(), key=lambda x: x[1], reverse=True),
        "tax_month": tax_month, "tax_summary": sorted(tax_summary.items()),
        "tax_summary_total": tax_summary_total, "available_tax_months": available_tax_months,
        "trade_in_count": trade_in_count, "trade_in_cash_paid": trade_in_cash_paid,
        "trade_in_credit_issued": trade_in_credit_issued, "trade_in_total_paid": trade_in_total_paid,
    })


def _build_eod_data(db: Session, month_str: str):
    """Shared by both the on-screen EOD report and the printable version."""
    tz = get_shop_tz(db)
    year, month = parse_month(month_str, local_today(tz))
    first, last = month_bounds(year, month)
    lo, hi = day_bounds_utc(first, last, tz)
    invoices = (db.query(Invoice).options(selectinload(Invoice.customer))
                .filter(Invoice.refunded == False, Invoice.date >= lo, Invoice.date < hi)  # noqa: E712
                .order_by(Invoice.date).all())
    days = {}
    for inv in invoices:
        local_dt = to_local(inv.date, tz)
        key = local_dt.strftime("%Y-%m-%d")
        d = days.setdefault(key, {
            "date": key, "weekday": local_dt.strftime("%A"),
            "count": 0, "subtotal": 0.0, "tax": 0.0, "total": 0.0,
            "by_payment": {}, "invoices": [],
        })
        d["count"] += 1
        d["subtotal"] += inv.subtotal
        d["tax"] += inv.tax_total
        d["total"] += inv.total
        pm_label = payment_method_label(inv.payment_method)
        d["by_payment"][pm_label] = d["by_payment"].get(pm_label, 0) + inv.total
        d["invoices"].append(inv)

    # v52: items taken back from a sale come off the day they were paid back
    for r in _returns.in_window(db, lo, hi):
        local_dt = to_local(r.refunded_at, tz)
        key = local_dt.strftime("%Y-%m-%d")
        d = days.setdefault(key, {
            "date": key, "weekday": local_dt.strftime("%A"),
            "count": 0, "subtotal": 0.0, "tax": 0.0, "total": 0.0,
            "by_payment": {}, "invoices": [],
        })
        d["returned"] = round(d.get("returned", 0.0) + r.total, 2)
        d["subtotal"] -= r.subtotal or 0
        d["tax"] -= r.tax_total or 0
        d["total"] -= r.total
        pm_label = payment_method_label(r.invoice.payment_method if r.invoice else "")
        d["by_payment"][pm_label] = d["by_payment"].get(pm_label, 0) - r.total

    for d in days.values():          # subtotal = what was charged before tax (discounts, loyalty and store credit already off)
        d["subtotal"] = round(d["total"] - d["tax"], 2)
    day_list = sorted(days.values(), key=lambda d: d["date"], reverse=True)
    grand_revenue = round(sum(d["total"] for d in day_list), 2)
    grand_tax = round(sum(d["tax"] for d in day_list), 2)
    grand_count = sum(d["count"] for d in day_list)
    daily_avg = round(grand_revenue / len(day_list), 2) if day_list else 0
    return day_list, grand_revenue, grand_tax, grand_count, daily_avg


def _local_month_str(db: Session) -> str:
    t = local_today(get_shop_tz(db))
    return f"{t.year:04d}-{t.month:02d}"


@app.get("/reports/eod", response_class=HTMLResponse)
def eod_report(request: Request, month: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — reports require manager or owner role.", status_code=403)
    month = month or _local_month_str(db)
    day_list, grand_revenue, grand_tax, grand_count, daily_avg = _build_eod_data(db, month)
    return templates.TemplateResponse(request, "eod_report.html", {
        "staff": staff, "month": month, "days": day_list,
        "grand_revenue": grand_revenue, "grand_tax": grand_tax,
        "grand_count": grand_count, "daily_avg": daily_avg,
    })


@app.get("/reports/eod/print", response_class=HTMLResponse)
def eod_report_print(request: Request, month: str = "", day: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — reports require manager or owner role.", status_code=403)
    month = month or _local_month_str(db)
    day_list, grand_revenue, grand_tax, grand_count, daily_avg = _build_eod_data(db, month)
    if day:
        day_list = [d for d in day_list if d["date"] == day]
        grand_revenue = round(sum(d["total"] for d in day_list), 2)
        grand_tax = round(sum(d["tax"] for d in day_list), 2)
        grand_count = sum(d["count"] for d in day_list)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    return templates.TemplateResponse(request, "eod_print.html", {
        "staff": staff, "month": month, "single_day": day, "days": day_list,
        "grand_revenue": grand_revenue, "grand_tax": grand_tax,
        "grand_count": grand_count, "shop_name": shop_name,
    })


# ── DATA EXPORT / IMPORT / BACKUP ───────────────────────────────────
def _csv_response(rows, headers, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    # text typed by staff (a customer named "=HYPERLINK(...)") must not turn into a live formula when the file is opened
    writer.writerows([tuple(_safe_cell(c) for c in r) for r in rows])
    return Response(content=buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def build_backup_json(db: Session) -> bytes:
    """A plain (password-less) backup document — kept under this name because tests and older code import it.
    Real backups go through current_backup(), which seals the file when a backup password is set."""
    return _backup.make_backup(db, None)[0]


_restore_moneris_records = _backup.restore_moneris_records     # kept under its old name for the tests


def backup_filename(sealed: bool = False) -> str:
    return _backup.backup_filename(sealed)


def backup_password(db: Session) -> str:
    return decrypt_value(get_setting(db, "backup_password", "")) or ""


def current_backup(db: Session):
    """(bytes, filename, sealed) — what both the Download button and the weekly email send."""
    data, ext, sealed = _backup.make_backup(db, backup_password(db) or None)
    return data, _backup.backup_filename(sealed, ext), sealed


@app.get("/export/backup")
def export_backup(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — backups require owner role.", status_code=403)

    content, filename, sealed = current_backup(db)
    set_setting(db, "last_backup", datetime.utcnow().isoformat())
    add_audit(db, staff, "BACKUP_EXPORT", "Full data backup exported" + (" (password-locked)" if sealed else " (no password — secrets left out)"))
    return Response(content=content, media_type="application/octet-stream" if sealed else "application/json",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@app.post("/settings/backup-password")
def save_backup_password(request: Request, password: str = Form(""), confirm: str = Form(""), clear: str = Form(""),
                         db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    if clear == "1":
        set_setting(db, "backup_password", "")
        add_audit(db, staff, "SECURITY", "Backup password removed — backups are no longer password-locked")
        request.session["flash"] = ("amber", "Backup password removed. New backups are plain files that leave out PIN hashes and passwords.")
    elif len(password) < _backup.MIN_PASSWORD_LENGTH:
        request.session["flash"] = ("red", f"The backup password must be at least {_backup.MIN_PASSWORD_LENGTH} characters. Nothing was changed.")
    elif password != confirm:
        request.session["flash"] = ("red", "The two passwords don't match. Nothing was changed.")
    else:
        set_setting(db, "backup_password", encrypt_value(password))
        add_audit(db, staff, "SECURITY", "Backup password set — backups are now password-locked")
        request.session["flash"] = ("green", "Backup password saved. Backups are now locked with it. Write it down: "
                                             "without it a locked backup can't be opened, and nobody can recover it for you.")
    return RedirectResponse("/settings#data", status_code=303)


def _backup_gate(request: Request, db: Session):
    staff = require_login(request, db)
    if not staff:
        return None, RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return None, HTMLResponse("Forbidden — restoring backups requires owner role.", status_code=403)
    return staff, None


def _restore_failed(request: Request, staff, message: str, status: int = 400):
    add_audit_failed = f"Backup restore refused: {message}"
    return HTMLResponse(f"{message} Nothing was changed; your existing data is untouched.", status_code=status), add_audit_failed


def _apply_restore(request: Request, db: Session, staff, doc: dict):
    """Runs a parsed backup into the database. Returns a redirect, or an error response."""
    try:
        if doc.get("version") == 1:
            counts = _backup.restore_legacy(db, doc)
            add_audit(db, staff, "BACKUP_IMPORT", "Older-format backup restored by merging (" + ", ".join(f"{k}: {v}" for k, v in counts.items()) + ")")
            db.commit()
            return RedirectResponse("/settings", status_code=303)
        counts = _backup.restore_full(db, doc, staff)
    except _backup.BackupError as e:
        add_audit(db, staff, "SECURITY_BLOCK", f"Backup restore refused: {e}")
        db.commit()
        return HTMLResponse(f"Restore aborted — {e} Nothing was changed; your existing data is untouched.", status_code=400)
    add_audit(db, None, "BACKUP_IMPORT", f"Full backup restored by {staff.name}: " + ", ".join(f"{k} {v}" for k, v in counts.items() if k not in ("settings",)))
    db.commit()
    _sec.bump_session_epoch(db)         # everyone signs in again, on purpose: the staff list may just have changed
    get_shop_tz(db)
    _schedule_background_jobs()
    request.session.clear()
    request.session["login_error"] = "Backup restored. Sign in again (with the PIN the backup holds)."
    return RedirectResponse("/login", status_code=303)


@app.post("/import/backup/preview", response_class=HTMLResponse)
async def import_backup_preview(request: Request, file: UploadFile = File(...), password: str = Form(""),
                                db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Step 1 of a restore: read the file and show what it would do. Nothing is changed yet."""
    staff, deny = _backup_gate(request, db)
    if deny:
        return deny
    raw = await file.read(_backup.MAX_UPLOAD_BYTES + 1)
    try:
        doc = _backup.read_backup(raw, password or None)
    except _backup.BackupError as e:
        request.session["flash"] = ("red", f"{e} Nothing was changed.")
        return RedirectResponse("/settings#data", status_code=303)
    token = _backup.hold(doc, staff.id, file.filename or "backup")
    legacy = doc.get("version") == 1
    return templates.TemplateResponse(request, "restore_confirm.html", {
        "staff": staff, "token": token, "legacy": legacy, "filename": file.filename,
        "exported_at": doc.get("exported_at", ""), "rows": _backup.summarize(db, doc),
        "has_secrets": bool(doc.get("includes_secrets")), "unknown": [] if legacy else _backup.unknown_tables(doc),
        "lost_total": sum(r["lost"] for r in _backup.summarize(db, doc)),
        "photo_effect": _backup.photo_effect(db, doc),
    })


@app.post("/import/backup/confirm")
def import_backup_confirm(request: Request, token: str = Form(""), confirm_text: str = Form(""),
                          db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """Step 2: the owner has seen the preview and typed the confirmation word."""
    staff, deny = _backup_gate(request, db)
    if deny:
        return deny
    item = _backup.take(token, staff.id)
    if not item:
        request.session["flash"] = ("red", "That restore expired (they wait 15 minutes). Choose the file again.")
        return RedirectResponse("/settings#data", status_code=303)
    if item["doc"].get("version") != 1 and confirm_text.strip().upper() != "RESTORE":
        request.session["flash"] = ("red", "Type RESTORE to confirm. Nothing was changed.")
        return RedirectResponse("/settings#data", status_code=303)
    _backup.release(token)
    return _apply_restore(request, db, staff, item["doc"])


@app.post("/import/backup")
async def import_backup(request: Request, file: UploadFile = File(...), password: str = Form(""),
                        db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
    """One-step restore (no preview) — for scripts and tests. The Settings page uses /preview + /confirm."""
    staff, deny = _backup_gate(request, db)
    if deny:
        return deny
    raw = await file.read(_backup.MAX_UPLOAD_BYTES + 1)
    try:
        doc = _backup.read_backup(raw, password or None)
    except _backup.BackupError as e:
        return HTMLResponse(f"Could not read the backup file: {e}", status_code=400)
    return _apply_restore(request, db, staff, doc)


@app.get("/export/csv/inventory")
def export_inventory_csv(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
    rows = [(p.sku, p.name, p.category, p.subcategory, p.price, p.cost, p.stock, p.barcode or "") for p in db.query(Product).all()]
    return _csv_response(rows, ["SKU", "Name", "Category", "Brand", "Price", "Cost", "Stock", "Barcode"], "inventory.csv")


@app.get("/export/csv/customers")
def export_customers_csv(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
    rows = [(c.name, c.phone, c.email, c.spent, c.points, c.store_credit) for c in db.query(Customer).all()]
    return _csv_response(rows, ["Name", "Phone", "Email", "Total Spent", "Points", "Store Credit"], "customers.csv")


@app.get("/export/csv/invoices")
def export_invoices_csv(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
    rows = [(i.number, i.customer.name if i.customer else "Walk-in", i.payment_method,
             i.subtotal, i.discount, i.tax_total, i.total, i.date.strftime("%Y-%m-%d %H:%M"),
             "Refunded" if i.refunded else ("Part returned" if i.returns else "Paid")) for i in db.query(Invoice).order_by(Invoice.date).all()]
    return _csv_response(rows, ["Number", "Customer", "Payment", "Subtotal", "Discount", "Tax", "Total", "Date", "Status"], "invoices.csv")


@app.get("/export/csv/tax-report")
def export_tax_report_csv(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
    rows = [(i.number, i.date.strftime("%Y-%m-%d"), i.subtotal, i.discount, i.tax_total, i.total)
            for i in db.query(Invoice).filter(Invoice.refunded == False).order_by(Invoice.date).all()]  # noqa: E712
    # v52: items taken back from a sale are their own negative lines, dated the day they were paid back
    rows += [(r.number, to_local(r.refunded_at, get_shop_tz(db)).strftime("%Y-%m-%d"), -r.subtotal, -r.discount, -r.tax_total, -r.total)
             for r in db.query(SaleReturn).order_by(SaleReturn.refunded_at).all()]
    return _csv_response(rows, ["Invoice", "Date", "Subtotal", "Discount", "Tax Collected", "Total"], "tax_report.csv")


# Bold, slightly larger header rows for every export sheet — small touch,
# but a workbook that gets handed to an accountant/bookkeeper reads a lot
# better with headers that are visually distinct from data rows.
_XLSX_HEADER_FONT = Font(bold=True)


def _autosize_columns(ws, widths):
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width


def _write_header(ws, headers):
    ws.append(headers)
    for cell in ws[ws.max_row]:
        cell.font = _XLSX_HEADER_FONT


@app.get("/export/xlsx/month-end")
def export_month_end_xlsx(request: Request, month: str = "", db: Session = Depends(get_db)):
    """Month-end workbook for handing off to a bookkeeper/accountant:
    every invoice for the selected month with its tax breakdown, a top
    products sheet, and a tax-summary-by-rate sheet lined up with the
    same CRA filing-period logic the Reports page already uses. Defaults
    to the current month; pass ?month=YYYY-MM for any other period."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)

    tz = get_shop_tz(db)
    if not month:
        month = _local_month_str(db)
    try:
        year, mon = int(month[:4]), int(month[5:7])
        if not 1 <= mon <= 12:
            raise ValueError
    except (ValueError, IndexError):
        return HTMLResponse("Invalid month — expected format YYYY-MM.", status_code=400)
    _first, _last = month_bounds(year, mon)
    _lo, _hi = day_bounds_utc(_first, _last, tz)

    invoices = (
        db.query(Invoice).options(selectinload(Invoice.lines), selectinload(Invoice.customer))
        .filter(Invoice.date >= _lo, Invoice.date < _hi)
        .order_by(Invoice.date)
        .all()
    )

    wb = Workbook()

    # ── Sheet 1: Invoices ──────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Invoices"
    _write_header(ws1, ["Invoice #", "Date", "Customer", "Payment Method",
                         "Subtotal", "Discount", "Tax Detail", "Tax Total", "Total", "Status"])
    for inv in invoices:
        try:
            tax_lines = json.loads(inv.tax_breakdown or "[]")
            tax_detail = "; ".join(f"{t['label']}: ${t['amount']:.2f}" for t in tax_lines)
        except (ValueError, KeyError, TypeError):
            tax_detail = ""
        ws1.append([
            inv.number,
            to_local(inv.date, tz).strftime("%Y-%m-%d %H:%M"),
            inv.customer.name if inv.customer else "Walk-in",
            inv.payment_method,
            float(inv.subtotal),
            float(inv.discount),
            tax_detail,
            float(inv.tax_total),
            float(inv.total),
            "Refunded" if inv.refunded else ("Part returned" if inv.returns else "Paid"),
        ])
    _autosize_columns(ws1, [14, 18, 20, 16, 12, 12, 34, 12, 12, 12])
    ws1.freeze_panes = "A2"

    # ── Sheet 2: Top Products (by qty sold, this month) ─────────────
    prod_sales = {}
    for inv in invoices:
        if inv.refunded:
            continue
        for line in inv.lines:
            key = line.product_id or line.name
            entry = prod_sales.setdefault(key, {"name": line.name, "qty": 0, "revenue": 0.0})
            entry["qty"] += line.qty
            entry["revenue"] += line.price * line.qty
    top_products = sorted(prod_sales.values(), key=lambda p: p["qty"], reverse=True)

    ws2 = wb.create_sheet("Top Products")
    _write_header(ws2, ["Product", "Qty Sold", "Revenue"])
    for p in top_products:
        ws2.append([p["name"], p["qty"], round(p["revenue"], 2)])
    _autosize_columns(ws2, [34, 12, 14])
    ws2.freeze_panes = "A2"

    # ── Sheet 3: Tax Summary (by rate/label, non-refunded only) ─────
    tax_summary = {}
    for inv in invoices:
        if inv.refunded:
            continue
        try:
            for line in json.loads(inv.tax_breakdown or "[]"):
                tax_summary[line["label"]] = tax_summary.get(line["label"], 0) + line["amount"]
        except (ValueError, KeyError, TypeError):
            continue

    month_returns = _returns.in_window(db, _lo, _hi)         # v52: items taken back from sales this month
    for r in month_returns:
        try:
            for line in json.loads(r.tax_breakdown or "[]"):
                tax_summary[line["label"]] = tax_summary.get(line["label"], 0) - line["amount"]
        except (ValueError, KeyError, TypeError):
            continue

    ws3 = wb.create_sheet("Tax Summary")
    _write_header(ws3, ["Tax", "Amount Collected (net of returns)"])
    for label, amount in sorted(tax_summary.items()):
        ws3.append([label, round(amount, 2)])
    ws3.append(["Total", round(sum(tax_summary.values()), 2)])
    ws3[ws3.max_row][0].font = _XLSX_HEADER_FONT
    ws3[ws3.max_row][1].font = _XLSX_HEADER_FONT
    _autosize_columns(ws3, [24, 18])

    # ── Sheet 4: Trade-Ins ───────────────────────────────────────────
    # Deliberately its own sheet rather than folded into Invoices — a
    # trade-in isn't a sale (see the TradeIn model's own docstring), and
    # mixing it into revenue rows would misrepresent both. Still real
    # money (or a real future liability), so a bookkeeper needs to see it
    # somewhere — this is that somewhere.
    trade_ins = (
        db.query(TradeIn)
        .filter(TradeIn.created_at >= _lo)
        .filter(TradeIn.created_at < _hi)
        .order_by(TradeIn.created_at)
        .all()
    )
    ws4 = wb.create_sheet("Trade-Ins")
    _write_header(ws4, ["Number", "Date", "Customer", "Device", "Condition",
                         "Amount", "Payout Method", "Status", "Staff"])
    for t in trade_ins:
        ws4.append([
            t.number or t.id[:8].upper(),
            to_local(t.created_at, tz).strftime("%Y-%m-%d %H:%M"),
            t.customer.name if t.customer else "Walk-in",
            t.device,
            t.condition,
            float(t.offered_amount),
            "Store Credit" if t.payout_method == "store_credit" else "Cash",
            t.status.title(),
            t.staff_name,
        ])
    if trade_ins:
        cash_total = sum(t.offered_amount for t in trade_ins if t.payout_method == "cash")
        credit_total = sum(t.offered_amount for t in trade_ins if t.payout_method == "store_credit")
        ws4.append([])
        ws4.append(["Cash Paid Out", "", "", "", "", round(cash_total, 2)])
        ws4[ws4.max_row][0].font = _XLSX_HEADER_FONT
        ws4.append(["Store Credit Issued", "", "", "", "", round(credit_total, 2)])
        ws4[ws4.max_row][0].font = _XLSX_HEADER_FONT
    _autosize_columns(ws4, [12, 18, 20, 24, 28, 12, 14, 12, 16])
    ws4.freeze_panes = "A2"

    # ── Sheet 5: Returns (v52) ───────────────────────────────────────
    ws5 = wb.create_sheet("Returns")
    _write_header(ws5, ["Return #", "Paid back on", "Customer", "Items", "Subtotal", "Discount", "Tax", "Total", "Paid back via", "Reason", "By"])
    for r in month_returns:
        ws5.append([r.number, to_local(r.refunded_at, tz).strftime("%Y-%m-%d %H:%M"), r.customer.name if r.customer else "Walk-in",
                    ", ".join(f"{l.qty}x {l.name}" for l in r.lines), float(r.subtotal), float(r.discount), float(r.tax_total),
                    float(r.total), r.refund_method or "", r.refund_reason or "", r.refunded_by or ""])
    if month_returns:
        ws5.append([])
        ws5.append(["Total returned", "", "", "", "", "", "", round(sum(r.total for r in month_returns), 2)])
        ws5[ws5.max_row][0].font = _XLSX_HEADER_FONT
    _autosize_columns(ws5, [14, 18, 20, 34, 12, 12, 10, 12, 14, 24, 14])
    ws5.freeze_panes = "A2"

    # ── Sheet 6: Expenses (v52) ──────────────────────────────────────
    ws6 = wb.create_sheet("Expenses")
    _write_header(ws6, ["Date", "Category", "Paid to", "What for", "How paid", "Receipt #", "Amount", "Tax in it", "Entered by"])
    month_expenses = _expenses_mod.in_window(db, _lo, _hi)
    for x in month_expenses:
        ws6.append([to_local(x.spent_at, tz).strftime("%Y-%m-%d %H:%M"), x.category, x.payee, x.description,
                    _expenses_mod.PAID_FROM.get(x.paid_from, x.paid_from), x.receipt_ref, float(x.amount), float(x.tax_paid or 0), x.staff_name])
    if month_expenses:
        biz = [x for x in month_expenses if x.category not in _expenses_mod.NOT_AN_EXPENSE]
        ws6.append([])
        ws6.append(["Business expenses", "", "", "", "", "", round(sum(x.amount for x in biz), 2), round(sum(x.tax_paid or 0 for x in biz), 2)])
        ws6[ws6.max_row][0].font = _XLSX_HEADER_FONT
    _autosize_columns(ws6, [18, 20, 20, 34, 24, 12, 12, 12, 16])
    ws6.freeze_panes = "A2"

    # Nothing in this workbook is a formula (totals are worked out above), so any cell that came out formula-typed is text
    # somebody typed ("=HYPERLINK(...)" as a customer name): keep it as plain text.
    for _ws in wb.worksheets:
        for _row in _ws.iter_rows():
            for _c in _row:
                if _c.data_type == "f":
                    _c.data_type = "s"
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    add_audit(db, staff, "EXPORT", f"Month-end workbook exported for {month}")
    db.commit()

    filename = f"month_end_{month}.xlsx"
    return Response(
        content=out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── AUDIT LOG ────────────────────────────────────────────────────────
@app.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — audit log requires manager or owner role.", status_code=403)
    logs = db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(200).all()
    return templates.TemplateResponse(request, "audit.html", {"staff": staff, "logs": logs})
