import hashlib
from datetime import datetime, timedelta
from fastapi import Request
import bcrypt as _bcrypt
from sqlalchemy.orm import Session

from .models import Staff, AuditLog
from . import security as _sec

ABSOLUTE_SESSION_HOURS = 24          # however active, a sign-in never lasts longer than this
ACTIVITY_REFRESH_SECONDS = 60        # how often the "last seen" stamp in the cookie is renewed

# Wrong-PIN pausing lives in security.py (per device, v48). The old shop-wide counter (LoginState) is gone:
# one stranger guessing could pause the real owner out of their own shop.

def hash_pin(pin: str) -> str:
    return _bcrypt.hashpw(pin.encode(), _bcrypt.gensalt()).decode()


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return _bcrypt.checkpw(pin.encode(), pin_hash.encode())
    except Exception:
        return False


def attempt_login(db: Session, pin: str):
    """Returns the matching active Staff, or None. The caller checks security.login_block_seconds() first."""
    for staff in db.query(Staff).filter(Staff.active == True).all():  # noqa: E712
        if verify_pin(pin, staff.pin_hash):
            return staff
    return None


def pin_fingerprint(pin_hash: str) -> str:
    """Short fingerprint of the stored PIN hash. A sign-in remembers it, so changing someone's PIN ends their
    other sign-ins without disturbing anyone else."""
    return hashlib.sha256((pin_hash or "").encode()).hexdigest()[:12]


def start_session(request: Request, db: Session, staff) -> None:
    """Begin a signed-in session. The old cookie contents are dropped first (so nothing from before the sign-in,
    including a half-finished 2-step sign-in, carries over)."""
    request.session.clear()
    now = datetime.utcnow().isoformat()
    request.session.update({
        "staff_id": staff.id, "last_activity": now, "login_at": now,
        "epoch": _sec.session_epoch(db), "pk": pin_fingerprint(staff.pin_hash),
    })


def _end_session(request: Request, message: str = "") -> None:
    request.session.clear()
    if message:
        request.session["login_error"] = message


def get_current_staff(request: Request, db: Session):
    """The signed-in staff member, or None. Besides "is there a cookie", this checks that the account is still
    active, that nobody used "sign out everywhere" or changed this PIN since, that the sign-in is not older than
    24 hours and that it has not sat idle longer than the shop's idle limit."""
    staff_id = request.session.get("staff_id")
    if not staff_id:
        return None
    staff = db.get(Staff, staff_id)
    if not staff or not staff.active:
        _end_session(request)
        return None

    if str(request.session.get("epoch", "1")) != _sec.session_epoch(db):
        _end_session(request, "You were signed out. Please sign in again.")
        return None
    fingerprint = pin_fingerprint(staff.pin_hash)
    if request.session.get("pk", fingerprint) != fingerprint:
        _end_session(request, "Your PIN was changed. Please sign in again.")
        return None

    now = datetime.utcnow()

    def _when(key):
        try:
            return datetime.fromisoformat(request.session.get(key) or "")
        except ValueError:
            return None

    login_at = _when("login_at")
    if login_at and now - login_at > timedelta(hours=ABSOLUTE_SESSION_HOURS):
        _end_session(request, "Your sign-in expired. Please sign in again.")
        return None
    idle_limit = _sec.idle_timeout_minutes(db)
    last = _when("last_activity")
    if idle_limit and last and now - last > timedelta(minutes=idle_limit):
        _end_session(request, f"You were signed out after {idle_limit} minutes without activity.")
        return None

    # Background refreshes (the notification poll) must not count as activity, or an unattended page would
    # keep itself signed in forever.
    if not request.url.path.startswith("/api/"):
        if not last or (now - last).total_seconds() > ACTIVITY_REFRESH_SECONDS:
            request.session["last_activity"] = now.isoformat()
    if "pk" not in request.session:
        request.session["pk"] = fingerprint
    if "epoch" not in request.session:
        request.session["epoch"] = _sec.session_epoch(db)
    if "login_at" not in request.session:
        request.session["login_at"] = now.isoformat()
    return staff


def role_allowed(staff, *roles) -> bool:
    if not staff:
        return False
    return staff.role in roles or staff.role == "owner"


def add_audit(db: Session, staff, action: str, detail: str):
    db.add(AuditLog(
        staff_id=staff.id if staff else None,
        staff_name=staff.name if staff else "System",
        action=action, detail=detail,
    ))
    db.commit()

