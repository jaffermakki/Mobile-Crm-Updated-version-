"""
v50: camera/barcode scanning and stock-count mode — the web side.

  GET  /products/search.json?q=                  quick product search for the scanner's "link this barcode" step
  POST /products/{id}/barcode                    put a manufacturer barcode on a product (owner / manager)
  GET  /stock-count                              all counts; start a new one
  POST /stock-count/start
  GET  /stock-count/{id}                         the counting screen (read-only once applied or cancelled)
  POST /stock-count/{id}/scan                    count one scanned / typed code (JSON)
  POST /stock-count/{id}/line                    +1 / -1 / set an exact quantity for a product (JSON)
  POST /stock-count/{id}/remove                  take a product out of the count
  GET  /stock-count/{id}/review                  what would change, before anything does
  POST /stock-count/{id}/reasons                 v51, owner / manager: why each difference happened (damaged, lost, ...)
  POST /stock-count/{id}/apply                   owner / manager: write the counts into the stock
  POST /stock-count/{id}/cancel                  owner / manager: throw the count away
  GET  /stock-count/{id}/export.csv              the counted lines as a spreadsheet (owner / manager)
  GET  /purchase-orders/{id}/scan.json?code=     v51: which line of this order a scanned code belongs to (owner / manager)

Anybody signed in can count; only an owner or manager can change the stock (apply) or give a product a barcode.
"""
from __future__ import annotations

import csv
import io
import re

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import barcodes as BC
from . import stockcount as SC
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import Product, PurchaseOrder, StockCount, StockCountLine
from .report_types import _safe_cell


def build_router(templates, require_login, get_db, category_labels) -> APIRouter:
    router = APIRouter()

    def _json(status: int, **body) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    def _can_manage(staff) -> bool:
        return role_allowed(staff, "owner", "manager")

    def _product_dict(p: Product) -> dict:
        return {"id": p.id, "sku": p.sku or "", "name": p.name or "", "barcode": p.barcode or "", "stock": int(p.stock or 0)}

    # ── helpers for the scanner on any page ──
    @router.get("/products/search.json")
    def product_search(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
        if not require_login(request, db):
            return _json(401, ok=False, error="Signed out.")
        q = " ".join(q.split())[:60]
        if len(q) < 2:
            return _json(200, ok=True, products=[])
        like = f"%{q}%"
        rows = (db.query(Product).filter(or_(Product.name.ilike(like), Product.sku.ilike(like), Product.phone_model.ilike(like),
                                              Product.variant_group.ilike(like), Product.color.ilike(like),
                                              Product.barcode.in_(BC.variants(q) or [q])))
                .order_by(Product.name).limit(20).all())
        return _json(200, ok=True, products=[_product_dict(p) for p in rows])

    @router.post("/products/{product_id}/barcode")
    def link_barcode(request: Request, product_id: str, barcode: str = Form(""), db: Session = Depends(get_db),
                     _csrf: None = Depends(csrf_protect)):
        ajax = request.headers.get("x-scan-ajax") == "1"
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="Signed out.") if ajax else RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return _json(403, ok=False, error="Only the owner or a manager can give a product a barcode.") if ajax \
                else HTMLResponse("Forbidden — only the owner or a manager can give a product a barcode.", status_code=403)
        product = db.get(Product, product_id)
        code = BC.clean(barcode)

        def fail(message: str, status: int = 400):
            if ajax:
                return _json(status, ok=False, error=message)
            request.session["flash"] = ("red", message)
            return RedirectResponse(f"/products/{product_id}/edit", status_code=303)

        if not product:
            return fail("That product no longer exists.", 404)
        if not code:
            return fail("Scan or type a barcode first.")
        clash = BC.conflict(db, code, product.id)
        if clash:
            return fail(f"The barcode {code} is already on “{clash.name}” ({clash.sku}).", 409)
        product.barcode = code
        add_audit(db, staff, "PRODUCT_EDIT", f"Barcode {code} linked to {product.name}")
        db.commit()
        warn = BC.check_digit_ok(code) is False
        if ajax:
            return _json(200, ok=True, product=_product_dict(product), warning="That number's check digit looks wrong — make sure it was read correctly." if warn else "")
        request.session["flash"] = ("green" if not warn else "amber", f"Barcode saved for {product.name}." + (" The check digit looks wrong, so double-check it." if warn else ""))
        return RedirectResponse(f"/products/{product_id}/edit", status_code=303)

    # ── list / start ──
    @router.get("/stock-count", response_class=HTMLResponse)
    def stock_counts(request: Request, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        counts = db.query(StockCount).order_by(StockCount.created_at.desc()).limit(60).all()
        line_totals = dict(db.query(StockCountLine.count_id, func.count(StockCountLine.id)).group_by(StockCountLine.count_id).all())
        categories = [c for (c,) in db.query(Product.category).filter(Product.category.isnot(None), Product.category != "").distinct().order_by(Product.category).all()]
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(request, "stock_count_list.html", {
            "staff": staff, "flash": flash, "counts": counts, "line_totals": line_totals, "categories": categories,
            "category_labels": category_labels, "can_manage": _can_manage(staff),
        })

    @router.post("/stock-count/start")
    def stock_count_start(request: Request, name: str = Form(""), scope: str = Form(""), db: Session = Depends(get_db),
                          _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        count = SC.start(db, staff.name, name, scope)
        add_audit(db, staff, "STOCK_COUNT", f"Started count “{count.name}”" + (f" ({count.scope_category})" if count.scope_category else ""))
        db.commit()
        return RedirectResponse(f"/stock-count/{count.id}", status_code=303)

    # ── the counting screen ──
    def _count_context(db: Session, count: StockCount) -> dict:
        lines = (db.query(StockCountLine).filter(StockCountLine.count_id == count.id)
                 .order_by(StockCountLine.updated_at.desc(), StockCountLine.id).all())
        return {"count": count, "lines": lines, "line_count": len(lines), "units": sum(int(l.counted) for l in lines)}

    @router.get("/stock-count/{count_id}", response_class=HTMLResponse)
    def stock_count_page(request: Request, count_id: str, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        count = db.get(StockCount, count_id)
        if not count:
            return RedirectResponse("/stock-count", status_code=303)
        flash = request.session.pop("flash", None)
        ctx = _count_context(db, count)
        rows = SC.review_rows(db, count) if count.status != "open" else []
        return templates.TemplateResponse(request, "stock_count.html", {
            "staff": staff, "flash": flash, "can_manage": _can_manage(staff), "category_labels": category_labels,
            "rows": rows, "summary": SC.summary(rows) if rows else None, **ctx,
        })

    # ── counting (JSON) ──
    @router.post("/stock-count/{count_id}/scan")
    def stock_count_scan(request: Request, count_id: str, code: str = Form(""), qty: int = Form(1),
                         db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="You were signed out. Sign in again, then continue.")
        count = db.get(StockCount, count_id)
        code = BC.clean(code)
        if not code or len(code) < 3:
            return _json(400, ok=False, error="That doesn't look like a barcode.")
        try:
            SC._require_open(count)
        except SC.CountError as e:
            return _json(409, ok=False, error=str(e))
        matches, how = BC.find_products(db, code)
        if not matches:
            return _json(200, ok=False, found=False, code=code, canLink=_can_manage(staff),
                         error=f"No product has the code {code}.")
        if len(matches) > 1:
            names = ", ".join(f"{m.name} ({m.sku})" for m in matches[:4])
            return _json(200, ok=False, found=True, code=code, error=f"{len(matches)} products share the code {code} ({names}). Add the right one by name.")
        product = matches[0]
        try:
            line = SC.add_to_line(db, count, product, max(-1000, min(1000, qty)), staff.name)
            db.commit()
        except SC.CountError as e:
            db.rollback()
            return _json(400, ok=False, error=str(e))
        outside = bool(count.scope_category) and (product.category or "") != count.scope_category
        return _json(200, ok=True, found=True, how=how, product=_product_dict(product) | {"stock": None}, counted=int(line.counted),
                     outside=outside, message=f"{product.name} — {int(line.counted)} counted" + (" (outside this count's category)" if outside else ""))

    @router.post("/stock-count/{count_id}/line")
    def stock_count_line(request: Request, count_id: str, product_id: str = Form(...), mode: str = Form("add"), qty: int = Form(1),
                         db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="You were signed out. Sign in again, then continue.")
        count = db.get(StockCount, count_id)
        product = db.get(Product, product_id)
        if not product:
            return _json(404, ok=False, error="That product no longer exists.")
        try:
            if mode == "set":
                line = SC.set_line(db, count, product, qty, staff.name)
            else:
                line = SC.add_to_line(db, count, product, max(-1000, min(1000, qty)), staff.name)
            db.commit()
        except SC.CountError as e:
            db.rollback()
            return _json(409 if "already" in str(e) else 400, ok=False, error=str(e))
        return _json(200, ok=True, product=_product_dict(product) | {"stock": None}, counted=int(line.counted),
                     message=f"{product.name} — {int(line.counted)} counted")

    @router.post("/stock-count/{count_id}/remove")
    def stock_count_remove(request: Request, count_id: str, product_id: str = Form(...), db: Session = Depends(get_db),
                           _csrf: None = Depends(csrf_protect)):
        ajax = request.headers.get("x-scan-ajax") == "1"
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="You were signed out. Sign in again, then continue.") if ajax else RedirectResponse("/login", status_code=303)
        count = db.get(StockCount, count_id)
        try:
            SC.remove_line(db, count, product_id)
            db.commit()
        except SC.CountError as e:
            db.rollback()
            if ajax:
                return _json(409, ok=False, error=str(e))
            request.session["flash"] = ("red", str(e))
        if ajax:
            return _json(200, ok=True, product_id=product_id)
        return RedirectResponse(f"/stock-count/{count_id}", status_code=303)

    # ── review / apply ──
    @router.get("/stock-count/{count_id}/review", response_class=HTMLResponse)
    def stock_count_review(request: Request, count_id: str, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        count = db.get(StockCount, count_id)
        if not count:
            return RedirectResponse("/stock-count", status_code=303)
        if count.status != "open":
            return RedirectResponse(f"/stock-count/{count_id}", status_code=303)
        rows = SC.review_rows(db, count)
        rest, rest_total = SC.uncounted(db, count, limit=200)
        zero_units = sum(int(p.stock or 0) for p in rest) if rest_total <= 200 else None
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(request, "stock_count_review.html", {
            "staff": staff, "flash": flash, "count": count, "rows": rows, "summary": SC.summary(rows), "uncounted": rest,
            "uncounted_total": rest_total, "uncounted_with_stock": [p for p in rest if (p.stock or 0) > 0],
            "zero_units": zero_units, "can_manage": _can_manage(staff), "category_labels": category_labels,
            "reason_choices": {r["product_id"]: SC.reasons_for(r["change"]) for r in rows if r["change"]},
        })

    def _posted_reasons(form) -> dict:
        """{product_id: reason key} from the review screen's reason_<product id> fields."""
        out = {}
        for key in form.keys():
            if key.startswith("reason_") and len(key) > 7:
                out[key[7:]] = str(form.get(key) or "")
        return out

    @router.post("/stock-count/{count_id}/reasons")
    async def stock_count_reasons(request: Request, count_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return HTMLResponse("Forbidden — only the owner or a manager can record reasons.", status_code=403)
        count = db.get(StockCount, count_id)
        if not count:
            return RedirectResponse("/stock-count", status_code=303)
        form = await request.form()
        try:
            n = SC.set_reasons(db, count, _posted_reasons(form))
            db.commit()
        except SC.CountError as e:
            db.rollback()
            request.session["flash"] = ("red", str(e))
            return RedirectResponse(f"/stock-count/{count_id}", status_code=303)
        request.session["flash"] = ("green", "Reasons saved." if n else "No reasons were changed.")
        return RedirectResponse(f"/stock-count/{count_id}/review", status_code=303)

    @router.post("/stock-count/{count_id}/apply")
    async def stock_count_apply(request: Request, count_id: str, db: Session = Depends(get_db),
                                _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return HTMLResponse("Forbidden — only the owner or a manager can change the stock from a count.", status_code=403)
        count = db.get(StockCount, count_id)
        if not count:
            return RedirectResponse("/stock-count", status_code=303)
        form = await request.form()
        zero_uncounted = str(form.get("zero_uncounted") or "")
        try:
            SC.set_reasons(db, count, _posted_reasons(form))
            result = SC.apply_count(db, count, staff.name, zero_uncounted=zero_uncounted in ("1", "true", "on", "yes"))
            add_audit(db, staff, "STOCK_COUNT",
                      f"Applied count “{count.name}”: {result['items_changed']} product(s) changed, net {result['units_net']:+d} units "
                      f"({result['value_net']:+.2f} at cost)" + (" — uncounted products set to zero" if count.zero_filled else ""))
            db.commit()
        except SC.CountError as e:
            db.rollback()
            request.session["flash"] = ("red", str(e))
            return RedirectResponse(f"/stock-count/{count_id}", status_code=303)
        request.session["flash"] = ("green", f"Stock updated: {result['items_changed']} product(s) changed, net {result['units_net']:+d} unit(s).")
        return RedirectResponse(f"/stock-count/{count_id}", status_code=303)

    @router.post("/stock-count/{count_id}/cancel")
    def stock_count_cancel(request: Request, count_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return HTMLResponse("Forbidden — only the owner or a manager can cancel a count.", status_code=403)
        count = db.get(StockCount, count_id)
        if count:
            try:
                SC.cancel_count(db, count, staff.name)
                add_audit(db, staff, "STOCK_COUNT", f"Cancelled count “{count.name}” (stock not changed)")
                db.commit()
            except SC.CountError as e:
                db.rollback()
                request.session["flash"] = ("red", str(e))
                return RedirectResponse(f"/stock-count/{count_id}", status_code=303)
        request.session["flash"] = ("green", "Count cancelled. The stock was not changed.")
        return RedirectResponse("/stock-count", status_code=303)

    @router.get("/stock-count/{count_id}/export.csv")
    def stock_count_csv(request: Request, count_id: str, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not _can_manage(staff):
            return HTMLResponse("Forbidden — exports require manager or owner role.", status_code=403)
        count = db.get(StockCount, count_id)
        if not count:
            return RedirectResponse("/stock-count", status_code=303)
        rows = SC.review_rows(db, count)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["SKU", "Name", "Counted", "System when counted", "Difference", "Stock changed by" if count.status == "applied" else "Would change by",
                    "Reason", "Unit cost", "Value at cost", "Counted by"])
        for r in rows:
            w.writerow([_safe_cell(r["sku"]), _safe_cell(r["name"]), r["counted"], r["at_count"], r["change"],
                        "" if r["will_change"] is None else r["will_change"], _safe_cell(r["reason_label"]),
                        f"{r['cost']:.2f}", f"{r['value']:.2f}", _safe_cell(r["counted_by"] or "")])
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", count.name)[:40].strip("_") or "count"
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="stock_count_{safe}.csv"', "Cache-Control": "no-store"})

    # ── v51: receiving a delivery by scanning it against its purchase order ──
    @router.get("/purchase-orders/{po_id}/scan.json")
    def po_scan_lookup(request: Request, po_id: str, code: str = Query(""), db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="You were signed out. Sign in again, then continue.")
        if not _can_manage(staff):
            return _json(403, ok=False, error="Only the owner or a manager can receive a delivery.")
        po = db.get(PurchaseOrder, po_id)
        if not po:
            return _json(404, ok=False, error="That purchase order no longer exists.")
        if po.status != "sent":
            return _json(409, ok=False, error=f"{po.number} is {po.status}, so it can't be received now.")
        code = BC.clean(code)
        if len(code) < 3:
            return _json(400, ok=False, error="That doesn't look like a barcode.")
        matches, how = BC.find_products(db, code)
        if not matches:
            return _json(200, ok=False, found=False, code=code, canLink=True, error=f"No product has the code {code}.")
        ids = {m.id for m in matches}
        mine = [l for l in po.lines if l.product_id in ids]
        if not mine:
            return _json(200, ok=False, found=True, code=code, error=f"“{matches[0].name}” is not on {po.number}. Set it aside.")
        line = mine[0]
        return _json(200, ok=True, found=True, how=how, line_id=line.id, name=line.name or matches[0].name, sku=line.sku or "", ordered=int(line.qty or 0))

    return router
