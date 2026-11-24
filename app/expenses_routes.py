"""
v52: expenses and till paid-outs — the web side.

  GET  /expenses                 the list (owner / manager: any dates; everyone else: what they entered today)
  GET  /expenses/new             the form (any signed-in staff)
  POST /expenses                 save one entry
  POST /expenses/{id}/void       cancel an entry, keeping it on file with who and why (owner / manager)

Anyone behind the counter can take cash out of the till and say so — hiding that is worse than allowing it — but
only a manager or the owner can back-date an entry or void one, and every save and void goes in the audit log.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import expenses as X
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .localtime import day_bounds_utc, day_start_utc, get_shop_tz, local_today, parse_date, to_local_date
from .models import CashSession, Expense
from .reporting import RANGE_PRESETS, preset_range

MAX_BACKDATE_DAYS = 400


def build_router(templates, require_login, get_db) -> APIRouter:
    router = APIRouter()

    def _is_manager(staff) -> bool:
        return role_allowed(staff, "owner", "manager")

    def _form_ctx(db, staff, values=None, error=""):
        tz = get_shop_tz(db)
        return {"staff": staff, "categories": X.CATEGORIES, "not_expense": X.NOT_AN_EXPENSE, "paid_from": X.PAID_FROM,
                "v": values or {}, "error": error, "today": local_today(tz).isoformat(), "can_backdate": _is_manager(staff)}

    def _when(db, staff, raw_date: str):
        """The moment to store: now for today, local noon for a back-dated day (managers only)."""
        tz = get_shop_tz(db)
        today = local_today(tz)
        raw = (raw_date or "").strip()
        if not raw or not _is_manager(staff):
            return datetime.utcnow()
        d = parse_date(raw, None) if raw else today
        if d is None:
            raise X.ExpenseError("That date isn't valid.")
        if d > today:
            raise X.ExpenseError("The date can't be in the future.")
        if (today - d).days > MAX_BACKDATE_DAYS:
            raise X.ExpenseError("That date is too far back.")
        if d == today:
            return datetime.utcnow()
        return day_start_utc(d, tz) + timedelta(hours=12)

    @router.get("/expenses/new", response_class=HTMLResponse)
    def expense_new(request: Request, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "expense_form.html", _form_ctx(db, staff, {"paid_from": "drawer"}))

    @router.post("/expenses", response_class=HTMLResponse)
    def expense_save(request: Request, amount: str = Form(""), tax_paid: str = Form(""), category: str = Form(""),
                     description: str = Form(""), payee: str = Form(""), receipt_ref: str = Form(""),
                     paid_from: str = Form("drawer"), spent_on: str = Form(""),
                     db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        raw = {"amount": amount, "tax_paid": tax_paid, "category": category, "description": description, "payee": payee,
               "receipt_ref": receipt_ref, "paid_from": paid_from, "spent_on": spent_on}
        try:
            values = X.validate(amount, tax_paid, category, description, payee, receipt_ref, paid_from)
            when = _when(db, staff, spent_on)
        except X.ExpenseError as e:
            return templates.TemplateResponse(request, "expense_form.html", _form_ctx(db, staff, raw, str(e)), status_code=400)
        e = X.record(db, staff, values, when)
        tz = get_shop_tz(db)
        day = to_local_date(e.spent_at, tz)
        add_audit(db, staff, "EXPENSE", X.describe(e) + f" — {day.isoformat()}")   # add_audit commits
        msg = f"Recorded ${e.amount:.2f} — {e.category}."
        colour = "green"
        if e.paid_from == "drawer":
            closed = db.query(CashSession).filter(CashSession.date == day.isoformat()).first()
            if closed is not None:
                msg += (f" Cash Up for {day.isoformat()} was already closed, so its saved “expected” figure doesn't include this —"
                        " count the drawer and close Cash Up again.")
                colour = "amber"
            else:
                msg += " Cash Up will now expect that much less in the drawer."
        request.session["flash"] = (colour, msg)
        return RedirectResponse("/expenses", status_code=303)

    @router.get("/expenses", response_class=HTMLResponse)
    def expense_list(request: Request, preset: str = "", date_from: str = "", date_to: str = "", db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        tz = get_shop_tz(db)
        today = local_today(tz)
        manager = _is_manager(staff)
        if manager:
            if preset in dict(RANGE_PRESETS) and not (date_from and date_to):
                d1, d2 = preset_range(preset, today)
            elif date_from or date_to:
                d1 = parse_date(date_from, today.replace(day=1))
                d2 = parse_date(date_to, today)
            else:
                d1, d2 = preset_range("this_month", today)
            if d2 < d1:
                d1, d2 = d2, d1
            if (d2 - d1).days > 732:
                d1 = d2 - timedelta(days=732)
        else:
            d1 = d2 = today
        s, e = day_bounds_utc(d1, d2, tz)
        rows = X.in_window(db, s, e, include_voided=True)
        if not manager:
            rows = [r for r in rows if r.staff_id == staff.id]
        live = [r for r in rows if not r.voided]
        biz = [r for r in live if r.category not in X.NOT_AN_EXPENSE]
        totals = {
            "business": round(sum(r.amount or 0 for r in biz), 2), "tax": round(sum(r.tax_paid or 0 for r in biz), 2),
            "till": round(sum(r.amount or 0 for r in live if r.paid_from == "drawer"), 2),
            "other": round(sum(r.amount or 0 for r in live if r.paid_from != "drawer"), 2),
        }
        return templates.TemplateResponse(request, "expenses.html", {
            "staff": staff, "rows": rows, "totals": totals, "d1": d1.isoformat(), "d2": d2.isoformat(), "manager": manager,
            "presets": RANGE_PRESETS, "preset": preset, "tz": tz, "paid_from": X.PAID_FROM, "by_cat": X.by_category(live),
            "not_expense": X.NOT_AN_EXPENSE,
            "flash": request.session.pop("flash", None),
        })

    @router.post("/expenses/{expense_id}/void")
    def expense_void(request: Request, expense_id: str, reason: str = Form(""),
                     db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not _is_manager(staff):
            return HTMLResponse("Forbidden — voiding an expense needs a manager or owner.", status_code=403)
        row = db.get(Expense, expense_id)
        if row is None:
            request.session["flash"] = ("red", "That entry no longer exists.")
            return RedirectResponse("/expenses", status_code=303)
        try:
            X.void(db, staff, row, reason)
        except X.ExpenseError as err:
            request.session["flash"] = ("red", str(err))
            return RedirectResponse("/expenses", status_code=303)
        add_audit(db, staff, "EXPENSE_VOID", f"Voided {X.describe(row)} — reason: {row.void_reason}")
        request.session["flash"] = ("green", f"Voided ${row.amount:.2f} ({row.category}). It stays on file but no longer counts.")
        return RedirectResponse("/expenses", status_code=303)

    return router
