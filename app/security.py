"""
v48 security helpers: who is connecting, how many wrong tries they get, the sign-in history, the
authenticator-app (TOTP) second step, one-time recovery codes, the security response headers, and the
guard that stops a payment card number from being saved in a free-text note.

Everything here is standard library + `cryptography` (already a dependency). The QR code for 2FA set-up
uses the small optional `segno` package; without it the page still shows the secret key to type in.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import LoginAttempt, Setting

log = logging.getLogger("techpro.security")

# ═══ who is connecting ═════════════════════════════════════════════════

def _client_ip_header() -> str:
    """Which header carries the real visitor address. On Railway that is X-Real-IP (Railway's edge replaces
    any copy the visitor sends). Anywhere else the socket address is used unless CLIENT_IP_HEADER is set,
    because a header that the host does not overwrite can be forged."""
    explicit = os.environ.get("CLIENT_IP_HEADER", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return "x-real-ip"
    return ""


def client_ip(request) -> str:
    header = _client_ip_header()
    if header:
        raw = (request.headers.get(header) or "").split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            pass
    return ((request.client.host if request.client else "") or "unknown")[:45]


def device_label(user_agent: str) -> str:
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "SamsungBrowser" in ua:
        browser = "Samsung Internet"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    if "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua:
        os_name = "iPhone"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "Mac"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "unknown device"
    return f"{browser} on {os_name}"


# ═══ sign-in history and per-device throttling ═════════════════════════

_SIGNIN_KINDS = ("pin", "2fa", "recovery")        # attempts that guess a secret and are throttled together
IP_FAIL_LIMIT = 5                                  # wrong tries before a device is paused
IP_BASE_LOCK = 60                                  # seconds; doubles each further block of 5 wrong tries
IP_MAX_LOCK = 30 * 60
GLOBAL_FAIL_LIMIT = 40                             # wrong tries from ALL devices in 10 minutes
GLOBAL_WINDOW = timedelta(minutes=10)
GLOBAL_LOCK_SECONDS = 120
KNOWN_DEVICE_DAYS = 30
STATUS_FAIL_LIMIT = 10                             # wrong public-status lookups per device per 15 minutes
STATUS_WINDOW = timedelta(minutes=15)


def log_attempt(db: Session, request, kind: str, success: bool, staff=None, note: str = "") -> None:
    try:
        db.add(LoginAttempt(
            ip=client_ip(request), kind=kind, success=success,
            staff_id=staff.id if staff else None, staff_name=staff.name if staff else "",
            device=device_label(request.headers.get("user-agent", "")), note=(note or "")[:200]))
        db.commit()
        if secrets.randbelow(50) == 0:      # tidy occasionally: keep six months
            db.query(LoginAttempt).filter(LoginAttempt.ts < datetime.utcnow() - timedelta(days=180)).delete()
            db.commit()
    except Exception:       # noqa: BLE001 — a history write must never break a sign-in
        db.rollback()
        log.exception("could not record sign-in attempt")


def _lock_seconds_for(failures: int) -> int:
    level = failures // IP_FAIL_LIMIT
    return int(min(IP_MAX_LOCK, IP_BASE_LOCK * (2 ** max(0, level - 1))))


def ip_lock_remaining(db: Session, ip: str) -> int:
    """Seconds this device must wait before it may try again (0 = free to try). A device gets 5 wrong tries,
    then a pause of 1 min, 5 more tries then 2 min, then 4 min ... up to 30 min. A correct sign-in resets it."""
    since = datetime.utcnow() - timedelta(hours=24)
    last_ok = (db.query(func.max(LoginAttempt.ts))
               .filter(LoginAttempt.ip == ip, LoginAttempt.success == True,  # noqa: E712
                       LoginAttempt.kind.in_(_SIGNIN_KINDS)).scalar())
    if last_ok and last_ok > since:
        since = last_ok
    fails = (db.query(LoginAttempt.ts)
             .filter(LoginAttempt.ip == ip, LoginAttempt.success == False,  # noqa: E712
                     LoginAttempt.kind.in_(_SIGNIN_KINDS), LoginAttempt.ts > since)
             .order_by(LoginAttempt.ts.desc()).all())
    n = len(fails)
    if n == 0 or n % IP_FAIL_LIMIT != 0:
        return 0
    wait = _lock_seconds_for(n) - (datetime.utcnow() - fails[0][0]).total_seconds()
    return max(0, int(wait + 0.999))


def _known_device(db: Session, ip: str) -> bool:
    since = datetime.utcnow() - timedelta(days=KNOWN_DEVICE_DAYS)
    return db.query(LoginAttempt.id).filter(
        LoginAttempt.ip == ip, LoginAttempt.success == True,  # noqa: E712
        LoginAttempt.kind.in_(_SIGNIN_KINDS), LoginAttempt.ts > since).first() is not None


def global_pressure_remaining(db: Session, ip: str) -> int:
    """Backstop for a guessing attack spread over many addresses: when the whole shop is seeing a flood of wrong
    PINs, devices that have NOT signed in successfully in the last 30 days are paused. Your own phone and
    computer keep working, so an attacker cannot lock you out by guessing."""
    recent = (db.query(LoginAttempt.ts)
              .filter(LoginAttempt.success == False, LoginAttempt.kind.in_(_SIGNIN_KINDS),  # noqa: E712
                      LoginAttempt.ts > datetime.utcnow() - GLOBAL_WINDOW)
              .order_by(LoginAttempt.ts.desc()).all())
    if len(recent) < GLOBAL_FAIL_LIMIT or _known_device(db, ip):
        return 0
    wait = GLOBAL_LOCK_SECONDS - (datetime.utcnow() - recent[0][0]).total_seconds()
    return max(0, int(wait + 0.999)) or 0


def login_block_seconds(db: Session, request) -> int:
    ip = client_ip(request)
    return max(ip_lock_remaining(db, ip), global_pressure_remaining(db, ip))


def status_lookup_blocked(db: Session, request) -> bool:
    n = db.query(func.count(LoginAttempt.id)).filter(
        LoginAttempt.ip == client_ip(request), LoginAttempt.kind == "status",
        LoginAttempt.success == False, LoginAttempt.ts > datetime.utcnow() - STATUS_WINDOW).scalar()  # noqa: E712
    return (n or 0) >= STATUS_FAIL_LIMIT


# ═══ settings access that does not depend on main.py ═══════════════════

def _get(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row and row.value is not None else default


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


IDLE_CHOICES = (0, 15, 30, 60, 120, 240)       # minutes; 0 = never (a sign-in still ends after 12 hours unused or 24 hours in total)
MIN_PIN_CHOICES = (4, 5, 6, 7, 8)


def idle_timeout_minutes(db: Session) -> int:
    try:
        v = int(_get(db, "idle_timeout_minutes", "60"))
    except ValueError:
        return 60
    return v if v in IDLE_CHOICES else 60


def min_pin_length(db: Session) -> int:
    try:
        v = int(_get(db, "min_pin_length", "4"))
    except ValueError:
        return 4
    return v if v in MIN_PIN_CHOICES else 4


def session_epoch(db: Session) -> str:
    return _get(db, "session_epoch", "1")


def bump_session_epoch(db: Session, request=None) -> str:
    """Signs every device out at once (a lost phone, a changed PIN). The device making the change stays signed in."""
    new = str(int(session_epoch(db) or "1") + 1)
    _set(db, "session_epoch", new)
    if request is not None and request.session.get("staff_id"):
        request.session["epoch"] = new
    return new


def format_wait(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}" + (f" {rest}s" if rest else "")


_PIN_RE = re.compile(r"[0-9]{4,8}")


def looks_like_pin(pin: str) -> bool:
    """4-8 plain digits (0-9 only: str.isdigit() would also accept full-width and superscript digits)."""
    return bool(_PIN_RE.fullmatch(pin or ""))


PIN_TAKEN_MESSAGE = "That PIN can't be used — pick a different one."
PIN_TAKEN_LIMIT = 5                       # refusals per hour before the answer stops depending on the PIN
PIN_TAKEN_WINDOW = timedelta(hours=1)
PIN_TAKEN_THROTTLED = "Too many PINs tried just now. Wait an hour, or ask the owner."


def pin_taken(db: Session, pin: str, exclude_staff_id: str | None = None) -> bool:
    """True when another active person already signs in with this PIN. The sign-in screen asks for a PIN only, so
    two people sharing one would be signed in as whoever happens to come first - sales and the audit trail would
    land on the wrong name (or a cashier could land in a manager's account)."""
    from .auth import verify_pin            # imported here: auth imports this module
    from .models import Staff
    for other in db.query(Staff).filter(Staff.active == True).all():     # noqa: E712
        if exclude_staff_id and other.id == exclude_staff_id:
            continue
        if verify_pin(pin, other.pin_hash):
            return True
    return False


def _pin_taken_refusals(db: Session, request, actor) -> int:
    q = db.query(func.count(LoginAttempt.id)).filter(
        LoginAttempt.kind == "pin_taken", LoginAttempt.success == False,       # noqa: E712
        LoginAttempt.ts > datetime.utcnow() - PIN_TAKEN_WINDOW)
    who = [LoginAttempt.ip == client_ip(request)] if request is not None else []
    if actor is not None:
        who.append(LoginAttempt.staff_id == actor.id)
    if who:
        from sqlalchemy import or_
        q = q.filter(or_(*who))
    return q.scalar() or 0


def validate_new_pin(db: Session, pin: str, staff_id: str | None = None, request=None, actor=None) -> str | None:
    """Returns an error message, or None when the PIN is acceptable. `staff_id` is the person the PIN is for (an
    existing person keeps their own PIN without it counting as taken); leave it out for someone being added.

    "Already taken" tells the asker that somebody signs in with that PIN, so when a request is given the refusals
    are counted and, after a few in an hour, the answer is the same whatever the PIN (no guessing someone's PIN by
    trying them as new ones)."""
    if not pin or not re.fullmatch(r"[0-9]+", pin):
        return "PIN must be digits only."
    need = min_pin_length(db)
    if not (need <= len(pin) <= 8):
        return f"PIN must be {need} to 8 digits." if need < 8 else "PIN must be 8 digits."
    if request is not None and _pin_taken_refusals(db, request, actor) >= PIN_TAKEN_LIMIT:
        return PIN_TAKEN_THROTTLED
    if pin_taken(db, pin, staff_id):
        if request is not None:
            log_attempt(db, request, "pin_taken", False, staff=actor, note="refused: PIN already used by someone else")
        return PIN_TAKEN_MESSAGE
    return None


_COMMON_PINS = {"1234", "0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "4321",
                "1212", "2580", "1004", "2000", "123456", "654321", "000000", "111111", "123123", "121212",
                "12345678", "1234567", "12345", "00000000"}


def is_weak_pin(pin: str) -> bool:
    if not pin:
        return False
    if pin in _COMMON_PINS or len(set(pin)) == 1:
        return True
    digits = [int(c) for c in pin if c.isdigit()]
    if len(digits) == len(pin) and len(digits) >= 4:
        steps = {b - a for a, b in zip(digits, digits[1:])}
        if steps in ({1}, {-1}):
            return True
    return False


def pin_length_for_keypad(staff_rows) -> int:
    """The login keypad submits by itself once the PIN reaches the shop's PIN length - only when every active
    account uses the same length. With mixed lengths the person taps Go."""
    lens = {int(s.pin_len or 4) for s in staff_rows}
    return lens.pop() if len(lens) == 1 else 0


# ═══ authenticator-app codes (TOTP, RFC 6238) ══════════════════════════

TOTP_STEP = 30
TOTP_DIGITS = 6


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _b32decode(secret: str) -> bytes:
    s = re.sub(r"\s+", "", secret or "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_code(secret: str, step: int) -> str:
    digest = hmac.new(_b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return f"{number:0{TOTP_DIGITS}d}"


def verify_totp(secret: str, code: str, last_step: int = 0, now: float | None = None) -> int | None:
    """Returns the 30-second step that matched, or None. The previous / next step are accepted (a phone clock a
    little off) and a step that was already used is refused, so a code cannot be replayed."""
    code = re.sub(r"\s+", "", code or "")
    if not (code.isdigit() and len(code) == TOTP_DIGITS):
        return None
    current = int((time.time() if now is None else now) // TOTP_STEP)
    for step in (current - 1, current, current + 1):
        if step <= (last_step or 0):
            continue
        try:
            if hmac.compare_digest(totp_code(secret, step), code):
                return step
        except Exception:   # noqa: BLE001 — a damaged secret must fail closed, not crash the login
            return None
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def qr_svg(data: str) -> str:
    """Inline SVG of a QR code, or '' when the optional `segno` package is not installed."""
    try:
        import segno
        return segno.make(data, error="m").svg_inline(scale=5, dark="#000000", light="#ffffff", border=2, omitsize=False)
    except Exception:   # noqa: BLE001
        return ""


def group_secret(secret: str) -> str:
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ── one-time recovery codes ───────────────────────────────────────────

_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"       # no look-alike characters


def new_recovery_codes(n: int = 8) -> list[str]:
    return ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) + "-" +
            "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(n)]


def _norm_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(("techpro-recovery:" + _norm_code(code)).encode()).hexdigest()


def store_recovery_codes(staff, codes: list[str]) -> None:
    staff.recovery_codes = json.dumps([hash_recovery_code(c) for c in codes])


def use_recovery_code(staff, code: str) -> bool:
    """True and the code is used up, when it matches one of the staff member's unused codes."""
    try:
        hashes = json.loads(staff.recovery_codes or "[]")
    except ValueError:
        return False
    h = hash_recovery_code(code)
    if len(_norm_code(code)) != 8:
        return False
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            hashes.remove(stored)
            staff.recovery_codes = json.dumps(hashes)
            return True
    return False


def recovery_codes_left(staff) -> int:
    try:
        return len(json.loads(staff.recovery_codes or "[]"))
    except ValueError:
        return 0


# ═══ security headers ══════════════════════════════════════════════════

def _csp() -> str:
    # Pages use inline scripts and styles, so those stay allowed; what the policy stops is a page loading
    # code from elsewhere, sending data to another site, or being framed by another site.
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",   # v54.1: lets the barcode reader (WebAssembly, our own file) compile; NOT 'unsafe-eval'
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


def security_headers(hosted: bool) -> list[tuple[bytes, bytes]]:
    headers = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "same-origin",
        "permissions-policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
        "cross-origin-opener-policy": "same-origin",
        "x-permitted-cross-domain-policies": "none",
        "content-security-policy": _csp(),
    }
    if hosted:
        headers["strict-transport-security"] = "max-age=31536000"
    return [(k.encode(), v.encode()) for k, v in headers.items()]


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (does not touch request bodies). Adds the headers above to every response and marks
    HTML pages `no-store`, so a signed-out shared phone cannot show an old page from its cache."""

    def __init__(self, app, hosted: bool = False):
        self.app = app
        self._extra = security_headers(hosted)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                for k, v in self._extra:
                    if k not in names:
                        headers.append((k, v))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html") and b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ═══ card numbers never stay in a free-text note ═══════════════════════

_PAN_FIELDS = (
    ("Customer", ("notes",)), ("Repair", ("issue", "description")), ("Invoice", ("refund_reason",)),
    ("InvoiceLine", ("exchange_note",)), ("CashSession", ("notes",)), ("Supplier", ("notes",)),
    ("PurchaseOrder", ("notes",)), ("TradeIn", ("notes",)), ("Layaway", ("notes",)),
)
_pan_installed = False


def install_pan_guard() -> None:
    """Before a note is saved, anything that looks like a card number is cut down to its last four digits.
    Only the listed free-text fields are touched: IMEIs, SKUs and barcodes are long numbers too and must stay."""
    global _pan_installed
    if _pan_installed:
        return
    from sqlalchemy import event
    from . import models
    from .money import scrub_card_numbers

    def make(columns):
        def handler(mapper, connection, target):
            for col in columns:
                value = getattr(target, col, None)
                if isinstance(value, str) and value:
                    clean, changed = scrub_card_numbers(value)
                    if changed:
                        setattr(target, col, clean)
                        log.warning("card number masked in %s.%s", type(target).__name__, col)
        return handler

    for model_name, columns in _PAN_FIELDS:
        model = getattr(models, model_name)
        h = make(columns)
        event.listen(model, "before_insert", h)
        event.listen(model, "before_update", h)
    _pan_installed = True


# ═══ private status link ═══════════════════════════════════════════════

def ensure_status_token(db: Session, repair) -> str:
    if not repair.status_token:
        repair.status_token = secrets.token_urlsafe(12)
        db.commit()
    return repair.status_token
