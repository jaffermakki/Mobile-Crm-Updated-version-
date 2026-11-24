"""
v52: partial returns — the web side.

  GET  /invoices/{id}/return             pick the items and how many to take back (owner / manager)
  POST /invoices/{id}/return/preview     show exactly what will be refunded, before anything is saved
  POST /invoices/{id}/return             save it (a one-time code from the preview stops a double click saving twice)
  GET  /returns/{id}                     printable return receipt (any signed-in staff)
"""
from __future__ import annotations

import json
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import returns as R
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import Invoice, SaleReturn
from .money import cash_rounding_on


def build_router(templates, require_login, get_db, get_shop_info, get_setting) -> APIRouter:
    router = APIRouter()

    def _can_manage(staff) -> bool:
        return role_allowed(staff, "owner", "manager")

    def _picks(form) -> dict:
        out = {}
        for k, v in form.items():
            m = re.fullmatch(r"qty_([A-Za-z0-9_\-]{1,40})", k)
            if m:
                s = str(v).strip()
                out[m.group(1)] = int(s) if re.fullmatch(r"\d{1,6}", s) else (0 if s == "" else -1)
        return out

    def _shelf(form, picks) -> dict:
        return {lid: (f"shelf_{lid}" in form) for lid in picks}

    def _back(request, invoice_id, colour, message):
        request.session["flash"] = (colour, message)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    def _guard(request, db, invoice_id):
        """(staff, invoice, response) — response is set when the visitor must be sent elsewhere."""
        staff = require_login(request, db)
        if not staff:
            return None, None, RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return staff, None, HTMLResponse("Forbidden — returns need a manager or owner.", status_code=403)
        invoice = db.get(Invoice, invoice_id)
        if not invoice:
            return staff, None, RedirectResponse("/invoices", status_code=303)
        return staff, invoice, None

    def _form_page(request, db, staff, invoice, picks=None, shelf=None, method="", reason="", error="", status=200):
        picks, shelf = picks or {}, shelf or {}
        return templates.TemplateResponse(request, "return_form.html", {
            "staff": staff, "invoice": invoice, "rows": R.line_status(invoice), "picks": picks, "shelf": shelf,
            "method": method, "reason": reason, "error": error, "methods": R.REFUND_METHODS,
            "has_customer": bool(invoice.customer_id), "rounding": cash_rounding_on(db),
        }, status_code=status)

    @router.get("/invoices/{invoice_id}/return", response_class=HTMLResponse)
    def return_form(request: Request, invoice_id: str, db: Session = Depends(get_db)):
        staff, invoice, resp = _guard(request, db, invoice_id)
        if resp:
            return resp
        problem = R.can_start(invoice)
        if problem:
            return _back(request, invoice_id, "red", problem)
        return _form_page(request, db, staff, invoice)

    @router.post("/invoices/{invoice_id}/return/preview", response_class=HTMLResponse)
    async def return_preview(request: Request, invoice_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, invoice, resp = _guard(request, db, invoice_id)
        if resp:
            return resp
        form = await request.form()
        picks = _picks(form)
        shelf = _shelf(form, picks)
        method = str(form.get("method", ""))
        reason = str(form.get("reason", "")).strip()[:R.MAX_REASON]
        problem = R.can_start(invoice)
        if problem:
            return _back(request, invoice_id, "red", problem)
        if method not in R.REFUND_METHODS:
            return _form_page(request, db, staff, invoice, picks, shelf, method, reason, "Choose how the money goes back to the customer.", 400)
        if method == "Store Credit" and not invoice.customer_id:
            return _form_page(request, db, staff, invoice, picks, shelf, method, reason,
                              "Store credit needs a customer on the invoice — choose another way to pay it back.", 400)
        try:
            p = R.plan(invoice, {k: v for k, v in picks.items()})
        except R.ReturnError as e:
            return _form_page(request, db, staff, invoice, picks, shelf, method, reason, str(e), 400)
        nonce = secrets.token_urlsafe(12)
        request.session[f"ret_nonce:{invoice_id}"] = nonce
        from .money import cash_refund_amount
        cash_out = cash_refund_amount(db, p["total"]) if method == "Cash" else None
        return templates.TemplateResponse(request, "return_preview.html", {
            "staff": staff, "invoice": invoice, "plan": p, "picks": {l["line"].id: l["qty"] for l in p["lines"]},
            "shelf": shelf, "method": method, "reason": reason, "nonce": nonce, "cash_out": cash_out,
            "customer": invoice.customer,
        })

    @router.post("/invoices/{invoice_id}/return", response_class=HTMLResponse)
    async def return_save(request: Request, invoice_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, invoice, resp = _guard(request, db, invoice_id)
        if resp:
            return resp
        form = await request.form()
        nonce = str(form.get("nonce", ""))
        expected = request.session.get(f"ret_nonce:{invoice_id}")
        if not nonce or not expected or not secrets.compare_digest(nonce, str(expected)):
            return _back(request, invoice_id, "amber",
                         "That return was already saved (or the page was out of date), so nothing more was recorded. Check the list below.")
        picks = _picks(form)
        try:
            ret = R.record(db, staff, invoice, picks, _shelf(form, picks), str(form.get("method", "")), str(form.get("reason", "")))
        except R.ReturnError as e:
            db.rollback()
            return _back(request, invoice_id, "red", str(e))
        request.session.pop(f"ret_nonce:{invoice_id}", None)
        add_audit(db, staff, "RETURN", R.describe(ret) + (f" — {ret.refund_reason}" if ret.refund_reason else ""))
        if ret.refund_method == "Store Credit" and invoice.customer:
            add_audit(db, staff, "STORE_CREDIT", f"Issued ${ret.total:.2f} store credit (return {ret.number}) to {invoice.customer.name}")
        db.commit()
        if ret.refund_method == "Cash":
            note = f" (${ret.total:.2f} rounded to the nearest 5¢)" if abs(ret.cash_paid - ret.total) > 0.001 else ""
            msg = f"Return {ret.number} recorded — hand back ${ret.cash_paid:.2f} in cash{note}."
        elif ret.refund_method == "Card":
            msg = f"Return {ret.number} recorded — refund ${ret.total:.2f} to the customer's card on the terminal."
        elif ret.refund_method == "Store Credit":
            msg = f"Return {ret.number} recorded — ${ret.total:.2f} added to {invoice.customer.name}'s store credit."
        else:
            msg = f"Return {ret.number} recorded — send ${ret.total:.2f} to the customer by E-Transfer."
        request.session["flash"] = ("green", msg)
        request.session["return_just_saved"] = ret.id
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @router.get("/returns/{return_id}", response_class=HTMLResponse)
    def return_receipt(request: Request, return_id: str, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        ret = db.get(SaleReturn, return_id)
        if not ret:
            return RedirectResponse("/invoices", status_code=303)
        try:
            tax_lines = json.loads(ret.tax_breakdown or "[]")
        except (ValueError, TypeError):
            tax_lines = []
        return templates.TemplateResponse(request, "return_receipt.html", {
            "staff": staff, "ret": ret, "invoice": ret.invoice, "shop": get_shop_info(db), "tax_lines": tax_lines,
        })

    return router
