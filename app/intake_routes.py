"""
v49 — repair check-in: the routes for the condition checklist, photos and the printable check-in sheet.

  POST /repairs/{id}/intake                    save (or clear) the condition checklist
  POST /repairs/{id}/photos                    upload one photo (the phone shrinks it first); answers JSON
  GET  /repairs/{id}/photos/{pid}[/thumb]      show a photo (signed-in staff only)
  POST /repairs/{id}/photos/{pid}/delete       delete a photo (owner / manager)
  GET  /repairs/{id}/intake-sheet              printable drop-off sheet with a signature line
  GET  /export/repair-photos                   every photo in one .zip (owner)
  POST /settings/repair-terms                  the wording printed at the bottom of the check-in sheet (owner)

Built with a factory so it can borrow main.py's template engine and login check.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from . import intake as I
from .auth import add_audit, role_allowed
from .csrf import csrf_protect
from .models import Repair, RepairPhoto
from .report_types import _safe_cell

TERMS_MAX = 1200


async def intake_fields(request: Request) -> dict:
    """Dependency: the check-in fields of a submitted form (only the names the checklist uses)."""
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str) and k.startswith(("chk_", "acc_", "lock_", "intake_"))}


def photo_list(db: Session, repair_id: str) -> list:
    """The repair's photos without their picture data (the page asks for each picture separately)."""
    return (db.query(RepairPhoto).options(defer(RepairPhoto.data), defer(RepairPhoto.thumb))
            .filter(RepairPhoto.repair_id == repair_id).order_by(RepairPhoto.created_at, RepairPhoto.id).all())


def _parse_when(text: str):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def detail_context(db: Session, repair, staff) -> dict:
    intake = I.load(repair)
    return {
        "intake_at": _parse_when(intake.get("at", "")),
        "intake": intake, "intake_rows": I.rows(intake), "intake_has": I.has_checklist(intake),
        "intake_problems": I.problems(intake), "intake_left": I.accessory_labels(intake),
        "intake_passcode": I.passcode_label(intake), "intake_find_my": I.find_my_label(intake),
        "intake_accessories": I.ACCESSORIES, "intake_passcode_states": I.PASSCODE_STATES, "intake_find_my_states": I.FIND_MY_STATES,
        "photos": photo_list(db, repair.id), "photo_labels": I.PHOTO_LABELS, "photo_label_names": dict(I.PHOTO_LABELS),
        "max_photos": I.MAX_PHOTOS, "can_delete_photos": role_allowed(staff, "owner", "manager"),
    }


def form_context() -> dict:
    """What the checklist fields need when they appear in the New Ticket form (no ticket exists yet)."""
    intake = I.empty()
    return {
        "intake": intake, "intake_rows": I.rows(intake), "intake_accessories": I.ACCESSORIES,
        "intake_passcode_states": I.PASSCODE_STATES, "intake_find_my_states": I.FIND_MY_STATES,
    }


def settings_context(db: Session) -> dict:
    """What the Settings → Data page shows about repair photos and the check-in sheet wording."""
    count, total = db.query(func.count(RepairPhoto.id), func.coalesce(func.sum(RepairPhoto.size), 0)).one()
    return {"photo_count": int(count or 0), "photo_mb": round((total or 0) / 1_000_000, 1),
            "default_repair_terms": I.DEFAULT_TERMS, "repair_terms_max": TERMS_MAX}


def build_router(templates, require_login, get_db, get_setting, set_setting, get_shop_info) -> APIRouter:
    router = APIRouter()

    def _json(status: int, **body) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    # ── checklist ──
    @router.post("/repairs/{repair_id}/intake")
    def save_intake(request: Request, repair_id: str, fields: dict = Depends(intake_fields),
                    db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        repair = db.get(Repair, repair_id)
        if not repair:
            return RedirectResponse("/repairs", status_code=303)
        data = I.normalise(fields, staff.name)
        repair.intake_json = json.dumps(data, separators=(",", ":")) if data else ""
        repair.updated_at = datetime.utcnow()
        if data:
            bad = len(I.problems(data))
            add_audit(db, staff, "REPAIR_INTAKE", f"#{repair.ticket_no} — check-in saved" + (f" ({bad} problem{'s' if bad != 1 else ''} noted)" if bad else ""))
            request.session["flash"] = ("green", "Check-in saved.")
        else:
            add_audit(db, staff, "REPAIR_INTAKE", f"#{repair.ticket_no} — check-in cleared")
            request.session["flash"] = ("green", "Check-in cleared.")
        db.commit()
        return RedirectResponse(f"/repairs/{repair_id}#checkin", status_code=303)

    # ── photos ──
    @router.post("/repairs/{repair_id}/photos")
    async def upload_photo(request: Request, repair_id: str, file: UploadFile = File(...), thumb: UploadFile = File(None),
                           label: str = Form("other"), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return _json(401, ok=False, error="You were signed out. Sign in again, then retry.")
        repair = db.get(Repair, repair_id)
        if not repair:
            return _json(404, ok=False, error="That ticket no longer exists.")
        have = db.query(func.count(RepairPhoto.id)).filter(RepairPhoto.repair_id == repair_id).scalar() or 0
        if have >= I.MAX_PHOTOS:
            return _json(409, ok=False, error=f"This ticket already has {I.MAX_PHOTOS} photos, the most it can hold. Delete one first.")
        raw = await file.read(I.MAX_PHOTO_BYTES + 1)
        if len(raw) > I.MAX_PHOTO_BYTES:
            return _json(413, ok=False, error="That photo is too big (the limit is 6 MB).")
        info = I.sniff_image(raw)
        if not info:
            return _json(400, ok=False, error="That doesn't look like a JPEG, PNG or WebP picture.")
        mime, width, height = info
        small = None
        if thumb is not None:
            traw = await thumb.read(I.MAX_THUMB_BYTES + 1)
            if len(traw) <= I.MAX_THUMB_BYTES and I.sniff_image(traw):
                small = traw
        label = label if label in I.PHOTO_LABEL_KEYS else "other"
        photo = RepairPhoto(repair_id=repair_id, label=label, mime=mime, width=width, height=height, data=raw, thumb=small,
                            size=len(raw), staff_name=staff.name)
        db.add(photo)
        add_audit(db, staff, "REPAIR_PHOTO", f"#{repair.ticket_no} — photo added ({dict(I.PHOTO_LABELS)[label]}, {len(raw) // 1024} KB)")
        repair.updated_at = datetime.utcnow()
        db.commit()
        return _json(200, ok=True, id=photo.id, count=have + 1, max=I.MAX_PHOTOS)

    def _photo_response(request: Request, repair_id: str, photo_id: str, db: Session, want_thumb: bool):
        staff = require_login(request, db)
        if not staff:
            return Response("Sign in first.", status_code=401, headers={"Cache-Control": "no-store"})
        photo = db.query(RepairPhoto).filter(RepairPhoto.id == photo_id, RepairPhoto.repair_id == repair_id).first()
        if not photo:
            return Response("Not found.", status_code=404, headers={"Cache-Control": "no-store"})
        if want_thumb and photo.thumb:
            body = photo.thumb
            info = I.sniff_image(bytes(body))
            mime = info[0] if info else "image/jpeg"
        else:
            body, mime = photo.data, photo.mime
        return Response(bytes(body), media_type=mime, headers={
            "Cache-Control": "private, max-age=86400", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})

    @router.get("/repairs/{repair_id}/photos/{photo_id}")
    def show_photo(request: Request, repair_id: str, photo_id: str, db: Session = Depends(get_db)):
        return _photo_response(request, repair_id, photo_id, db, want_thumb=False)

    @router.get("/repairs/{repair_id}/photos/{photo_id}/thumb")
    def show_thumb(request: Request, repair_id: str, photo_id: str, db: Session = Depends(get_db)):
        return _photo_response(request, repair_id, photo_id, db, want_thumb=True)

    @router.post("/repairs/{repair_id}/photos/{photo_id}/delete")
    def delete_photo(request: Request, repair_id: str, photo_id: str, db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner", "manager"):
            return HTMLResponse("Forbidden — only the owner or a manager can delete photos.", status_code=403)
        photo = db.query(RepairPhoto).filter(RepairPhoto.id == photo_id, RepairPhoto.repair_id == repair_id).first()
        repair = db.get(Repair, repair_id)
        if photo:
            db.delete(photo)
            add_audit(db, staff, "REPAIR_PHOTO", f"#{repair.ticket_no if repair else '?'} — photo deleted ({photo.label})")
            db.commit()
            request.session["flash"] = ("green", "Photo deleted.")
        return RedirectResponse(f"/repairs/{repair_id}#photos", status_code=303)

    # ── printable sheet ──
    @router.get("/repairs/{repair_id}/intake-sheet", response_class=HTMLResponse)
    def intake_sheet(request: Request, repair_id: str, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        repair = db.get(Repair, repair_id)
        if not repair:
            return RedirectResponse("/repairs", status_code=303)
        ctx = detail_context(db, repair, staff)
        shots = [p for p in ctx["photos"] if p.label != "after"][:6]
        terms = (get_setting(db, "repair_terms", "") or "").strip() or I.DEFAULT_TERMS
        return templates.TemplateResponse(request, "repair_intake_sheet.html", {
            **ctx, "staff": staff, "repair": repair, "shop": get_shop_info(db), "shots": shots, "terms": terms,
            "printed_at": datetime.utcnow(),
        })

    # ── one zip of everything ──
    @router.get("/export/repair-photos")
    def export_photos(request: Request, db: Session = Depends(get_db)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner"):
            return HTMLResponse("Forbidden — only the owner can download all photos.", status_code=403)
        repairs = {r.id: r for r in db.query(Repair).all()}
        rows = (db.query(RepairPhoto.id, RepairPhoto.repair_id, RepairPhoto.label, RepairPhoto.mime, RepairPhoto.created_at, RepairPhoto.staff_name)
                .order_by(RepairPhoto.created_at, RepairPhoto.id).all())
        tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        index = io.StringIO()
        writer = csv.writer(index)
        writer.writerow(["file", "ticket", "device", "customer", "label", "taken (UTC)", "by"])
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for pid, rid, label, mime, created, by in rows:
                repair = repairs.get(rid)
                name = I.photo_file_name(repair.ticket_no if repair else "removed", label, pid, mime)
                blob = db.query(RepairPhoto.data).filter(RepairPhoto.id == pid).scalar()
                if blob is None:
                    continue
                z.writestr(name, bytes(blob))
                writer.writerow([name, repair.ticket_no if repair else "", _safe_cell(repair.device if repair else ""),
                                 _safe_cell(repair.customer.name if repair and repair.customer else ""), label,
                                 created.isoformat(timespec="seconds") if created else "", _safe_cell(by or "")])
            z.writestr("index.csv", index.getvalue())
        add_audit(db, staff, "EXPORT", f"Downloaded all repair photos ({len(rows)} photos)")
        db.commit()
        size = tmp.tell()
        tmp.seek(0)

        def stream():
            try:
                while True:
                    chunk = tmp.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                tmp.close()

        return StreamingResponse(stream(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="techpro_repair_photos_{datetime.utcnow().strftime("%Y-%m-%d")}.zip"',
            "Content-Length": str(size), "Cache-Control": "no-store"})

    # ── the wording at the bottom of the sheet ──
    @router.post("/settings/repair-terms")
    def save_terms(request: Request, terms: str = Form(""), db: Session = Depends(get_db), _csrf: None = Depends(csrf_protect)):
        staff = require_login(request, db)
        if not staff:
            return RedirectResponse("/login", status_code=303)
        if not role_allowed(staff, "owner"):
            return HTMLResponse("Forbidden", status_code=403)
        text = " ".join((terms or "").split())
        if len(text) > TERMS_MAX:
            request.session["flash"] = ("red", f"That's too long ({len(text)} characters; the limit is {TERMS_MAX}). Nothing was saved.")
            return RedirectResponse("/settings#data", status_code=303)
        set_setting(db, "repair_terms", text)
        add_audit(db, staff, "SETTINGS", "Check-in sheet wording " + ("changed" if text else "reset to the standard wording"))
        db.commit()
        request.session["flash"] = ("green", "Check-in sheet wording saved." if text else "Check-in sheet wording reset to the standard text.")
        return RedirectResponse("/settings#data", status_code=303)

    return router
