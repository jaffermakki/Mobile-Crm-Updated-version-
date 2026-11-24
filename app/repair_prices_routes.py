"""
v54: the repair price menu: the web side.

  GET  /repair-prices                  the list (owner edits, manager can look)
  POST /repair-prices/add              add a line (owner)
  POST /repair-prices/{id}/save        change a line (owner)
  POST /repair-prices/{id}/delete      remove a line (owner)
  POST /repair-prices/import           add/update many lines from a CSV file (owner)
  GET  /repair-prices/export.csv       the whole list as a CSV file (owner / manager)
  GET  /repair-menu?device=...         JSON: the lines that apply to that phone; the new-ticket form uses it (any signed-in staff)

The paths start with /repair- (not /repairs/) so they can never be mistaken for a ticket id.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from urllib.parse import quote

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import repair_prices as RPR
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import Product, RepairPrice
from .repair_parts import REPAIR_CATEGORIES
from .repairs_const import ISSUE_TYPES
from .report_types import _safe_cell

SHOW_LIMIT = 300


def build_router(templates, require_login, get_db, custom_models) -> APIRouter:
    router = APIRouter()

    def _back(request, colour, message, anchor=""):
        request.session["flash"] = (colour, message)
        return RedirectResponse("/repair-prices" + anchor, status_code=303)

    def _gate(request, db, edit=False):
        """(staff, response). Response is set when the visitor must be sent elsewhere."""
        staff = require_login(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if edit and not role_allowed(staff, "owner"):
            return staff, HTMLResponse("Forbidden — only the owner can change the repair prices.", status_code=403)
        if not edit and not role_allowed(staff, "owner", "manager"):
            return staff, HTMLResponse("Forbidden — the repair price list is for the owner and managers.", status_code=403)
        return staff, None

    def _money(x) -> str:
        return f"${float(x or 0):,.2f}"

    def _page(request, db, staff, q="", import_result=None, status=200):
        from . import devices as _devices
        query = db.query(RepairPrice)
        q = RPR.clean_text(q, 60)
        if q:
            like = f"%{q.lower()}%"
            query = query.filter(or_(func.lower(RepairPrice.model).like(like), func.lower(RepairPrice.repair).like(like)))
        total_matching = query.count()
        rows = query.order_by(RepairPrice.model_key, RepairPrice.repair_key).limit(SHOW_LIMIT).all()
        groups, index = [], {}
        for r in rows:
            g = index.get(r.model_key)
            if g is None:
                g = {"title": r.model or RPR.ANY_PHONE_LABEL, "any": not r.model_key, "lines": []}
                index[r.model_key] = g
                groups.append(g)
            g["lines"].append({"row": r, "part": RPR.part_info(db, r.product_id)})
        groups.sort(key=lambda g: (not g["any"], g["title"].lower()))
        parts = db.query(Product).filter(Product.category.in_(REPAIR_CATEGORIES)).order_by(Product.name).limit(2000).all()
        return templates.TemplateResponse(request, "repair_prices.html", {
            "staff": staff, "flash": request.session.pop("flash", None), "can_edit": role_allowed(staff, "owner"),
            "groups": groups, "q": q, "total": RPR.count_lines(db), "total_matching": total_matching, "shown": len(rows),
            "show_limit": SHOW_LIMIT, "repair_choices": list(ISSUE_TYPES) + RPR.repair_names(db), "parts": parts,
            "import_result": import_result, "device_catalog": _devices.payload(custom_models(db)),
            "max_lines": RPR.MAX_LINES, "max_minutes": RPR.MAX_MINUTES,
        }, status_code=status)

    @router.get("/repair-prices", response_class=HTMLResponse)
    def prices_page(request: Request, q: str = "", db: Session = Depends(get_db)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        return _page(request, db, staff, q)

    @router.post("/repair-prices/add")
    def prices_add(request: Request, device: str = Form(""), model_any: str = Form(""), repair: str = Form(""),
                   price: str = Form(""), sku: str = Form(""), minutes: str = Form(""),
                   db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db, edit=True)
        if resp:
            return resp
        model = "" if model_any else device
        if not model_any and not RPR.clean_text(device, RPR.MAX_MODEL + 1):
            return _back(request, "red", "Pick the phone first (or use the “Same price on every phone” box).")
        try:
            line = RPR.clean_line(db, model, repair, price, sku, minutes)
            row = RPR.save_new(db, line, staff.name)
        except RPR.PriceError as e:
            db.rollback()
            return _back(request, "red", f"Not added: {e}")
        add_audit(db, staff, "PRICE_ADD", f"{row.model or RPR.ANY_PHONE_LABEL} · {row.repair}: {_money(row.price)}")
        return _back(request, "green", f"Added {row.model or RPR.ANY_PHONE_LABEL} · {row.repair} at {_money(row.price)}.")

    @router.post("/repair-prices/{line_id}/save")
    def prices_save(request: Request, line_id: str, model: str = Form(""), repair: str = Form(""), price: str = Form(""),
                    sku: str = Form(""), minutes: str = Form(""), q: str = Form(""),
                    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db, edit=True)
        if resp:
            return resp
        row = db.get(RepairPrice, line_id)
        if not row:
            return _back(request, "red", "That price line no longer exists.")
        old_price, label = float(row.price or 0), f"{row.model or RPR.ANY_PHONE_LABEL} · {row.repair}"
        try:
            line = RPR.clean_line(db, row.model, repair, price, sku, minutes)     # the phone is fixed: change it by delete + add
            RPR.update(db, row, line, staff.name)
        except RPR.PriceError as e:
            db.rollback()
            return _back(request, "red", f"Not saved ({label}): {e}")
        detail = f"{row.model or RPR.ANY_PHONE_LABEL} · {row.repair}: " + (
            f"{_money(old_price)} → {_money(row.price)}" if round(old_price, 2) != round(float(row.price), 2) else f"{_money(row.price)} (other details changed)")
        add_audit(db, staff, "PRICE_CHANGE", detail)
        suffix = f"?q={quote(RPR.clean_text(q, 60))}" if RPR.clean_text(q, 60) else ""
        request.session["flash"] = ("green", f"Saved {row.model or RPR.ANY_PHONE_LABEL} · {row.repair} at {_money(row.price)}.")
        return RedirectResponse("/repair-prices" + suffix, status_code=303)

    @router.post("/repair-prices/{line_id}/delete")
    def prices_delete(request: Request, line_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db, edit=True)
        if resp:
            return resp
        row = db.get(RepairPrice, line_id)
        if not row:
            return _back(request, "red", "That price line no longer exists.")
        label = f"{row.model or RPR.ANY_PHONE_LABEL} · {row.repair}"
        detail = f"{label}: {_money(row.price)}"
        db.delete(row)
        db.commit()
        add_audit(db, staff, "PRICE_DELETE", detail)
        return _back(request, "green", f"Removed {label}. Tickets already made are not changed.")

    @router.post("/repair-prices/import", response_class=HTMLResponse)
    async def prices_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                            _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db, edit=True)
        if resp:
            return resp
        raw = await file.read(RPR.MAX_IMPORT_BYTES + 1)
        rows, problem = RPR.parse_csv(raw)
        if problem:
            request.session["flash"] = ("red", problem)
            return _page(request, db, staff, status=400)
        if len(rows) > RPR.MAX_LINES:
            request.session["flash"] = ("red", f"That file has {len(rows)} lines; the most one file can hold is {RPR.MAX_LINES}.")
            return _page(request, db, staff, status=400)
        result = RPR.import_rows(db, rows, staff.name)
        db.commit()
        add_audit(db, staff, "PRICE_IMPORT", f"Price list import: {result['added']} added, {result['updated']} updated, {len(result['skipped'])} skipped")
        request.session["flash"] = ("green" if not result["skipped"] else "amber",
                                    f"Import finished: {result['added']} added, {result['updated']} updated, {len(result['skipped'])} skipped.")
        return _page(request, db, staff, import_result=result)

    @router.get("/repair-prices/export.csv")
    def prices_export(request: Request, db: Session = Depends(get_db)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(RPR.CSV_HEADERS)
        w.writerows([tuple(_safe_cell(c) for c in r) for r in RPR.to_csv_rows(db)])
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="repair-prices.csv"'})

    @router.get("/repair-menu")
    def repair_menu(request: Request, device: str = "", db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return JSONResponse({"ok": False, "error": "Sign in again."}, status_code=401)
        device = RPR.clean_text(device, 120)
        items = RPR.as_json(db, RPR.menu_for_device(db, device)) if device else []
        return JSONResponse({"ok": True, "device": device, "items": items},
                            headers={"Cache-Control": "no-store"})

    return router
