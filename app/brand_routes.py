"""
v54: the owner's "Customer pages" screen and the public logo.

  GET  /branding                owner: logo, colour, hours, photos switch, and a preview of what customers see
  POST /branding/save           owner: colour, hours, photos switch
  POST /branding/logo           owner: upload the logo
  POST /branding/logo/remove    owner: go back to the plain shop name
  GET  /branding/preview        owner: the customer pages with made-up sample data (nothing real is shown)
  GET  /brand/logo              public: the logo picture itself (customers' phones load it)

Only the owner can change how the shop looks to customers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import brand as B
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import ShopLogo
from .repairs_const import STATUS_BADGE, STATUS_LABELS


def build_router(templates, require_login, get_db) -> APIRouter:
    router = APIRouter()

    def _gate(request, db):
        staff = require_login(request, db)
        if not staff:
            return None, RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner"):
            return staff, HTMLResponse("Forbidden — only the owner can change the customer pages.", status_code=403)
        return staff, None

    def _back(request, colour, message):
        request.session["flash"] = (colour, message)
        return RedirectResponse("/branding", status_code=303)

    @router.get("/branding", response_class=HTMLResponse)
    def page(request: Request, db: Session = Depends(get_db)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        logo = db.query(ShopLogo.width, ShopLogo.height, ShopLogo.size, ShopLogo.updated_at).filter(ShopLogo.id == "logo").first()
        return templates.TemplateResponse(request, "branding.html", {
            "staff": staff, "flash": request.session.pop("flash", None), "brand": B.get_brand(db), "logo": logo,
            "default_color": B.DEFAULT_COLOR, "max_logo_kb": B.MAX_LOGO_BYTES // 1024, "max_side": B.MAX_LOGO_SIDE,
            "max_hours": B.MAX_HOURS_CHARS, "max_hour_lines": B.MAX_HOURS_LINES,
        })

    @router.post("/branding/save")
    def save(request: Request, color: str = Form(""), hours: str = Form(""), show_photos: str = Form(""),
             db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        before = B.get_brand(db)
        try:
            done = B.save_settings(db, color, hours, show_photos.lower() in ("1", "true", "on", "yes"))
        except B.BrandError as e:
            db.rollback()
            return _back(request, "red", f"Not saved: {e}")
        db.commit()
        bits = []
        if (before["color"] if before["custom_color"] else "") != done["color"]:
            bits.append("colour")
        if before["hours"] != done["hours"]:
            bits.append("opening hours")
        if before["show_photos"] != done["show_photos"]:
            bits.append("photos " + ("shown to customers" if done["show_photos"] else "hidden from customers"))
        add_audit(db, staff, "BRAND_CHANGE", "Customer pages: " + (", ".join(bits) if bits else "saved, nothing changed"))
        return _back(request, "green", "Saved. Customers see the new look right away.")

    @router.post("/branding/logo")
    async def logo_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                          _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        raw = await file.read(B.MAX_LOGO_BYTES + 1)
        try:
            mime, w, h = B.save_logo(db, raw, staff.name)
        except B.BrandError as e:
            db.rollback()
            return _back(request, "red", f"Logo not changed: {e}")
        db.commit()
        add_audit(db, staff, "BRAND_CHANGE", f"Customer pages: logo uploaded ({w}×{h}, {len(raw) // 1024} KB)")
        return _back(request, "green", "Logo saved.")

    @router.post("/branding/logo/remove")
    def logo_remove(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        had = B.remove_logo(db)
        db.commit()
        if had:
            add_audit(db, staff, "BRAND_CHANGE", "Customer pages: logo removed")
        return _back(request, "green", "Logo removed. Customers see the shop name instead." if had else "There was no logo to remove.")

    @router.get("/brand/logo")
    def logo_file(db: Session = Depends(get_db)):
        row = db.get(ShopLogo, "logo")
        if not row:
            return Response("No logo.", status_code=404, headers={"Cache-Control": "no-store"})
        return Response(bytes(row.data), media_type=row.mime, headers={
            "Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    @router.get("/branding/preview", response_class=HTMLResponse)
    def preview(request: Request, view: str = "progress", db: Session = Depends(get_db)):
        staff, resp = _gate(request, db)
        if resp:
            return resp
        brand = B.get_brand(db)
        shop_name = brand["shop_name"]
        if view in ("quote", "quote-approved"):
            q = SimpleNamespace(token="preview-only", amount=189.0, summary="Screen replacement (original quality)",
                                note="Includes a 90-day warranty on the part and the work.",
                                status="approved" if view == "quote-approved" else "pending",
                                expires_at=datetime.utcnow() + timedelta(days=7))
            repair = SimpleNamespace(ticket_no=1042, device="iPhone 14 Pro")
            return templates.TemplateResponse(request, "quote_public.html", {
                "shop_name": shop_name, "q": q, "repair": repair, "error": "", "notfound": False, "labels": {},
                "shop_phone": brand["phone"], "brand": brand, "preview": True})
        if view == "lookup":
            return templates.TemplateResponse(request, "public_status.html", {
                "result": None, "error": None, "shop_name": shop_name, "brand": brand, "preview": True})
        status = {"progress": "IN_PROGRESS", "ready": "READY", "waiting": "WAITING", "received": "RECEIVED"}.get(view, "IN_PROGRESS")
        photos = [{"id": "x", "label": "Front"}, {"id": "y", "label": "Damage"}] if brand["show_photos"] else []
        result = {"ticket_no": 1042, "device": "iPhone 14 Pro", "status": STATUS_LABELS[status],
                  "status_badge": STATUS_BADGE[status], "promised_by": "Fri, Nov 27", "ready": status == "READY",
                  "progress": B.progress(status), "photos": photos, "token": "", "sample": True}
        return templates.TemplateResponse(request, "public_status.html", {
            "result": result, "error": None, "shop_name": shop_name, "brand": brand, "preview": True})

    return router
