"""
v46 — automatic customer texts: ready-for-pickup, uncollected reminders,
review request and (optional) win-back.

How it works, in one paragraph
------------------------------
Something happens in the shop (a repair becomes Ready, a phone is collected).
A *hook* writes one or more rows into `outbound_messages` — it does not send
anything. A scheduled job (every 5 minutes) calls `process_due()`, which
re-checks that each text is still wanted *at the moment of sending* (device
still uncollected? customer hasn't replied STOP? consent still on? inside
quiet hours? under the daily cap?) and only then calls Twilio. Because the
decision is made at send time, a mistaken click, a collected phone or a
late STOP can never produce a wrong text.

Guarantees (each one has a test)
--------------------------------
* Off by default. Nothing is queued or sent until the owner switches it on.
* A given text is never sent twice: `dedupe_key` is UNIQUE, rows are claimed
  with a conditional UPDATE, and a crash mid-send is NOT retried automatically
  (it is marked failed with "outcome unknown") so nobody gets a double text.
* Customers who replied STOP, or whom staff marked "don't text", get nothing
  automatic — transactional or promotional.
* Promotional texts (review request, win-back) also need the customer's
  marketing consent (CASL) and always carry "Reply STOP to opt out".
* Quiet hours (shop-local, default 9:00–20:00), a daily cap (default 150) and
  a per-run batch limit protect customers and the Twilio bill.
* Temporary failures retry (15 min, 1 h, 4 h); permanent ones (bad number,
  landline, blocked) never do. A wrong Twilio login stops the run and leaves
  the queue intact.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from .localtime import day_bounds_utc, get_shop_tz, to_local, to_local_date
from .models import Customer, Invoice, OutboundMessage, Repair, Setting, SmsMessage

# ── constants ─────────────────────────────────────────────────────────────

AWAITING = ("READY", "COMPLETED")            # repair statuses meaning "finished, still in the shop"
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "YES", "UNSTOP"}
KINDS = ("ready", "reminder", "review", "winback")
PROMO_KINDS = ("review", "winback")           # these need marketing consent
OPT_OUT_LINE = "Reply STOP to opt out."
# A text that is still unsent this long after it was due is dropped rather than
# sent late and confusing ("your phone is ready" two days after you collected it).
MAX_LATE = {"ready": timedelta(hours=48), "reminder": timedelta(hours=72),
            "review": timedelta(days=14), "winback": timedelta(days=7)}
RETRY_BACKOFF = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4))
MAX_ATTEMPTS = 3
STALE_SENDING = timedelta(minutes=10)
BATCH = 20
MAX_BODY = 1000

DEFAULTS = {
    "msg_enabled": "false",
    "msg_ready_enabled": "true", "msg_ready_delay_min": "5",
    "msg_remind_enabled": "true", "msg_remind_days": "3,7,14",
    "msg_review_enabled": "false", "msg_review_delay_days": "3", "msg_review_link": "",
    "msg_winback_enabled": "false", "msg_winback_after_days": "120",
    "msg_winback_cooldown_days": "180", "msg_winback_per_day": "10",
    "msg_quiet_start": "9", "msg_quiet_end": "20",
    "msg_daily_cap": "150", "msg_country_code": "1", "msg_shop_phone": "",
}
DEFAULT_TEMPLATES = {
    "ready": "Hi {name}, your {device} is repaired and ready for pickup at {shop}. Ticket #{ticket}. See you soon!",
    "reminder": "Hi {name}, a friendly reminder: your {device} (ticket #{ticket}) has been ready for pickup at {shop} "
                "for {days} days.",
    "review": "Hi {name}, thanks for choosing {shop}! If we looked after you, a quick Google review helps a small "
              "shop a lot: {review_link}",
    "winback": "Hi {name}, it's {shop}. We haven't seen you in a while - if your phone or any device needs a "
               "check-up, repair or accessory, we're here to help.",
}
PLACEHOLDERS = ("name", "device", "ticket", "shop", "shop_phone", "days", "review_link")
_TOKEN = re.compile(r"\{(\w+)\}")


# ── phone numbers ─────────────────────────────────────────────────────────

def normalize_phone(raw, default_cc: str = "1"):
    """Return the number in E.164 (+12505550101) or None if it can't receive
    a text. Customer phones are stored exactly as typed ("(250) 555-0101"),
    but Twilio wants E.164 — and silently mis-parses some other forms."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or re.search(r"[A-Za-z]", s):          # "N/A", "ext 5", "x12" ...
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    international = s.startswith("+") or s.startswith("00")
    if s.startswith("00"):
        digits = digits[2:]
    cc = re.sub(r"\D", "", str(default_cc or "1")) or "1"
    if international:
        full = digits
    elif cc == "1":
        if len(digits) == 10:
            full = "1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            full = digits
        else:
            return None
    else:
        full = digits if (digits.startswith(cc) and len(digits) > len(cc) + 5) else cc + digits.lstrip("0")
    if full.startswith("1"):                          # every +1 number is North American (NANP)
        if len(full) != 11:
            return None
        area, exch = full[1:4], full[4:7]
        if not (re.fullmatch(r"[2-9]\d\d", area) and re.fullmatch(r"[2-9]\d\d", exch)):
            return None
        if area[1:] == "11" or exch[1:] == "11":      # 911, 411 ... are not subscriber numbers
            return None
    if not (8 <= len(full) <= 15):
        return None
    return "+" + full


# ── text length (so the owner can see what a message will cost) ──────────

_GSM_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXT = set("^{}\\[~]|€")


def sms_info(text: str) -> dict:
    """{encoding, units, segments}: how many billable parts a text is. One
    emoji or curly quote switches the whole message to UCS-2 (70 chars/part
    instead of 160)."""
    text = text or ""
    if all(ch in _GSM_BASIC or ch in _GSM_EXT for ch in text):
        units = sum(2 if ch in _GSM_EXT else 1 for ch in text)
        segs = 0 if units == 0 else (1 if units <= 160 else math.ceil(units / 153))
        return {"encoding": "GSM-7", "units": units, "segments": segs}
    units = len(text.encode("utf-16-le")) // 2
    segs = 0 if units == 0 else (1 if units <= 70 else math.ceil(units / 67))
    return {"encoding": "Unicode", "units": units, "segments": segs}


# ── settings ──────────────────────────────────────────────────────────────

def get_setting(db, key: str, default: str = ""):
    s = db.get(Setting, key)
    return s.value if s else default


def _set(db, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


def _int(v, default, lo, hi):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def parse_days(v, default=(3, 7, 14)):
    """'3, 7,14' -> [3, 7, 14]; sorted, unique, 1-60, at most 5. Junk -> default."""
    try:
        nums = sorted({int(x) for x in re.split(r"[,\s]+", str(v).strip()) if x != ""})
    except ValueError:
        return list(default)
    nums = [n for n in nums if 1 <= n <= 60][:5]
    return nums or list(default)


@dataclass
class Config:
    enabled: bool = False
    ready_enabled: bool = True
    ready_delay_min: int = 5
    remind_enabled: bool = True
    remind_days: list = field(default_factory=lambda: [3, 7, 14])
    review_enabled: bool = False
    review_delay_days: int = 3
    review_link: str = ""
    winback_enabled: bool = False
    winback_after_days: int = 120
    winback_cooldown_days: int = 180
    winback_per_day: int = 10
    quiet_start: int = 9
    quiet_end: int = 20
    daily_cap: int = 150
    country_code: str = "1"
    shop_phone: str = ""
    shop_name: str = "our shop"
    templates: dict = field(default_factory=lambda: dict(DEFAULT_TEMPLATES))


def valid_link(url: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s]+\.[^\s]+", (url or "").strip())) and len(url.strip()) <= 300


def load_config(db) -> Config:
    g = lambda k: get_setting(db, k, DEFAULTS.get(k, ""))
    c = Config()
    c.enabled = g("msg_enabled") == "true"
    c.ready_enabled = g("msg_ready_enabled") == "true"
    c.ready_delay_min = _int(g("msg_ready_delay_min"), 5, 0, 120)
    c.remind_enabled = g("msg_remind_enabled") == "true"
    c.remind_days = parse_days(g("msg_remind_days"))
    c.review_enabled = g("msg_review_enabled") == "true"
    c.review_delay_days = _int(g("msg_review_delay_days"), 3, 0, 30)
    c.review_link = g("msg_review_link").strip()
    c.winback_enabled = g("msg_winback_enabled") == "true"
    c.winback_after_days = _int(g("msg_winback_after_days"), 120, 30, 730)
    c.winback_cooldown_days = _int(g("msg_winback_cooldown_days"), 180, 30, 730)
    c.winback_per_day = _int(g("msg_winback_per_day"), 10, 1, 100)
    qs, qe = _int(g("msg_quiet_start"), 9, 0, 23), _int(g("msg_quiet_end"), 20, 1, 24)
    if qs >= qe:                                   # nonsense window -> safe default instead of "never sends"
        qs, qe = 9, 20
    c.quiet_start, c.quiet_end = qs, qe
    c.daily_cap = _int(g("msg_daily_cap"), 150, 1, 2000)
    c.country_code = re.sub(r"\D", "", g("msg_country_code")) or "1"
    c.shop_phone = g("msg_shop_phone").strip()
    c.shop_name = (get_setting(db, "shop_name", "") or "our shop").strip()
    for k in KINDS:
        t = get_setting(db, f"msg_tpl_{k}", "").strip()
        if t:
            c.templates[k] = t
    return c


def template_problems(text: str) -> list[str]:
    """Why a template can't be saved (unknown {placeholders}, too long, empty)."""
    probs = []
    if not (text or "").strip():
        probs.append("is empty")
    unknown = sorted({m for m in _TOKEN.findall(text or "") if m not in PLACEHOLDERS})
    if unknown:
        probs.append("uses unknown placeholder(s): " + ", ".join("{" + u + "}" for u in unknown))
    if len(text or "") > 320:
        probs.append("is longer than 320 characters (that is 3+ billable text parts)")
    return probs


def twilio_configured(db) -> bool:
    return bool(get_setting(db, "twilio_sid") and get_setting(db, "twilio_token") and get_setting(db, "twilio_from"))


# ── rendering ─────────────────────────────────────────────────────────────

def first_name(name: str) -> str:
    n = re.sub(r"[\r\n\t]+", " ", (name or "")).strip()
    if not n or n.lower() in ("walk-in", "walkin", "customer", "guest", "n/a", "unknown"):
        return "there"
    return n.split()[0][:30]


def _clean(v, limit=60) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(v or "")).strip()[:limit]


def render(template: str, values: dict) -> str:
    """Replace only the known {placeholders}; anything else becomes empty.
    (str.format would let a template read object attributes — this cannot.)"""
    out = _TOKEN.sub(lambda m: str(values.get(m.group(1), "")) if m.group(1) in PLACEHOLDERS else "", template or "")
    out = re.sub(r"\(\s*\)", "", out)                  # "(  )" left by an empty {shop_phone}
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([.,!?])", r"\1", out)
    return out.strip()


def with_opt_out(kind: str, body: str, reminder_seq: int = 0) -> str:
    """Promotional texts always carry the unsubscribe line; reminders carry it
    too (they are repeats). The very first ready-for-pickup text does not."""
    needs = kind in PROMO_KINDS or kind == "reminder"
    if needs and "reply stop" not in body.lower():
        return f"{body}\n{OPT_OUT_LINE}"
    return body


def sample_values(cfg: Config) -> dict:
    return {"name": "Alex", "device": "iPhone 13", "ticket": "1042", "shop": cfg.shop_name,
            "shop_phone": cfg.shop_phone or "our shop number", "days": "7",
            "review_link": cfg.review_link or "https://g.page/r/your-link"}


def preview(cfg: Config, kind: str, template: str | None = None) -> dict:
    body = with_opt_out(kind, render(template if template is not None else cfg.templates[kind], sample_values(cfg)))
    return {"body": body, **sms_info(body)}


# ── helpers on repairs / customers ────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.utcnow()


def ready_at(repair) -> datetime:
    """When the ticket last became Ready, from its status history."""
    try:
        hist = json.loads(repair.status_history or "[]")
    except ValueError:
        hist = []
    for h in reversed(hist):
        if h.get("status") == "READY":
            try:
                return datetime.fromisoformat(str(h.get("date")))
            except (TypeError, ValueError):
                break
    return repair.updated_at or repair.created_at or utcnow()


def can_text(customer, country_code="1"):
    """(ok, reason). Reason is empty when ok."""
    if customer is None:
        return False, "No customer on the ticket"
    if customer.sms_opt_out:
        return False, "Customer opted out of texts (STOP)"
    if not (customer.phone or "").strip():
        return False, "No phone number on file"
    if not normalize_phone(customer.phone, country_code):
        return False, "Phone number isn't a valid mobile number"
    return True, ""


def customers_for_phone(db, e164: str, country_code="1"):
    """Customers whose stored phone (any format) is this number. Done in
    Python because phones are stored as typed, e.g. '(250) 555-0101'."""
    out = []
    last4 = e164[-4:]
    for c in db.query(Customer).filter(Customer.phone != "", Customer.phone.isnot(None)).all():
        if last4 in re.sub(r"\D", "", c.phone or "")[-6:] and normalize_phone(c.phone, country_code) == e164:
            out.append(c)
    return out


# ── queue: writing rows ───────────────────────────────────────────────────

def _enqueue(db, kind: str, key: str, due: datetime, *, repair=None, customer=None, seq=0, source="auto"):
    """Insert a pending row unless one with this key already exists. Safe
    against two workers racing (the UNIQUE key decides; the loser gets None)."""
    if db.query(OutboundMessage.id).filter(OutboundMessage.dedupe_key == key).first():
        return None
    row = OutboundMessage(kind=kind, seq=seq, dedupe_key=key, status="pending", source=source, due_at=due,
                          repair_id=repair.id if repair is not None else None,
                          customer_id=(customer.id if customer is not None else (repair.customer_id if repair is not None else None)))
    try:
        db.flush()                                    # persist the caller's pending changes first
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


def on_repair_ready(db, repair, now: datetime | None = None) -> int:
    """Hook: a ticket just moved to Ready. Queues the ready text and the
    reminder ladder. Does not commit (the caller commits with the status change)."""
    cfg = load_config(db)
    if not cfg.enabled:
        return 0
    ok, _ = can_text(repair.customer, cfg.country_code)
    if not ok:
        return 0
    now = now or utcnow()
    n = 0
    if cfg.ready_enabled and _enqueue(db, "ready", f"ready:{repair.id}", now + timedelta(minutes=cfg.ready_delay_min), repair=repair):
        n += 1
    if cfg.remind_enabled:
        for i, d in enumerate(cfg.remind_days, 1):
            if _enqueue(db, "reminder", f"remind:{repair.id}:{i}", now + timedelta(days=d), repair=repair, seq=i):
                n += 1
    return n


def cancel_pending(db, *, repair_id=None, customer_id=None, kinds=None, reason="Cancelled") -> int:
    q = db.query(OutboundMessage).filter(OutboundMessage.status == "pending")
    if repair_id:
        q = q.filter(OutboundMessage.repair_id == repair_id)
    if customer_id:
        q = q.filter(OutboundMessage.customer_id == customer_id)
    if kinds:
        q = q.filter(OutboundMessage.kind.in_(kinds))
    return q.update({"status": "cancelled", "skip_reason": reason}, synchronize_session=False)


def on_repair_collected(db, repair, now: datetime | None = None) -> int:
    """Hook: the customer collected the device. Stops reminders; queues the
    review request if that's switched on and the customer agreed to
    promotional texts. Does not commit."""
    cancel_pending(db, repair_id=repair.id, kinds=("ready", "reminder"), reason="Device collected")
    cfg = load_config(db)
    if not (cfg.enabled and cfg.review_enabled and valid_link(cfg.review_link)):
        return 0
    cust = repair.customer
    ok, _ = can_text(cust, cfg.country_code)
    if not ok or not cust.marketing_consent:
        return 0
    now = now or utcnow()
    return 1 if _enqueue(db, "review", f"review:{repair.id}", now + timedelta(days=cfg.review_delay_days), repair=repair) else 0


def record_manual_ready(db, repair, phone: str, body: str, staff_name: str, sid: str = ""):
    """Staff pressed 'Send Ready-for-Pickup SMS'. Record it so the automatic
    ready text for the same ticket knows it has already been done."""
    now = utcnow()
    db.add(OutboundMessage(kind="ready", dedupe_key=f"manual-ready:{repair.id}:{int(now.timestamp() * 1000)}",
                           status="sent", source="manual", due_at=now, sent_at=now, attempts=1, to_phone=phone, body=body,
                           provider_sid=sid, staff_name=staff_name, repair_id=repair.id, customer_id=repair.customer_id))
    cancel_pending(db, repair_id=repair.id, kinds=("ready",), reason="Staff already sent it by hand")


# ── queue: sending ────────────────────────────────────────────────────────

def in_window(now_utc: datetime, tz, start: int, end: int) -> bool:
    return start <= to_local(now_utc, tz).hour < end


def _sent_today(db, tz, now: datetime) -> int:
    d = to_local_date(now, tz)
    s, e = day_bounds_utc(d, d, tz)
    return (db.query(func.count(OutboundMessage.id))
            .filter(OutboundMessage.status == "sent", OutboundMessage.source != "manual", OutboundMessage.sent_at >= s, OutboundMessage.sent_at < e).scalar() or 0)


def _reap_stale(db, now: datetime) -> int:
    """A row stuck in 'sending' means the app stopped mid-send. We cannot know
    whether Twilio got it, so we do NOT resend (that could double-text a
    customer) — we mark it failed and say so."""
    n = (db.query(OutboundMessage)
         .filter(OutboundMessage.status == "sending", OutboundMessage.claimed_at < now - STALE_SENDING)
         .update({"status": "failed", "error_code": "unknown",
                  "last_error": "The app stopped while sending, so it is unknown whether this text went out. "
                                "Check Twilio's message log before sending it again."}, synchronize_session=False))
    if n:
        db.commit()
    return n


def _claim(db, mid: str, now: datetime) -> bool:
    res = db.execute(update(OutboundMessage).where(OutboundMessage.id == mid, OutboundMessage.status == "pending")
                     .values(status="sending", claimed_at=now))
    db.commit()
    return res.rowcount == 1


def _skip(row, reason: str) -> str:
    row.status, row.skip_reason, row.claimed_at = "skipped", reason[:200], None
    return "skipped"


def _last_activity(db, customer_id: str):
    a = db.query(func.max(Invoice.date)).filter(Invoice.customer_id == customer_id).scalar()
    b = db.query(func.max(Repair.created_at)).filter(Repair.customer_id == customer_id).scalar()
    return max([x for x in (a, b) if x], default=None)


def _already_texted(db, repair_id: str, exclude_id: str, since: datetime | None, kinds=("ready", "reminder")) -> bool:
    q = db.query(OutboundMessage.id).filter(OutboundMessage.repair_id == repair_id, OutboundMessage.status == "sent",
                                            OutboundMessage.id != exclude_id, OutboundMessage.kind.in_(kinds))
    if since:
        q = q.filter(OutboundMessage.sent_at >= since)
    return q.first() is not None


def _default_send(db, to_phone, body):
    from .notifications import send_sms_detailed
    return send_sms_detailed(db, to_phone, body, get_setting)


def _deliver(db, row, cfg: Config, now: datetime, send) -> str:
    """One claimed row -> sent / skipped / failed / retry / abort."""
    if now - row.due_at > MAX_LATE.get(row.kind, timedelta(days=3)):
        return _skip(row, "Too late to be useful (it was due long ago)")
    rep = row.repair
    cust = rep.customer if (rep is not None and row.kind != "winback") else row.customer
    if row.kind in ("ready", "reminder"):
        if rep is None:
            return _skip(row, "The repair ticket no longer exists")
        if rep.status not in AWAITING:
            return _skip(row, "Device already collected" if rep.status == "COLLECTED" else "Repair is no longer waiting for pickup")
    if row.kind == "review":
        if rep is None or rep.status != "COLLECTED":
            return _skip(row, "Repair is not in Collected status")
    if cust is None:
        return _skip(row, "No customer on the ticket")
    if cust.sms_opt_out:
        return _skip(row, "Customer opted out of texts (STOP)")
    phone = normalize_phone(cust.phone, cfg.country_code)
    if not phone:
        return _skip(row, "No valid mobile number on file")
    if row.kind in PROMO_KINDS and not cust.marketing_consent:
        return _skip(row, "No consent for promotional texts (CASL)")
    row.customer_id = cust.id

    vals = {"name": first_name(cust.name), "shop": _clean(cfg.shop_name, 40), "shop_phone": _clean(cfg.shop_phone, 20),
            "device": _clean(rep.device, 40) if rep is not None else "", "ticket": str(rep.ticket_no) if rep is not None else "",
            "days": "", "review_link": cfg.review_link}
    if row.kind == "ready":
        if _already_texted(db, rep.id, row.id, None, ("ready",)):
            return _skip(row, "A ready text was already sent for this ticket")
    elif row.kind == "reminder":
        if _already_texted(db, rep.id, row.id, now - timedelta(hours=36)):
            return _skip(row, "Customer was texted about this ticket in the last 36 hours")
        vals["days"] = str(max(1, (now - ready_at(rep)).days))
    elif row.kind == "review":
        if not (cfg.review_enabled and valid_link(cfg.review_link)):
            return _skip(row, "Review texts are switched off or have no review link")
    elif row.kind == "winback":
        if not cfg.winback_enabled:
            return _skip(row, "Win-back texts are switched off")
        last = _last_activity(db, cust.id)
        if last and last > now - timedelta(days=cfg.winback_after_days):
            return _skip(row, "Customer has been in since this was queued")
        if db.query(Repair.id).filter(Repair.customer_id == cust.id, Repair.status != "COLLECTED").first():
            return _skip(row, "Customer has a repair in progress")

    body = with_opt_out(row.kind, render(cfg.templates[row.kind], vals))
    if not body or len(body) > MAX_BODY:
        return _skip(row, "The rendered message is empty or too long")

    res = send(db, phone, body)
    row.attempts = (row.attempts or 0) + 1
    row.claimed_at = None
    row.to_phone = res.get("to") or phone
    if res.get("ok"):
        row.status, row.sent_at, row.body, row.provider_sid = "sent", now, body, res.get("sid", "")
        row.error_code = row.last_error = ""
        db.add(SmsMessage(customer_id=cust.id, phone=row.to_phone, body=body, direction="out", staff_name="Auto"))
        return "sent"
    row.error_code, row.last_error = str(res.get("error_code", ""))[:30], str(res.get("message", ""))[:400]
    code = row.error_code
    if res.get("configured") is False:                       # no Twilio login saved: leave it for later, stop the run
        row.status, row.attempts = "pending", max(0, row.attempts - 1)
        return "abort"
    if code in ("20003", "20404"):                           # Twilio login rejected: same — config problem, not the customer's
        row.status, row.attempts = "pending", max(0, row.attempts - 1)
        return "abort"
    if code == "21610":                                      # carrier-level STOP: remember it for good
        cust.sms_opt_out, cust.sms_opt_out_at = True, now
        row.status = "failed"
        return "failed"
    if res.get("retryable") and row.attempts < MAX_ATTEMPTS:
        row.status = "pending"
        row.due_at = now + RETRY_BACKOFF[min(row.attempts - 1, len(RETRY_BACKOFF) - 1)]
        return "retry"
    row.status = "failed"
    return "failed"


def process_due(db, now: datetime | None = None, send=None, limit: int = BATCH, ignore_hours: bool = False) -> dict:
    """Send what is due and still wanted. Safe to call as often as you like."""
    stats = {"sent": 0, "failed": 0, "skipped": 0, "retry": 0, "deferred": "", "aborted": ""}
    cfg = load_config(db)
    if not cfg.enabled:
        stats["deferred"] = "Messaging is switched off"
        return stats
    now = now or utcnow()
    tz = get_shop_tz(db)
    _reap_stale(db, now)
    if not ignore_hours and not in_window(now, tz, cfg.quiet_start, cfg.quiet_end):
        stats["deferred"] = f"Outside sending hours ({cfg.quiet_start}:00–{cfg.quiet_end}:00)"
        return stats
    room = cfg.daily_cap - _sent_today(db, tz, now)
    if room <= 0:
        stats["deferred"] = f"Daily limit of {cfg.daily_cap} texts reached"
        return stats
    send = send or _default_send
    ids = [i for (i,) in db.query(OutboundMessage.id).filter(OutboundMessage.status == "pending", OutboundMessage.due_at <= now)
           .order_by(OutboundMessage.due_at).limit(min(limit, room)).all()]
    for mid in ids:
        if not _claim(db, mid, now):
            continue                                           # another worker took it
        row = db.get(OutboundMessage, mid)
        try:
            outcome = _deliver(db, row, cfg, now, send)
        except Exception as e:                                # a bug on one row must not block the others
            row.status, row.error_code, row.last_error, row.claimed_at = "failed", "internal", f"Unexpected error: {e}"[:400], None
            outcome = "failed"
        db.commit()
        if outcome == "abort":
            stats["aborted"] = row.last_error or "SMS is not configured"
            break
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


# ── campaigns: win-back and catch-up ──────────────────────────────────────

def enqueue_winback(db, now: datetime | None = None) -> int:
    """Daily: queue win-back texts for lapsed, consenting customers, best
    spenders first, at most `winback_per_day` a day, each customer at most
    once per cooldown. Returns how many were queued."""
    cfg = load_config(db)
    if not (cfg.enabled and cfg.winback_enabled):
        return 0
    now = now or utcnow()
    tz = get_shop_tz(db)
    today = to_local_date(now, tz)
    day_start, _ = day_bounds_utc(today, today, tz)
    queued_today = db.query(func.count(OutboundMessage.id)).filter(OutboundMessage.kind == "winback", OutboundMessage.created_at >= day_start).scalar() or 0
    room = cfg.winback_per_day - queued_today
    if room <= 0:
        return 0
    cutoff = now - timedelta(days=cfg.winback_after_days)
    last_inv = dict(db.query(Invoice.customer_id, func.max(Invoice.date)).filter(Invoice.customer_id.isnot(None)).group_by(Invoice.customer_id).all())
    last_rep = dict(db.query(Repair.customer_id, func.max(Repair.created_at)).filter(Repair.customer_id.isnot(None)).group_by(Repair.customer_id).all())
    open_rep = {c for (c,) in db.query(Repair.customer_id).filter(Repair.status != "COLLECTED", Repair.customer_id.isnot(None)).distinct()}
    recent = {c for (c,) in db.query(OutboundMessage.customer_id).filter(
        OutboundMessage.kind == "winback", OutboundMessage.status.in_(("pending", "sending", "sent")),
        OutboundMessage.created_at >= now - timedelta(days=cfg.winback_cooldown_days)).distinct()}
    cands = []
    for c in db.query(Customer).filter(Customer.marketing_consent.is_(True), Customer.spent > 0).order_by(Customer.spent.desc()).all():
        if c.sms_opt_out or c.id in open_rep or c.id in recent:
            continue
        last = max([x for x in (last_inv.get(c.id), last_rep.get(c.id)) if x], default=None)
        if last is None or last >= cutoff:
            continue
        if not can_text(c, cfg.country_code)[0]:
            continue
        cands.append(c)
    n = 0
    for c in cands[:room]:
        if _enqueue(db, "winback", f"winback:{c.id}:{today.isoformat()}", now, customer=c, source="campaign"):
            n += 1
    db.commit()
    return n


def catchup_candidates(db, now: datetime | None = None, cfg: Config | None = None) -> list:
    """Tickets that were already Ready (for a day or more) before messaging was
    switched on — the phones sitting on the shelf. Never queued automatically."""
    cfg = cfg or load_config(db)
    now = now or utcnow()
    out = []
    for r in db.query(Repair).filter(Repair.status.in_(AWAITING)).all():
        if ready_at(r) > now - timedelta(days=1):
            continue
        if not can_text(r.customer, cfg.country_code)[0]:
            continue
        if db.query(OutboundMessage.id).filter(OutboundMessage.repair_id == r.id, OutboundMessage.status == "sent",
                                               OutboundMessage.sent_at >= now - timedelta(days=2)).first():
            continue
        out.append(r)
    return out


def enqueue_catchup(db, now: datetime | None = None) -> int:
    """Owner pressed 'Remind customers whose phones are already waiting'.
    One reminder per ticket, ever (the key makes a second press harmless)."""
    cfg = load_config(db)
    if not cfg.enabled:
        return 0
    now = now or utcnow()
    n = 0
    for r in catchup_candidates(db, now, cfg):
        if _enqueue(db, "reminder", f"catchup:{r.id}", now, repair=r, seq=0, source="campaign"):
            n += 1
    db.commit()
    return n


# ── inbound replies (STOP / START) ────────────────────────────────────────

def classify_reply(body: str) -> str | None:
    """'stop', 'start' or None. STOP wins for short messages that begin with a
    stop word ('Stop texting me please'); START needs the exact word."""
    words = re.findall(r"[A-Za-z]+", body or "")
    if not words:
        return None
    first = words[0].upper()
    if len(words) <= 6 and first in STOP_WORDS:
        return "stop"
    if len(words) == 1 and first in START_WORDS:
        return "start"
    return None


def handle_inbound(db, from_phone: str, body: str, now: datetime | None = None) -> str | None:
    """Log a customer's reply and obey STOP / START. Matches the customer by
    normalised number, so '+12505550101' finds '(250) 555-0101'."""
    from .auth import add_audit
    now = now or utcnow()
    cc = get_setting(db, "msg_country_code", "1") or "1"
    e164 = normalize_phone(from_phone, cc)
    custs = customers_for_phone(db, e164, cc) if e164 else []
    for c in db.query(Customer).filter(Customer.phone == from_phone).all():     # exact text match, as the app always did
        if c not in custs:
            custs.append(c)
    first = custs[0] if custs else None
    db.add(SmsMessage(customer_id=first.id if first else None, phone=from_phone, body=body, direction="in"))
    action = classify_reply(body)
    if action == "stop":
        for c in custs:
            c.sms_opt_out, c.sms_opt_out_at = True, now
            cancel_pending(db, customer_id=c.id, reason="Customer replied STOP")
        if custs:
            db.commit()
            add_audit(db, None, "SMS_OPT_OUT", f"{', '.join(c.name for c in custs)} replied STOP ({e164})")
    elif action == "start":
        for c in custs:
            c.sms_opt_out, c.sms_opt_out_at = False, None
        if custs:
            db.commit()
            add_audit(db, None, "SMS_OPT_IN", f"{', '.join(c.name for c in custs)} replied START ({e164})")
    db.commit()
    return action


def set_opt_out(db, customer, opted_out: bool, staff) -> None:
    """Staff toggle (customer told them at the counter / asked to be texted again)."""
    from .auth import add_audit
    customer.sms_opt_out = bool(opted_out)
    customer.sms_opt_out_at = utcnow() if opted_out else None
    if opted_out:
        cancel_pending(db, customer_id=customer.id, reason="Marked 'do not text' by staff")
    db.commit()
    add_audit(db, staff, "SMS_OPT_OUT" if opted_out else "SMS_OPT_IN",
              f"{customer.name} {'marked do-not-text' if opted_out else 'allowed to be texted again'} by staff")


# ── settings form ─────────────────────────────────────────────────────────

def save_settings(db, form: dict) -> list[str]:
    """Validate and store the Messaging settings form. Returns a list of
    problems; if there are any, NOTHING is saved."""
    errs: list[str] = []
    flag = lambda k: "true" if str(form.get(k, "")).lower() in ("on", "true", "1", "yes") else "false"
    new: dict[str, str] = {}
    for k in ("msg_enabled", "msg_ready_enabled", "msg_remind_enabled", "msg_review_enabled", "msg_winback_enabled"):
        new[k] = flag(k)

    def num(k, label, lo, hi):
        raw = str(form.get(k, DEFAULTS[k])).strip()
        try:
            v = int(raw)
        except ValueError:
            errs.append(f"{label} must be a whole number.")
            return
        if not lo <= v <= hi:
            errs.append(f"{label} must be between {lo} and {hi}.")
            return
        new[k] = str(v)

    num("msg_ready_delay_min", "Delay before the ready text (minutes)", 0, 120)
    num("msg_review_delay_days", "Days to wait before asking for a review", 0, 30)
    num("msg_winback_after_days", "Win-back: days since last visit", 30, 730)
    num("msg_winback_cooldown_days", "Win-back: days between texts to one customer", 30, 730)
    num("msg_winback_per_day", "Win-back: texts per day", 1, 100)
    num("msg_quiet_start", "Earliest sending hour", 0, 23)
    num("msg_quiet_end", "Latest sending hour", 1, 24)
    num("msg_daily_cap", "Daily text limit", 1, 2000)
    if "msg_quiet_start" in new and "msg_quiet_end" in new and int(new["msg_quiet_start"]) >= int(new["msg_quiet_end"]):
        errs.append("The earliest sending hour must be before the latest one.")

    raw_days = str(form.get("msg_remind_days", DEFAULTS["msg_remind_days"])).strip()
    if not re.fullmatch(r"\d{1,2}([,\s]+\d{1,2}){0,4}", raw_days) or not all(1 <= int(x) <= 60 for x in re.findall(r"\d+", raw_days)):
        errs.append("Reminder days must be 1–5 numbers between 1 and 60, like 3, 7, 14.")
    else:
        new["msg_remind_days"] = ", ".join(str(d) for d in parse_days(raw_days))

    cc = re.sub(r"\D", "", str(form.get("msg_country_code", "1")))
    if not cc or len(cc) > 3:
        errs.append("Country code must be 1–3 digits (Canada and the US are 1).")
    else:
        new["msg_country_code"] = cc

    link = str(form.get("msg_review_link", "")).strip()
    if link and not valid_link(link):
        errs.append("The review link must start with http:// or https://.")
    else:
        new["msg_review_link"] = link
    if new.get("msg_review_enabled") == "true" and not link:
        errs.append("Add your Google review link before switching review texts on.")

    new["msg_shop_phone"] = re.sub(r"[^\d+()\-\s.]", "", str(form.get("msg_shop_phone", "")))[:25].strip()

    for k in KINDS:
        t = str(form.get(f"msg_tpl_{k}", "")).replace("\r", "").strip()
        if t == DEFAULT_TEMPLATES[k]:
            t = ""                                          # store only real customisations
        if t:
            probs = template_problems(t)
            if probs:
                errs.append(f"The {k} message " + "; ".join(probs) + ".")
        new[f"msg_tpl_{k}"] = t

    if errs:
        return errs
    for k, v in new.items():
        _set(db, k, v)
    db.commit()
    return []


# ── what the pages need ───────────────────────────────────────────────────

def repair_activity(db, repair_id: str) -> list:
    return (db.query(OutboundMessage).filter(OutboundMessage.repair_id == repair_id)
            .order_by(OutboundMessage.due_at, OutboundMessage.created_at).all())


def summary(db, now: datetime | None = None) -> dict:
    now = now or utcnow()
    tz = get_shop_tz(db)
    cfg = load_config(db)
    d = to_local_date(now, tz)
    s, e = day_bounds_utc(d, d, tz)
    def cnt(*conds):
        return db.query(func.count(OutboundMessage.id)).filter(*conds).scalar() or 0
    return {
        "cfg": cfg,
        "configured": twilio_configured(db),
        "pending": cnt(OutboundMessage.status == "pending"),
        "sent_today": cnt(OutboundMessage.status == "sent", OutboundMessage.sent_at >= s, OutboundMessage.sent_at < e),
        "failed_7d": cnt(OutboundMessage.status == "failed", OutboundMessage.created_at >= now - timedelta(days=7)),
        "skipped_7d": cnt(OutboundMessage.status == "skipped", OutboundMessage.created_at >= now - timedelta(days=7)),
        "opted_out": db.query(func.count(Customer.id)).filter(Customer.sms_opt_out.is_(True)).scalar() or 0,
        "in_window": in_window(now, tz, cfg.quiet_start, cfg.quiet_end),
        "waiting_devices": len(catchup_candidates(db, now, cfg)) if cfg.enabled else 0,
    }
