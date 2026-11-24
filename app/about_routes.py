"""
v54: GET /about — version and health of the shop, for the owner and managers.

"Back up now" is the existing /export/backup download (owner only), so a backup made here counts exactly like any other.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import health as H
from . import version as V
from .auth import role_allowed


def build_router(templates, require_login, get_db) -> APIRouter:
    router = APIRouter()

    @router.get("/about", response_class=HTMLResponse)
    def about(request: Request, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner", "manager"):
            return HTMLResponse("Forbidden — the About page is for the owner and managers.", status_code=403)
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        https = request.url.scheme == "https" or proto == "https"
        can_backup = role_allowed(staff, "owner")
        report = H.collect(db, https, can_backup=can_backup)
        return templates.TemplateResponse(request, "about.html", {
            "staff": staff, "report": report, "support_text": H.support_text(report, db),
            "can_backup": can_backup, "V": V,
        }, headers={"Cache-Control": "no-store"})

    return router
