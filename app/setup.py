"""v51: first-run setup for a brand-new shop (a second location, or a copy sold to someone else).

Until now a new database started with an owner whose PIN was 1234, which anybody who found the web address first could
use. Now:

* A BRAND-NEW database (no staff at all) starts "pending setup". Its owner account exists but has no usable PIN, and
  every page except /setup sends the visitor to the setup screen.
* The setup screen asks for a one-time SETUP CODE, so only the person who can see the server's log (or who chose the
  SETUP_CODE variable) can claim the shop. It then takes the shop's name, province (which sets the tax) and the owner's
  own PIN, and signs the owner in.
* An EXISTING shop is never touched: it already has staff, so it is never "pending" and nothing here runs.
* A backup taken from another shop can still be restored afterwards. Setup state is a flag in the settings table, and
  only a database that was created empty ever receives it.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets

from sqlalchemy.orm import Session

from .auth import hash_pin
from .models import AuditLog, InvoiceLine, Product, Setting, Staff

log = logging.getLogger("techpro.setup")

PENDING_KEY = "setup_pending"
CODE_KEY = "setup_code"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: easy to read off a log line
MIN_ENV_CODE = 6
OWNER_ID = "s1"

# "is this database waiting for setup?" — None until first asked. A stale True in another worker heals itself: the
# setup page re-reads the database, finds it finished and clears it (see routes in main.py).
_state: dict = {"pending": None}


def _setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row is not None else default


def normalise_code(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def _env_code() -> str:
    code = normalise_code(os.environ.get("SETUP_CODE", ""))
    if os.environ.get("SETUP_CODE", "").strip() and len(code) < MIN_ENV_CODE:
        log.warning("SETUP_CODE is shorter than %d letters/digits, so it is ignored and a random code is used.", MIN_ENV_CODE)
        return ""
    return code


def begin(db: Session) -> None:
    """Called once, by init_db, when the database has no staff at all. The caller commits."""
    db.add(Staff(id=OWNER_ID, name="Owner", role="owner", active=True,
                 pin_hash=hash_pin("setup-pending-" + secrets.token_hex(16))))      # not digits: no PIN can ever match it
    db.add(Setting(key=PENDING_KEY, value="1"))
    if not _env_code():
        db.add(Setting(key=CODE_KEY, value=_new_code()))
    _state["pending"] = True


def is_pending(db: Session | None = None) -> bool:
    if _state["pending"] is None:
        if db is None:
            return False
        _state["pending"] = _setting(db, PENDING_KEY) == "1"
    return bool(_state["pending"])


def refresh(db: Session) -> bool:
    """Re-read the flag from the database (the setup page does this, so it never trusts a stale answer)."""
    _state["pending"] = _setting(db, PENDING_KEY) == "1"
    return _state["pending"]


def expected_code(db: Session) -> str:
    return _env_code() or normalise_code(_setting(db, CODE_KEY))


def code_ok(db: Session, given: str) -> bool:
    want, got = expected_code(db), normalise_code(given)
    return bool(want) and bool(got) and hmac.compare_digest(want.encode(), got.encode())


def announce(db: Session) -> None:
    """Print where to find the setup code, at every start-up while setup is still waiting."""
    if not refresh(db):
        return
    if _env_code():
        where = "the SETUP_CODE variable you set on the server"
        code_line = "(use the value of your SETUP_CODE variable)"
    else:
        where = "the line below"
        code_line = _setting(db, CODE_KEY)
    banner = ("\n" + "=" * 66 + f"\n  NEW SHOP: open the site and finish setup. Setup code: see {where}.\n"
              f"  SETUP CODE: {code_line}\n" + "=" * 66)
    log.warning(banner)
    print(banner, flush=True)


def complete(db: Session, *, owner_name: str, pin: str, shop_name: str, province: str, phone: str = "", address: str = "",
             gst: str = "", pst: str = "", remove_samples: bool = True) -> Staff:
    """Apply the answers from the setup page. The caller has validated them. Commits."""
    owner = db.get(Staff, OWNER_ID) or db.query(Staff).filter(Staff.role == "owner").first()
    owner.name = owner_name
    owner.pin_hash = hash_pin(pin)
    owner.pin_len = len(pin)
    owner.active = True

    def put(key: str, value: str) -> None:
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value

    put("shop_name", shop_name)
    put("province", province)
    put("shop_phone", phone)
    put("shop_address", address)
    put("shop_gst", gst)
    put("shop_pst", pst)
    removed = 0
    if remove_samples:
        # only the factory sample products, and only when nothing was ever sold from them
        sold = {pid for (pid,) in db.query(InvoiceLine.product_id).filter(InvoiceLine.product_id.isnot(None)).distinct().all()}
        for p in db.query(Product).filter(Product.sku.in_(SAMPLE_SKUS)).all():
            if p.id not in sold:
                db.delete(p)
                removed += 1
    for key in (PENDING_KEY, CODE_KEY):
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
    db.add(AuditLog(staff_id=owner.id, staff_name=owner.name, action="SETUP",
                    detail=f"First-run setup finished for “{shop_name}” ({province})" + (f"; {removed} sample product(s) removed" if removed else "")))
    db.commit()
    _state["pending"] = False
    return owner


# the four products init_db adds to an empty catalogue so a first look at the app is not blank
SAMPLE_SKUS = ("SCRN-IP13", "CASE-IP13", "CHRG-USBC", "BATT-IP12")


class SetupGateMiddleware:
    """While a new shop is waiting for setup, every page except the setup screen (and the files it needs) goes there."""

    OPEN_PREFIXES = ("/static/",)
    OPEN_PATHS = {"/setup", "/healthz"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _state["pending"]:
            path = scope.get("path", "")
            if path not in self.OPEN_PATHS and not path.startswith(self.OPEN_PREFIXES):
                await send({"type": "http.response.start", "status": 303,
                            "headers": [(b"location", b"/setup"), (b"cache-control", b"no-store"), (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)
