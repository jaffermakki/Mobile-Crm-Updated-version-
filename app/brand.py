"""
v54: the shop's look on the pages customers see (the repair status page and the quote page).

What the owner sets once, on the Customer pages screen:
  * a logo (JPEG, PNG or WebP; checked by its bytes, never SVG)
  * a colour (#RRGGBB)
  * opening hours (a few lines of plain text)
  * whether the drop-off photos show on a customer's private status link (OFF unless the owner turns it on)
The shop name, phone, address and email come from Settings → Shop, so they are only ever typed once.

Nothing in here talks to the web layer. Every value that reaches a template is cleaned here first.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import intake as _intake
from .models import RepairPhoto, Setting, ShopLogo

DEFAULT_COLOR = "#FFC53D"            # the app's own yellow, used until the owner chooses another
MAX_LOGO_BYTES = 300 * 1024
MAX_LOGO_SIDE = 2000                 # pixels: a logo needs nothing bigger and a huge picture is refused
MAX_HOURS_CHARS = 300
MAX_HOURS_LINES = 7
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Photo labels a customer can see. The IMEI/serial label photo is left out: it is for the shop's records.
CUSTOMER_PHOTO_LABELS = ("front", "back", "damage", "after", "other")
MAX_CUSTOMER_PHOTOS = 8

# Customer-facing progress: five steps, each repair status maps onto one of them.
STEPS = ("Received", "Checked", "Repairing", "Ready", "Picked up")
STEP_OF = {"RECEIVED": 0, "DIAGNOSED": 1, "WAITING": 1, "IN_PROGRESS": 2, "READY": 3, "COMPLETED": 3, "COLLECTED": 4}
STEP_NOTE = {
    "RECEIVED": "We have your phone and it is in our queue.",
    "DIAGNOSED": "We have looked at it and know what it needs.",
    "WAITING": "We are waiting for a part to arrive. We will let you know.",
    "IN_PROGRESS": "Your repair is being worked on right now.",
    "READY": "Your phone is ready to pick up.",
    "COMPLETED": "Your phone is ready to pick up.",
    "COLLECTED": "Picked up. Thank you!",
}


class BrandError(ValueError):
    pass


def _get(db, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row and row.value is not None else default


def _put(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


# ── colour ───────────────────────────────────────────────────────────────────

def clean_color(text) -> str:
    """'#RRGGBB' in lower case, '' for an empty box (use the default), or BrandError."""
    t = str(text or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{6}", t):
        t = "#" + t
    if not HEX.match(t):
        raise BrandError("The colour must look like #1A73E8 (a # and six letters or numbers).")
    return t.lower()


def _luminance(hex_color: str) -> float:
    def chan(i):
        v = int(hex_color[1 + 2 * i:3 + 2 * i], 16) / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * chan(0) + 0.7152 * chan(1) + 0.0722 * chan(2)


def on_color(hex_color: str) -> str:
    """Text colour (near black or white) that reads best on top of `hex_color`."""
    lum = _luminance(hex_color)
    dark, light = "#14110a", "#ffffff"
    contrast_dark = (lum + 0.05) / (_luminance(dark) + 0.05)
    contrast_light = (1.05) / (lum + 0.05)
    return dark if contrast_dark >= contrast_light else light


# ── hours ────────────────────────────────────────────────────────────────────

def clean_hours(text) -> str:
    """A few short lines of plain text. Blank lines and stray spaces are dropped; too much is refused, not cut."""
    lines = [" ".join(ln.split()) for ln in str(text or "").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    out = "\n".join(lines)
    if len(lines) > MAX_HOURS_LINES:
        raise BrandError(f"Opening hours can be at most {MAX_HOURS_LINES} lines.")
    if len(out) > MAX_HOURS_CHARS:
        raise BrandError(f"Opening hours are too long (most {MAX_HOURS_CHARS} letters).")
    return out


# ── logo ─────────────────────────────────────────────────────────────────────

def check_logo(raw: bytes):
    """(mime, width, height) for a usable logo, or BrandError with a plain message."""
    if not raw:
        raise BrandError("Choose a picture first.")
    if len(raw) > MAX_LOGO_BYTES:
        raise BrandError(f"That picture is {len(raw) // 1024} KB. The most is {MAX_LOGO_BYTES // 1024} KB; export it smaller.")
    info = _intake.sniff_image(raw)
    if not info:
        raise BrandError("That file is not a PNG, JPEG or WebP picture. (SVG files are not accepted.)")
    mime, w, h = info
    if w > MAX_LOGO_SIDE or h > MAX_LOGO_SIDE:
        raise BrandError(f"That picture is {w}×{h}. The longest side can be at most {MAX_LOGO_SIDE} pixels.")
    return mime, w, h


def save_logo(db, raw: bytes, who: str):
    mime, w, h = check_logo(raw)
    row = db.get(ShopLogo, "logo")
    now = datetime.utcnow()
    if row:
        row.mime, row.width, row.height, row.data, row.size = mime, w, h, raw, len(raw)
        row.updated_by, row.updated_at = who, now
    else:
        db.add(ShopLogo(id="logo", mime=mime, width=w, height=h, data=raw, size=len(raw), updated_by=who, updated_at=now))
    return mime, w, h


def remove_logo(db) -> bool:
    row = db.get(ShopLogo, "logo")
    if not row:
        return False
    db.delete(row)
    return True


def logo_version(db) -> str:
    """Changes whenever the logo does, so a phone never keeps showing the old one."""
    row = db.query(ShopLogo.updated_at).filter(ShopLogo.id == "logo").first()
    return str(int(row[0].timestamp())) if row and row[0] else ""


# ── what the pages read ──────────────────────────────────────────────────────

def phone_digits(phone: str) -> str:
    """Digits (and a leading +) only, safe to put in tel: and sms: links."""
    t = str(phone or "").strip()
    digits = re.sub(r"\D", "", t)
    if not digits:
        return ""
    return ("+" if t.startswith("+") else "") + digits[:15]


def get_brand(db) -> dict:
    color = ""
    try:
        color = clean_color(_get(db, "brand_color"))
    except BrandError:
        color = ""                                       # a bad value from a hand-edited database falls back to the default
    version = logo_version(db)
    hours = ""
    try:
        hours = clean_hours(_get(db, "brand_hours"))
    except BrandError:
        hours = ""
    shown = color or DEFAULT_COLOR
    phone = _get(db, "shop_phone").strip()
    return {
        "shop_name": _get(db, "shop_name", "TechPro+") or "TechPro+",
        "phone": phone, "phone_digits": phone_digits(phone),
        "address": _get(db, "shop_address").strip(), "email": _get(db, "shop_email").strip(),
        "hours": hours, "hours_lines": hours.split("\n") if hours else [],
        "color": shown, "custom_color": bool(color), "on_color": on_color(shown),
        "has_logo": bool(version), "logo_url": f"/brand/logo?v={version}" if version else "",
        "show_photos": _get(db, "brand_show_photos", "false") == "true",
    }


def save_settings(db, color_text, hours_text, show_photos: bool) -> dict:
    """Validate everything first, then write it, so a bad colour never leaves half the form saved."""
    color = clean_color(color_text)
    hours = clean_hours(hours_text)
    _put(db, "brand_color", color)
    _put(db, "brand_hours", hours)
    _put(db, "brand_show_photos", "true" if show_photos else "false")
    return {"color": color, "hours": hours, "show_photos": bool(show_photos)}


# ── progress steps and photos for a customer ─────────────────────────────────

def progress(status: str) -> dict:
    idx = STEP_OF.get(status, 0)
    finished = status == "COLLECTED"
    steps = []
    for i, name in enumerate(STEPS):
        state = "done" if (i < idx or finished) else ("current" if i == idx else "todo")
        steps.append({"name": name, "state": state})
    return {"steps": steps, "note": STEP_NOTE.get(status, ""), "waiting": status == "WAITING"}


def customer_photos(db, repair_id: str) -> list:
    """Photos a customer may see on their private link (no picture data, just ids and labels), oldest first."""
    from sqlalchemy.orm import defer
    rows = (db.query(RepairPhoto).options(defer(RepairPhoto.data), defer(RepairPhoto.thumb))
            .filter(RepairPhoto.repair_id == repair_id, RepairPhoto.label.in_(CUSTOMER_PHOTO_LABELS))
            .order_by(RepairPhoto.created_at, RepairPhoto.id).limit(MAX_CUSTOMER_PHOTOS).all())
    names = dict(_intake.PHOTO_LABELS)
    return [{"id": p.id, "label": names.get(p.label, "Photo")} for p in rows]
