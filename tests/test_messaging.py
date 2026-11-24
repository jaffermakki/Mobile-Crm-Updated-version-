"""
Tests for v46 automatic customer messaging (app/messaging.py, messaging_routes.py).

Engine tests call the functions directly with a fixed, fake clock (2031) and a
fake sender, so nothing here can reach Twilio and nothing depends on the time
of day the suite runs. The shared test database is not reset between tests,
so an autouse fixture clears the text queue and the msg_* settings around
every test, and every customer/phone created here is unique.
"""
import io
import itertools
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from openpyxl import load_workbook
from sqlalchemy import inspect as sa_inspect

from app import messaging as M
from app import reporting as R
from app.database import SessionLocal, engine
from app.encryption import encrypt_value
from app.localtime import load_zone
from app.models import AuditLog, Customer, Invoice, OutboundMessage, Repair, Setting, SmsMessage

NOW = datetime(2031, 3, 10, 18, 0)           # a Monday, 18:00 UTC
PACIFIC = load_zone("America/Vancouver")
GOOD_LINK = "https://g.page/r/test-review-link"
_seq = itertools.count(1)
MSG_KEYS = list(M.DEFAULTS) + [f"msg_tpl_{k}" for k in M.KINDS]
TWILIO_KEYS = ["twilio_sid", "twilio_token", "twilio_from"]


# ── fixtures & helpers ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean(db_session):
    """Empty queue + default messaging settings before and after every test."""
    def wipe():
        db_session.rollback()
        db_session.query(OutboundMessage).delete()
        db_session.query(Setting).filter(Setting.key.in_(MSG_KEYS)).delete(synchronize_session=False)
        db_session.commit()
        db_session.expire_all()
    wipe()
    yield
    wipe()


@pytest.fixture
def twilio_off(db_session):
    """Guarantee no Twilio login is saved (restored afterwards)."""
    saved = {k: db_session.get(Setting, k).value for k in TWILIO_KEYS if db_session.get(Setting, k)}
    db_session.query(Setting).filter(Setting.key.in_(TWILIO_KEYS)).delete(synchronize_session=False)
    db_session.commit()
    yield
    for k, v in saved.items():
        M._set(db_session, k, v)
    db_session.commit()


@pytest.fixture
def twilio_on(db_session):
    saved = {k: db_session.get(Setting, k).value for k in TWILIO_KEYS if db_session.get(Setting, k)}
    M._set(db_session, "twilio_sid", "ACtest")
    M._set(db_session, "twilio_token", encrypt_value("tok"))
    M._set(db_session, "twilio_from", "+15005550006")
    db_session.commit()
    yield
    db_session.query(Setting).filter(Setting.key.in_(TWILIO_KEYS)).delete(synchronize_session=False)
    db_session.commit()
    for k, v in saved.items():
        M._set(db_session, k, v)
    db_session.commit()


def setcfg(db, **kw):
    for k, v in kw.items():
        key = k if k.startswith("msg_") else "msg_" + k
        M._set(db, key, ("true" if v else "false") if isinstance(v, bool) else str(v))
    db.commit()


def enable(db, **kw):
    """Switch messaging on and open the sending window 24h a day."""
    setcfg(db, enabled=True, quiet_start=0, quiet_end=24, **kw)


class Sender:
    """Stands in for Twilio. Each outcome is a dict overriding a successful reply."""
    def __init__(self, *outcomes):
        self.calls, self.outcomes = [], list(outcomes)

    def __call__(self, db, to, body):
        self.calls.append((to, body))
        res = {"ok": True, "message": "SMS sent successfully.", "error_code": "", "sid": f"SM{len(self.calls):04d}",
               "retryable": False, "configured": True, "to": to}
        if self.outcomes:
            res.update(self.outcomes.pop(0))
        return res


FAIL_500 = {"ok": False, "error_code": "500", "retryable": True, "message": "Twilio error (500)"}


def phone_n():
    return f"(250) 555-{4000 + next(_seq):04d}"


def new_customer(db, name="Alex Tester", consent=False, spent=0.0, optout=False, phone=None):
    c = Customer(id=f"msg-c-{uuid.uuid4().hex[:10]}", name=name, phone=phone_n() if phone is None else phone,
                 marketing_consent=consent, spent=spent, sms_opt_out=optout)
    db.add(c)
    db.commit()
    return c


def new_repair(db, cust, status="IN_PROGRESS", device="Pixel 8", ready_ago=None, now=NOW):
    hist = []
    if ready_ago is not None:
        hist = [{"status": "READY", "note": "", "date": (now - ready_ago).isoformat()}]
    r = Repair(id=f"msg-r-{uuid.uuid4().hex[:10]}", ticket_no=7000 + next(_seq), customer_id=cust.id, device=device, status=status,
               status_history=json.dumps(hist), created_at=now - timedelta(days=10), updated_at=now - (ready_ago or timedelta(0)))
    db.add(r)
    db.commit()
    return r


def ready_repair(db, now=NOW, **kw):
    """Customer + repair already queued by the 'became Ready' hook. Returns (customer, repair)."""
    cust = new_customer(db, **kw)
    rep = new_repair(db, cust, status="READY", ready_ago=timedelta(0), now=now)
    M.on_repair_ready(db, rep, now=now)
    db.commit()
    return cust, rep


def rows(db, **filters):
    db.expire_all()
    return db.query(OutboundMessage).filter_by(**filters).order_by(OutboundMessage.due_at, OutboundMessage.seq).all()


def raw_row(db, status="pending", kind="ready", **kw):
    r = OutboundMessage(kind=kind, dedupe_key=f"raw:{uuid.uuid4().hex}", status=status, due_at=kw.pop("due_at", NOW), **kw)
    db.add(r)
    db.commit()
    return r


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure helpers
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("(250) 555-4001", "+12505554001"), ("250.555.4001", "+12505554001"), ("250 555 4001", "+12505554001"),
    ("1-250-555-4001", "+12505554001"), ("+1 (250) 555-4001", "+12505554001"), ("+12505554001", "+12505554001"),
    ("  250-555-4001  ", "+12505554001"), ("+44 7911 123456", "+447911123456"), ("0044 7911 123456", "+447911123456"),
])
def test_normalize_phone_accepts_real_numbers(raw, expected):
    assert M.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "N/A", "none", "555-1234", "250 555 40", "(111) 555-4001", "(250) 155-4001", "(911) 555-0100",
    "(250) 411-4001", "250-555-4001 ext 5", "x12", "+12505554001234", "+1 250 555", "12345678901234567890", "+", "()",
])
def test_normalize_phone_rejects_what_cannot_receive_a_text(raw):
    assert M.normalize_phone(raw) is None


def test_normalize_phone_other_default_country():
    assert M.normalize_phone("7911 123456", "44") == "+447911123456"
    assert M.normalize_phone("07911 123456", "44") == "+447911123456"
    assert M.normalize_phone("+44 7911 123456", "44") == "+447911123456"


def test_sms_info_counts_segments():
    assert M.sms_info("") == {"encoding": "GSM-7", "units": 0, "segments": 0}
    assert M.sms_info("a" * 160)["segments"] == 1
    assert M.sms_info("a" * 161)["segments"] == 2
    assert M.sms_info("a" * 306)["segments"] == 2 and M.sms_info("a" * 307)["segments"] == 3
    assert M.sms_info("[" * 80)["segments"] == 1 and M.sms_info("[" * 81)["segments"] == 2      # [ costs 2 units
    emoji = M.sms_info("Ready \U0001F600")
    assert emoji["encoding"] == "Unicode" and emoji["segments"] == 1
    assert M.sms_info("’" * 70)["segments"] == 1 and M.sms_info("’" * 71)["segments"] == 2   # curly apostrophe = Unicode


def test_render_replaces_only_known_placeholders():
    vals = {"name": "Alex", "shop": "TechPro+", "device": "Pixel"}
    out = M.render("Hi {name} at {shop} {__class__} {0} {evil.__init__} {unknown}", vals)
    assert out.startswith("Hi Alex at TechPro+")
    assert "__class__" not in out and "unknown" not in out and "{name}" not in out and "{shop}" not in out


def test_render_tidies_empty_placeholders():
    assert M.render("Hello {name} ({shop_phone}).", {"name": "Alex", "shop_phone": ""}) == "Hello Alex."
    assert M.render("A  {x}  B", {}) == "A B"


@pytest.mark.parametrize("raw,expected", [("Alex Tester", "Alex"), ("", "there"), ("  ", "there"), ("Walk-in", "there"),
                                          ("guest", "there"), ("Mary\nAnn Smith", "Mary"), (None, "there")])
def test_first_name(raw, expected):
    assert M.first_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3, 7,14", [3, 7, 14]), ("14 7 3 3", [3, 7, 14]), ("abc", [3, 7, 14]), ("", [3, 7, 14]), ("0, 99", [3, 7, 14]),
    ("1,2,3,4,5,6,7", [1, 2, 3, 4, 5]), ("2", [2]),
])
def test_parse_days(raw, expected):
    assert M.parse_days(raw) == expected


def test_template_problems():
    assert M.template_problems("Hi {name}, ticket {ticket}") == []
    assert any("unknown" in p for p in M.template_problems("Hi {nmae}"))
    assert any("empty" in p for p in M.template_problems("   "))
    assert any("320" in p for p in M.template_problems("x" * 321))
    for k, t in M.DEFAULT_TEMPLATES.items():
        assert M.template_problems(t) == [], k


def test_with_opt_out_rules():
    assert "STOP" not in M.with_opt_out("ready", "Your phone is ready.")
    for kind in ("reminder", "review", "winback"):
        out = M.with_opt_out(kind, "Hello.")
        assert out.endswith(M.OPT_OUT_LINE), kind
        assert M.with_opt_out(kind, out) == out                                  # never added twice
    assert M.with_opt_out("review", "Non-stop service!").endswith(M.OPT_OUT_LINE)  # the word 'stop' alone is not an unsubscribe line


@pytest.mark.parametrize("body,expected", [
    ("STOP", "stop"), ("stop.", "stop"), (" Stop ", "stop"), ("UNSUBSCRIBE", "stop"), ("cancel", "stop"), ("QUIT", "stop"),
    ("END", "stop"), ("STOPALL", "stop"), ("Stop texting me", "stop"),
    ("START", "start"), ("yes", "start"), ("Unstop", "start"),
    ("Yes please", None), ("Is my phone ready?", None), ("", None), ("123", None), ("\U0001F642", None),
    ("Stop by tomorrow to pick it up please ok", None), (None, None),
])
def test_classify_reply(body, expected):
    assert M.classify_reply(body) == expected


def test_in_window_boundaries_in_shop_time():
    assert M.in_window(datetime(2031, 3, 10, 9, 0), load_zone("UTC"), 9, 20)
    assert not M.in_window(datetime(2031, 3, 10, 8, 59), load_zone("UTC"), 9, 20)
    assert M.in_window(datetime(2031, 3, 10, 19, 59), load_zone("UTC"), 9, 20)
    assert not M.in_window(datetime(2031, 3, 10, 20, 0), load_zone("UTC"), 9, 20)
    # Vancouver is UTC-7 in March 2031 (daylight time began Mar 9)
    assert M.in_window(datetime(2031, 3, 10, 2, 0), PACIFIC, 9, 20)       # 19:00 local, still inside
    assert not M.in_window(datetime(2031, 3, 10, 4, 0), PACIFIC, 9, 20)   # 21:00 local
    assert not M.in_window(datetime(2031, 3, 10, 14, 0), load_zone("UTC"), 15, 20)


# ═══════════════════════════════════════════════════════════════════════
# 2. Settings
# ═══════════════════════════════════════════════════════════════════════

def good_form(**over):
    f = {"msg_enabled": "on", "msg_ready_enabled": "on", "msg_ready_delay_min": "10", "msg_remind_enabled": "on",
         "msg_remind_days": "2, 5, 9", "msg_review_delay_days": "4", "msg_review_link": GOOD_LINK, "msg_review_enabled": "on",
         "msg_winback_after_days": "100", "msg_winback_cooldown_days": "200", "msg_winback_per_day": "7",
         "msg_quiet_start": "8", "msg_quiet_end": "21", "msg_daily_cap": "90", "msg_country_code": "1",
         "msg_shop_phone": "250-555-0100"}
    for k in M.KINDS:
        f[f"msg_tpl_{k}"] = M.DEFAULT_TEMPLATES[k]
    f.update(over)
    return f


def test_defaults_are_safe(db_session):
    cfg = M.load_config(db_session)
    assert cfg.enabled is False and cfg.review_enabled is False and cfg.winback_enabled is False
    assert (cfg.quiet_start, cfg.quiet_end, cfg.daily_cap) == (9, 20, 150)
    assert cfg.remind_days == [3, 7, 14] and cfg.country_code == "1"


def test_save_settings_roundtrip(db_session):
    assert M.save_settings(db_session, good_form()) == []
    cfg = M.load_config(db_session)
    assert cfg.enabled and cfg.review_enabled and not cfg.winback_enabled
    assert cfg.ready_delay_min == 10 and cfg.remind_days == [2, 5, 9] and cfg.review_delay_days == 4
    assert cfg.review_link == GOOD_LINK and cfg.winback_per_day == 7 and cfg.daily_cap == 90
    assert (cfg.quiet_start, cfg.quiet_end) == (8, 21) and cfg.shop_phone == "250-555-0100"


def test_save_settings_unticked_boxes_turn_things_off(db_session):
    M.save_settings(db_session, good_form())
    f = good_form()
    for k in ("msg_enabled", "msg_ready_enabled", "msg_remind_enabled", "msg_review_enabled"):
        f.pop(k)
    f["msg_review_link"] = ""
    assert M.save_settings(db_session, f) == []
    cfg = M.load_config(db_session)
    assert not (cfg.enabled or cfg.ready_enabled or cfg.remind_enabled or cfg.review_enabled)


@pytest.mark.parametrize("over,fragment", [
    ({"msg_daily_cap": "abc"}, "Daily text limit"), ({"msg_daily_cap": "0"}, "Daily text limit"), ({"msg_daily_cap": "99999"}, "Daily text limit"),
    ({"msg_quiet_start": "20", "msg_quiet_end": "9"}, "earliest sending hour"), ({"msg_quiet_start": "9", "msg_quiet_end": "9"}, "earliest sending hour"),
    ({"msg_quiet_start": "-1"}, "Earliest sending hour"), ({"msg_quiet_end": "25"}, "Latest sending hour"),
    ({"msg_remind_days": "0"}, "Reminder days"), ({"msg_remind_days": "1,2,3,4,5,6"}, "Reminder days"), ({"msg_remind_days": "x"}, "Reminder days"),
    ({"msg_remind_days": "61"}, "Reminder days"), ({"msg_country_code": "abcd"}, "Country code"), ({"msg_country_code": "1234"}, "Country code"),
    ({"msg_review_link": "not a url"}, "review link"), ({"msg_review_link": "javascript:alert(1)"}, "review link"),
    ({"msg_review_link": ""}, "review link"),                                   # review texts on with no link
    ({"msg_tpl_ready": "Hi {nmae}"}, "unknown placeholder"), ({"msg_tpl_winback": "x" * 400}, "320"),
    ({"msg_ready_delay_min": "-5"}, "Delay before the ready text"), ({"msg_winback_per_day": "0"}, "texts per day"),
    ({"msg_winback_after_days": "5"}, "days since last visit"), ({"msg_review_delay_days": "99"}, "Days to wait"),
])
def test_save_settings_rejects_bad_input_and_saves_nothing(db_session, over, fragment):
    M.save_settings(db_session, good_form(msg_daily_cap="55"))                     # known baseline (ready delay 10, daily cap 55)
    form = good_form(**{"msg_ready_delay_min": "33", **over})                      # a form that changes several things at once, one of them bad
    errs = M.save_settings(db_session, form)
    assert errs and any(fragment.lower() in e.lower() for e in errs), errs
    db_session.expire_all()
    cfg = M.load_config(db_session)
    assert cfg.daily_cap == 55 and cfg.ready_delay_min == 10                       # untouched: the baseline, not the new form


def test_default_wording_is_not_stored_as_a_customisation(db_session):
    M.save_settings(db_session, good_form())
    assert db_session.get(Setting, "msg_tpl_ready").value == ""
    M.save_settings(db_session, good_form(msg_tpl_ready="Hey {name}, {device} is done!"))
    assert M.load_config(db_session).templates["ready"] == "Hey {name}, {device} is done!"
    M.save_settings(db_session, good_form(msg_tpl_ready=""))                       # blank = back to default
    assert M.load_config(db_session).templates["ready"] == M.DEFAULT_TEMPLATES["ready"]


def test_shop_phone_is_sanitised(db_session):
    M.save_settings(db_session, good_form(msg_shop_phone="<b>250</b> 555-0100; DROP"))
    phone = M.load_config(db_session).shop_phone
    assert "<" not in phone and ";" not in phone and "DROP" not in phone and "250" in phone


def test_load_config_survives_garbage_in_settings(db_session):
    setcfg(db_session, daily_cap="lots", quiet_start="25", quiet_end="3", remind_days="a,b", ready_delay_min="x")
    cfg = M.load_config(db_session)
    assert cfg.daily_cap == 150 and (cfg.quiet_start, cfg.quiet_end) == (9, 20) and cfg.remind_days == [3, 7, 14] and cfg.ready_delay_min == 5


# ═══════════════════════════════════════════════════════════════════════
# 3. Hooks: what gets queued, and when
# ═══════════════════════════════════════════════════════════════════════

def test_nothing_is_queued_while_switched_off(db_session):
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="READY", ready_ago=timedelta(0))
    assert M.on_repair_ready(db_session, rep, now=NOW) == 0
    assert rows(db_session) == []


def test_ready_queues_ready_text_and_reminder_ladder(db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session)
    rs = rows(db_session)
    assert [(r.kind, r.seq) for r in rs] == [("ready", 0), ("reminder", 1), ("reminder", 2), ("reminder", 3)]
    assert rs[0].due_at == NOW + timedelta(minutes=5)
    assert [r.due_at for r in rs[1:]] == [NOW + timedelta(days=d) for d in (3, 7, 14)]
    assert all(r.status == "pending" and r.source == "auto" and r.repair_id == rep.id and r.customer_id == cust.id for r in rs)


def test_ready_hook_is_idempotent(db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session)
    assert M.on_repair_ready(db_session, rep, now=NOW + timedelta(hours=1)) == 0
    db_session.commit()
    assert len(rows(db_session)) == 4


def test_custom_ladder_and_delay_are_used(db_session):
    enable(db_session, ready_delay_min=0, remind_days="2, 10")
    _, rep = ready_repair(db_session)
    rs = rows(db_session)
    assert [(r.kind, r.due_at) for r in rs] == [("ready", NOW), ("reminder", NOW + timedelta(days=2)), ("reminder", NOW + timedelta(days=10))]


def test_ready_only_and_reminders_only(db_session):
    enable(db_session, remind_enabled=False)
    ready_repair(db_session)
    assert [r.kind for r in rows(db_session)] == ["ready"]
    db_session.query(OutboundMessage).delete(); db_session.commit()
    setcfg(db_session, remind_enabled=True, ready_enabled=False)
    ready_repair(db_session)
    assert [r.kind for r in rows(db_session)] == ["reminder"] * 3


@pytest.mark.parametrize("kw", [{"phone": ""}, {"phone": "   "}, {"phone": "N/A"}, {"phone": "555-1234"}, {"optout": True}])
def test_unreachable_customers_are_never_queued(db_session, kw):
    enable(db_session)
    ready_repair(db_session, **kw)
    assert rows(db_session) == []


def test_ticket_without_customer_is_ignored(db_session):
    enable(db_session)
    rep = Repair(id=f"msg-r-{uuid.uuid4().hex[:8]}", ticket_no=7999, customer_id=None, device="Walk-in thing", status="READY")
    db_session.add(rep); db_session.commit()
    assert M.on_repair_ready(db_session, rep, now=NOW) == 0


def test_collecting_cancels_everything_still_waiting(db_session):
    enable(db_session)
    _, rep = ready_repair(db_session)
    M.on_repair_collected(db_session, rep, now=NOW + timedelta(hours=1)); db_session.commit()
    rs = rows(db_session)
    assert {r.status for r in rs} == {"cancelled"} and all(r.skip_reason == "Device collected" for r in rs)


@pytest.mark.parametrize("review_on,link,consent,queued", [
    (True, GOOD_LINK, True, 1), (True, "", True, 0), (True, "not a link", True, 0), (True, GOOD_LINK, False, 0), (False, GOOD_LINK, True, 0),
])
def test_review_request_needs_switch_link_and_consent(db_session, review_on, link, consent, queued):
    enable(db_session, review_enabled=review_on, review_link=link, review_delay_days=3)
    cust = new_customer(db_session, consent=consent)
    rep = new_repair(db_session, cust, status="COLLECTED")
    assert M.on_repair_collected(db_session, rep, now=NOW) == queued
    db_session.commit()
    rs = rows(db_session, kind="review")
    assert len(rs) == queued
    if queued:
        assert rs[0].due_at == NOW + timedelta(days=3)


def test_review_not_queued_for_opted_out_or_phoneless(db_session):
    enable(db_session, review_enabled=True, review_link=GOOD_LINK)
    for kw in ({"optout": True}, {"phone": ""}):
        cust = new_customer(db_session, consent=True, **kw)
        rep = new_repair(db_session, cust, status="COLLECTED")
        assert M.on_repair_collected(db_session, rep, now=NOW) == 0
    assert rows(db_session) == []


def test_manual_ready_text_cancels_and_blocks_the_automatic_one(db_session):
    enable(db_session, ready_delay_min=0)
    cust, rep = ready_repair(db_session)
    M.record_manual_ready(db_session, rep, cust.phone, "Hi, ready!", "Test Owner"); db_session.commit()
    rs = {(r.kind, r.status) for r in rows(db_session)}
    assert ("ready", "cancelled") in rs and ("ready", "sent") in rs
    s = Sender()
    st = M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert s.calls == [] and st["sent"] == 0
    # the manual one never counts against the daily limit
    assert M._sent_today(db_session, load_zone("UTC"), NOW) == 0


def test_a_reminder_right_after_any_other_text_is_skipped(db_session):
    """A reminder due within 36h of a text already sent for the same ticket is skipped (no double-texting)."""
    enable(db_session, remind_days="1")
    cust, rep = ready_repair(db_session)
    M.record_manual_ready(db_session, rep, cust.phone, "Hi, ready!", "Test Owner"); db_session.commit()
    manual = db_session.query(OutboundMessage).filter(OutboundMessage.source == "manual").one()
    manual.sent_at = NOW + timedelta(days=1) - timedelta(hours=10); db_session.commit()
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(days=1, hours=1), send=s)
    assert s.calls == []
    assert rows(db_session, kind="reminder")[0].skip_reason.startswith("Customer was texted")


# ═══════════════════════════════════════════════════════════════════════
# 4. Sending
# ═══════════════════════════════════════════════════════════════════════

def test_due_ready_text_is_sent_once_to_the_e164_number(db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session, name="Jordan Lee", phone="(250) 555-4900")
    s = Sender()
    st = M.process_due(db_session, now=NOW + timedelta(minutes=6), send=s)
    assert st["sent"] == 1 and len(s.calls) == 1
    to, body = s.calls[0]
    assert to == "+12505554900"
    assert "Jordan" in body and "Pixel 8" in body and f"#{rep.ticket_no}" in body
    assert "STOP" not in body and "{" not in body                              # first text carries no unsubscribe line
    r = rows(db_session, kind="ready")[0]
    assert (r.status, r.attempts, r.to_phone, r.provider_sid, r.body) == ("sent", 1, "+12505554900", "SM0001", body)
    assert r.sent_at == NOW + timedelta(minutes=6)
    logged = db_session.query(SmsMessage).filter(SmsMessage.customer_id == cust.id).one()
    assert logged.direction == "out" and logged.staff_name == "Auto" and logged.body == body
    # running again must not text again
    assert M.process_due(db_session, now=NOW + timedelta(minutes=7), send=s)["sent"] == 0 and len(s.calls) == 1


def test_not_yet_due_is_left_alone(db_session):
    enable(db_session)
    ready_repair(db_session)
    s = Sender()
    st = M.process_due(db_session, now=NOW + timedelta(minutes=2), send=s)
    assert s.calls == [] and st["sent"] == 0
    assert {r.status for r in rows(db_session)} == {"pending"}


def test_reminder_wording_days_and_unsubscribe_line(db_session):
    enable(db_session, ready_enabled=False)
    _, rep = ready_repair(db_session)
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(days=3, hours=1), send=s)
    assert len(s.calls) == 1
    body = s.calls[0][1]
    assert "for 3 days" in body and body.endswith(M.OPT_OUT_LINE)
    M.process_due(db_session, now=NOW + timedelta(days=7, hours=1), send=s)
    assert "for 7 days" in s.calls[1][1]


def test_text_is_skipped_when_too_late_to_be_useful(db_session):
    enable(db_session, remind_enabled=False)
    ready_repair(db_session)
    s = Sender()
    st = M.process_due(db_session, now=NOW + timedelta(days=5), send=s)        # ready text was due 5 days ago
    r = rows(db_session, kind="ready")[0]
    assert r.status == "skipped" and "late" in r.skip_reason.lower() and s.calls == []
    assert st["skipped"] >= 1


def test_switched_off_means_the_queue_is_left_alone(db_session):
    enable(db_session)
    ready_repair(db_session)
    setcfg(db_session, enabled=False)
    s = Sender()
    st = M.process_due(db_session, now=NOW + timedelta(minutes=6), send=s)
    assert s.calls == [] and "off" in st["deferred"].lower()
    assert rows(db_session, kind="ready")[0].status == "pending"


def test_quiet_hours_hold_texts_back_then_release_them(db_session):
    setcfg(db_session, enabled=True, quiet_start=9, quiet_end=20, ready_delay_min=0)
    ready_repair(db_session, now=datetime(2031, 3, 10, 2, 0))                  # became ready at 02:00 UTC
    s = Sender()
    st = M.process_due(db_session, now=datetime(2031, 3, 10, 3, 0), send=s)
    assert s.calls == [] and "hours" in st["deferred"].lower()
    assert rows(db_session, kind="ready")[0].status == "pending"
    st = M.process_due(db_session, now=datetime(2031, 3, 10, 9, 30), send=s)
    assert st["sent"] == 1


def test_run_now_can_ignore_quiet_hours(db_session):
    setcfg(db_session, enabled=True, quiet_start=9, quiet_end=20, ready_delay_min=0)
    ready_repair(db_session, now=datetime(2031, 3, 10, 2, 0))
    s = Sender()
    assert M.process_due(db_session, now=datetime(2031, 3, 10, 3, 0), send=s, ignore_hours=True)["sent"] == 1


def test_daily_limit_then_next_day_continues(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False, daily_cap=2)
    for _ in range(5):
        ready_repair(db_session)
    s = Sender()
    assert M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)["sent"] == 2
    st = M.process_due(db_session, now=NOW + timedelta(minutes=2), send=s)
    assert st["sent"] == 0 and "limit" in st["deferred"].lower() and len(s.calls) == 2
    assert M.process_due(db_session, now=NOW + timedelta(days=1), send=s)["sent"] == 2     # a new shop day
    assert len(rows(db_session, status="pending")) == 1


def test_manual_texts_do_not_use_up_the_daily_limit(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False, daily_cap=1)
    cust, rep = ready_repair(db_session)
    other = new_repair(db_session, cust, status="READY", ready_ago=timedelta(0))
    M.record_manual_ready(db_session, other, cust.phone, "hi", "Owner"); db_session.commit()
    s = Sender()
    assert M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)["sent"] == 1


def test_batch_limit(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    for _ in range(4):
        ready_repair(db_session)
    s = Sender()
    assert M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s, limit=3)["sent"] == 3
    assert M.process_due(db_session, now=NOW + timedelta(minutes=2), send=s, limit=3)["sent"] == 1


def test_temporary_failure_retries_with_backoff_then_gives_up(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    s = Sender(FAIL_500, FAIL_500, FAIL_500)
    t = NOW + timedelta(minutes=1)
    st = M.process_due(db_session, now=t, send=s)
    r = rows(db_session)[0]
    assert st["retry"] == 1 and (r.status, r.attempts, r.due_at) == ("pending", 1, t + timedelta(minutes=15))
    assert M.process_due(db_session, now=t + timedelta(minutes=5), send=s) == {"sent": 0, "failed": 0, "skipped": 0, "retry": 0, "deferred": "", "aborted": ""}
    assert len(s.calls) == 1                                                   # not due yet
    t2 = t + timedelta(minutes=16)
    M.process_due(db_session, now=t2, send=s)
    r = rows(db_session)[0]
    assert (r.status, r.attempts, r.due_at) == ("pending", 2, t2 + timedelta(hours=1))
    t3 = t2 + timedelta(hours=1, minutes=1)
    st = M.process_due(db_session, now=t3, send=s)
    r = rows(db_session)[0]
    assert st["failed"] == 1 and (r.status, r.attempts, r.error_code) == ("failed", 3, "500") and "500" in r.last_error
    assert len(s.calls) == 3
    assert M.process_due(db_session, now=t3 + timedelta(hours=1), send=s)["sent"] == 0 and len(s.calls) == 3     # a failed row is never re-sent on its own


def test_retry_that_succeeds(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    s = Sender(FAIL_500)
    t = NOW + timedelta(minutes=1)
    M.process_due(db_session, now=t, send=s)
    M.process_due(db_session, now=t + timedelta(minutes=16), send=s)
    r = rows(db_session)[0]
    assert (r.status, r.attempts, r.error_code, r.last_error) == ("sent", 2, "", "")


def test_permanent_failure_is_not_retried(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    s = Sender({"ok": False, "error_code": "21211", "retryable": False, "message": "Invalid 'To' phone number"})
    st = M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    r = rows(db_session)[0]
    assert st["failed"] == 1 and (r.status, r.attempts, r.error_code) == ("failed", 1, "21211")
    assert M.process_due(db_session, now=NOW + timedelta(hours=2), send=s)["sent"] == 0 and len(s.calls) == 1


def test_carrier_stop_error_records_opt_out_and_stops_later_texts(db_session):
    enable(db_session, ready_delay_min=0)
    cust, rep = ready_repair(db_session)
    s = Sender({"ok": False, "error_code": "21610", "retryable": False, "message": "unsubscribed"})
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is True and db_session.get(Customer, cust.id).sms_opt_out_at
    M.process_due(db_session, now=NOW + timedelta(days=3, hours=1), send=s)
    assert len(s.calls) == 1
    assert "opted out" in rows(db_session, kind="reminder")[0].skip_reason.lower()


@pytest.mark.parametrize("outcome", [{"configured": False, "error_code": "not_configured"}, {"error_code": "20003"}, {"error_code": "20404"}])
def test_setup_problems_stop_the_whole_run_and_keep_everything_waiting(db_session, outcome):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    ready_repair(db_session)
    s = Sender(dict({"ok": False, "message": "SMS is not configured"}, **outcome))
    st = M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert st["aborted"] and st["sent"] == 0 and len(s.calls) == 1             # stopped after the first
    rs = rows(db_session)
    assert [(r.status, r.attempts) for r in rs] == [("pending", 0), ("pending", 0)]
    # once fixed, both go out
    assert M.process_due(db_session, now=NOW + timedelta(minutes=30), send=Sender())["sent"] == 2


def test_default_sender_without_twilio_login_aborts_cleanly(db_session, twilio_off):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    st = M.process_due(db_session, now=NOW + timedelta(minutes=1))              # real sender, nothing saved
    assert "not configured" in st["aborted"].lower()
    assert rows(db_session)[0].status == "pending"


def test_a_crash_while_sending_is_not_resent_automatically(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    r = rows(db_session)[0]
    r.status, r.claimed_at = "sending", NOW - timedelta(minutes=30)             # app died mid-send
    db_session.commit()
    s = Sender()
    M.process_due(db_session, now=NOW, send=s)
    r = rows(db_session)[0]
    assert s.calls == [] and r.status == "failed" and r.error_code == "unknown" and "Twilio" in r.last_error


def test_a_row_being_sent_right_now_is_left_alone(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    r = rows(db_session)[0]
    r.status, r.claimed_at = "sending", NOW - timedelta(minutes=2)
    db_session.commit()
    s = Sender()
    M.process_due(db_session, now=NOW, send=s)
    assert s.calls == [] and rows(db_session)[0].status == "sending"


def test_two_workers_cannot_claim_the_same_text(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    mid = rows(db_session)[0].id
    other = SessionLocal()
    try:
        assert M._claim(db_session, mid, NOW) is True
        assert M._claim(other, mid, NOW) is False
    finally:
        other.close()


def test_one_bad_row_does_not_block_the_rest(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session)
    ready_repair(db_session)
    calls = []

    def flaky(db, to, body):
        calls.append(to)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True, "message": "ok", "sid": "SMx", "to": to, "error_code": "", "retryable": False, "configured": True}
    st = M.process_due(db_session, now=NOW + timedelta(minutes=1), send=flaky)
    assert st["sent"] == 1 and st["failed"] == 1
    bad = db_session.query(OutboundMessage).filter(OutboundMessage.status == "failed").one()
    assert bad.error_code == "internal" and "boom" in bad.last_error


@pytest.mark.parametrize("change,fragment", [
    (lambda c, r: setattr(r, "status", "COLLECTED"), "collected"),
    (lambda c, r: setattr(r, "status", "IN_PROGRESS"), "no longer waiting"),
    (lambda c, r: setattr(c, "sms_opt_out", True), "opted out"),
    (lambda c, r: setattr(c, "phone", "call me"), "valid mobile"),
    (lambda c, r: setattr(c, "phone", ""), "valid mobile"),
])
def test_wishes_are_rechecked_at_send_time(db_session, change, fragment):
    enable(db_session, ready_delay_min=0)
    cust, rep = ready_repair(db_session)
    change(cust, rep)
    db_session.commit()
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    r = rows(db_session, kind="ready")[0]
    assert s.calls == [] and r.status == "skipped" and fragment in r.skip_reason.lower(), r.skip_reason


def test_a_changed_phone_number_is_used_not_the_one_at_queue_time(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    cust, rep = ready_repair(db_session, phone="(250) 555-4011")
    cust.phone = "(250) 555-4022"; db_session.commit()
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert s.calls[0][0] == "+12505554022"


def test_completed_status_still_counts_as_waiting_for_pickup(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    cust, rep = ready_repair(db_session)
    rep.status = "COMPLETED"; db_session.commit()
    s = Sender()
    assert M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)["sent"] == 1


def test_promotional_text_needs_consent_at_send_time(db_session):
    enable(db_session, review_enabled=True, review_link=GOOD_LINK, review_delay_days=0)
    cust = new_customer(db_session, consent=True)
    rep = new_repair(db_session, cust, status="COLLECTED")
    M.on_repair_collected(db_session, rep, now=NOW); db_session.commit()
    cust.marketing_consent = False; db_session.commit()                         # customer withdrew consent in between
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    r = rows(db_session, kind="review")[0]
    assert s.calls == [] and r.status == "skipped" and "consent" in r.skip_reason.lower()


def test_review_request_end_to_end(db_session):
    enable(db_session, review_enabled=True, review_link=GOOD_LINK, review_delay_days=3)
    cust = new_customer(db_session, consent=True, name="Sam Rivera")
    rep = new_repair(db_session, cust, status="COLLECTED")
    M.on_repair_collected(db_session, rep, now=NOW); db_session.commit()
    s = Sender()
    assert M.process_due(db_session, now=NOW + timedelta(days=1), send=s)["sent"] == 0      # waits the 3 days
    assert M.process_due(db_session, now=NOW + timedelta(days=3, minutes=5), send=s)["sent"] == 1
    body = s.calls[0][1]
    assert "Sam" in body and GOOD_LINK in body and body.endswith(M.OPT_OUT_LINE)


def test_review_skipped_if_the_ticket_is_not_collected(db_session):
    enable(db_session, review_enabled=True, review_link=GOOD_LINK, review_delay_days=0)
    cust = new_customer(db_session, consent=True)
    rep = new_repair(db_session, cust, status="COLLECTED")
    M.on_repair_collected(db_session, rep, now=NOW); db_session.commit()
    rep.status = "READY"; db_session.commit()                                   # reopened
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert s.calls == [] and rows(db_session, kind="review")[0].status == "skipped"


def test_review_skipped_if_link_removed_after_queueing(db_session):
    enable(db_session, review_enabled=True, review_link=GOOD_LINK, review_delay_days=0)
    cust = new_customer(db_session, consent=True)
    rep = new_repair(db_session, cust, status="COLLECTED")
    M.on_repair_collected(db_session, rep, now=NOW); db_session.commit()
    setcfg(db_session, review_link="")
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert s.calls == [] and "review link" in rows(db_session, kind="review")[0].skip_reason.lower()


def test_custom_template_is_used_and_cannot_inject(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    setcfg(db_session, tpl_ready="Yo {name}: {device} #{ticket} {__class__} {shop_phone}")
    cust, rep = ready_repair(db_session, name="Pat\nMorgan")
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    body = s.calls[0][1]
    assert body.startswith("Yo Pat:") and "__class__" not in body and "\n" not in body


def test_overlong_rendered_text_is_skipped_not_sent(db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    setcfg(db_session, tpl_ready="{name} " * 300)
    ready_repair(db_session)
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s)
    assert s.calls == [] and "too long" in rows(db_session)[0].skip_reason.lower()


def test_duplicate_dedupe_key_is_refused_by_the_database(db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session)
    assert M._enqueue(db_session, "ready", f"ready:{rep.id}", NOW, repair=rep) is None
    # even if the pre-check were bypassed, the UNIQUE key stops it: simulate a racing worker
    from sqlalchemy.exc import IntegrityError
    dup = OutboundMessage(kind="ready", dedupe_key=f"ready:{rep.id}", status="pending", due_at=NOW)
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    assert len(rows(db_session, kind="ready")) == 1


def test_enqueue_does_not_lose_the_callers_pending_changes(db_session):
    """_enqueue flushes the caller's work first and uses a savepoint, so a duplicate key never rolls back a status change."""
    enable(db_session)
    cust, rep = ready_repair(db_session)
    rep.device = "Changed device name"
    assert M._enqueue(db_session, "ready", f"ready:{rep.id}", NOW, repair=rep) is None
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Repair, rep.id).device == "Changed device name"


# ═══════════════════════════════════════════════════════════════════════
# 5. Win-back and catch-up
# ═══════════════════════════════════════════════════════════════════════

def add_invoice(db, cust, when):
    db.add(Invoice(id=f"msg-i-{uuid.uuid4().hex[:10]}", number=f"MSG-{uuid.uuid4().hex[:8]}", customer_id=cust.id, date=when, total=10, subtotal=10))
    db.commit()


def lapsed(db, **kw):
    """A customer who bought 200 days ago."""
    c = new_customer(db, consent=kw.pop("consent", True), spent=kw.pop("spent", 1e9), **kw)
    add_invoice(db, c, datetime.utcnow() - timedelta(days=200))
    return c


def queued_customers(db):
    return {r.customer_id for r in rows(db, kind="winback")}


def test_winback_off_unless_switched_on(db_session):
    c = lapsed(db_session)
    assert M.enqueue_winback(db_session) == 0
    enable(db_session)                                   # master switch alone is not enough
    assert M.enqueue_winback(db_session) == 0
    assert rows(db_session, kind="winback") == []


def test_winback_picks_only_the_right_people(db_session):
    enable(db_session, winback_enabled=True, winback_per_day=100)
    good = lapsed(db_session)
    no_consent = lapsed(db_session, consent=False)
    opted_out = lapsed(db_session, optout=True)
    bad_phone = lapsed(db_session, phone="N/A")
    never_spent = lapsed(db_session, spent=0)
    recent = lapsed(db_session)
    add_invoice(db_session, recent, datetime.utcnow() - timedelta(days=10))
    busy = lapsed(db_session)
    new_repair(db_session, busy, status="IN_PROGRESS", now=datetime.utcnow())
    M.enqueue_winback(db_session)
    q = queued_customers(db_session)
    assert good.id in q
    for c in (no_consent, opted_out, bad_phone, never_spent, recent, busy):
        assert c.id not in q, c.name
    assert all(r.source == "campaign" for r in rows(db_session, kind="winback"))


def test_winback_daily_limit_best_spenders_first(db_session):
    enable(db_session, winback_enabled=True, winback_per_day=1)
    small = lapsed(db_session, spent=1e9)
    big = lapsed(db_session, spent=2e9)
    assert M.enqueue_winback(db_session) == 1
    assert queued_customers(db_session) == {big.id}
    assert M.enqueue_winback(db_session) == 0                                   # today's room is used up
    assert queued_customers(db_session) == {big.id}


def test_winback_cooldown(db_session):
    enable(db_session, winback_enabled=True, winback_per_day=100, winback_cooldown_days=180)
    c = lapsed(db_session)
    now = datetime.utcnow()
    M.enqueue_winback(db_session, now=now)
    assert c.id in queued_customers(db_session)
    M.enqueue_winback(db_session, now=now + timedelta(days=30))
    assert len([r for r in rows(db_session, kind="winback") if r.customer_id == c.id]) == 1      # still cooling down
    M.enqueue_winback(db_session, now=now + timedelta(days=181))
    assert len([r for r in rows(db_session, kind="winback") if r.customer_id == c.id]) == 2


def test_winback_sends_with_unsubscribe_line_and_rechecks_visits(db_session):
    enable(db_session, winback_enabled=True, winback_per_day=100)
    stays, comes_back = lapsed(db_session, name="Stay Away"), lapsed(db_session, name="Came Back")
    M.enqueue_winback(db_session)
    add_invoice(db_session, comes_back, datetime.utcnow() - timedelta(days=1))   # visited after it was queued
    s = Sender()
    M.process_due(db_session, now=datetime.utcnow() + timedelta(minutes=1), send=s)
    sent = [r for r in rows(db_session, kind="winback") if r.status == "sent"]
    skipped = [r for r in rows(db_session, kind="winback") if r.customer_id == comes_back.id]
    assert any(r.customer_id == stays.id for r in sent)
    assert skipped[0].status == "skipped" and "since" in skipped[0].skip_reason.lower()
    stay_body = [b for _, b in s.calls if "Stay" in b][0]
    assert stay_body.endswith(M.OPT_OUT_LINE)


def test_catchup_finds_phones_already_waiting(db_session):
    enable(db_session)
    waiting = new_customer(db_session)
    rep = new_repair(db_session, waiting, status="READY", ready_ago=timedelta(days=3))
    fresh = new_repair(db_session, new_customer(db_session), status="READY", ready_ago=timedelta(hours=2))
    stopped = new_repair(db_session, new_customer(db_session, optout=True), status="READY", ready_ago=timedelta(days=3))
    nophone = new_repair(db_session, new_customer(db_session, phone=""), status="READY", ready_ago=timedelta(days=3))
    texted = new_repair(db_session, new_customer(db_session), status="READY", ready_ago=timedelta(days=3))
    raw_row(db_session, status="sent", kind="ready", repair_id=texted.id, sent_at=NOW - timedelta(hours=5))
    ids = {r.id for r in M.catchup_candidates(db_session, now=NOW)}
    assert rep.id in ids
    assert not ({fresh.id, stopped.id, nophone.id, texted.id} & ids)


def test_catchup_queues_one_reminder_per_ticket_ever(db_session):
    enable(db_session)
    c = new_customer(db_session)
    rep = new_repair(db_session, c, status="READY", ready_ago=timedelta(days=3))
    assert M.enqueue_catchup(db_session, now=NOW) >= 1
    assert len([r for r in rows(db_session, kind="reminder") if r.repair_id == rep.id]) == 1
    n_before = len(rows(db_session))
    M.enqueue_catchup(db_session, now=NOW + timedelta(hours=1))
    assert len(rows(db_session)) == n_before                                    # pressing the button twice is harmless
    s = Sender()
    M.process_due(db_session, now=NOW + timedelta(minutes=1), send=s, limit=500)
    mine = [b for t, b in s.calls if t == M.normalize_phone(c.phone)]
    assert len(mine) == 1 and "for 3 days" in mine[0] and mine[0].endswith(M.OPT_OUT_LINE)


def test_catchup_needs_messaging_switched_on(db_session):
    c = new_customer(db_session)
    new_repair(db_session, c, status="READY", ready_ago=timedelta(days=3))
    assert M.enqueue_catchup(db_session, now=NOW) == 0


# ═══════════════════════════════════════════════════════════════════════
# 6. Inbound replies: STOP / START
# ═══════════════════════════════════════════════════════════════════════

def test_stop_reply_matches_a_formatted_number_and_cancels_everything(db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session, phone="(250) 555-4301")
    assert len(rows(db_session, status="pending")) == 4
    assert M.handle_inbound(db_session, "+12505554301", "STOP", now=NOW) == "stop"
    db_session.expire_all()
    c = db_session.get(Customer, cust.id)
    assert c.sms_opt_out is True and c.sms_opt_out_at == NOW
    assert {r.status for r in rows(db_session)} == {"cancelled"} and rows(db_session)[0].skip_reason == "Customer replied STOP"
    logged = db_session.query(SmsMessage).filter(SmsMessage.customer_id == cust.id, SmsMessage.direction == "in").one()
    assert logged.body == "STOP"
    assert db_session.query(AuditLog).filter(AuditLog.action == "SMS_OPT_OUT", AuditLog.detail.contains(cust.name)).count() >= 1


@pytest.mark.parametrize("body", ["stop", "STOP.", "Unsubscribe", "cancel", "Stop texting me", " quit "])
def test_stop_variants(db_session, body):
    cust = new_customer(db_session, phone="(250) 555-4302")
    assert M.handle_inbound(db_session, "+12505554302", body) == "stop"
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is True


def test_start_reply_allows_texts_again(db_session):
    cust = new_customer(db_session, phone="(250) 555-4303", optout=True)
    assert M.handle_inbound(db_session, "+12505554303", "START") == "start"
    db_session.expire_all()
    c = db_session.get(Customer, cust.id)
    assert c.sms_opt_out is False and c.sms_opt_out_at is None


def test_ordinary_reply_changes_nothing_but_is_logged(db_session):
    cust = new_customer(db_session, phone="(250) 555-4304")
    assert M.handle_inbound(db_session, "+12505554304", "Is it ready? I can come at 5") is None
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is False
    assert db_session.query(SmsMessage).filter(SmsMessage.customer_id == cust.id).count() == 1


def test_stop_reaches_every_customer_record_on_the_same_number(db_session):
    a = new_customer(db_session, name="Dup A", phone="250-555-4305")
    b = new_customer(db_session, name="Dup B", phone="(250) 555-4305")
    other = new_customer(db_session, name="Somebody Else", phone="(250) 555-4306")
    M.handle_inbound(db_session, "+12505554305", "STOP")
    db_session.expire_all()
    assert db_session.get(Customer, a.id).sms_opt_out and db_session.get(Customer, b.id).sms_opt_out
    assert not db_session.get(Customer, other.id).sms_opt_out


def test_stop_from_an_unknown_number_is_logged_and_harmless(db_session):
    assert M.handle_inbound(db_session, "+12505554399", "STOP") == "stop"
    assert db_session.query(SmsMessage).filter(SmsMessage.phone == "+12505554399", SmsMessage.customer_id.is_(None)).count() == 1


def test_stop_matches_a_customer_stored_in_a_non_standard_format(db_session):
    """Existing customers/tests store odd numbers; an exact text match still finds them."""
    cust = new_customer(db_session, phone="+15551234567")                        # exchange 123 is not a valid NANP number
    M.handle_inbound(db_session, "+15551234567", "STOP")
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is True


def test_webhook_route_applies_stop_and_start(anon_client, db_session):
    from app.main import get_sms_webhook_secret
    secret = get_sms_webhook_secret(db_session)
    cust = new_customer(db_session, phone="(250) 555-4311")
    r = anon_client.post(f"/webhooks/sms-reply/{secret}", data={"From": "+12505554311", "Body": "STOP"})
    assert r.status_code == 200 and "<Response" in r.text
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is True
    anon_client.post(f"/webhooks/sms-reply/{secret}", data={"From": "+12505554311", "Body": "start"})
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is False


def test_webhook_rejects_wrong_secret_and_tolerates_empty_posts(anon_client, db_session):
    from app.main import get_sms_webhook_secret
    cust = new_customer(db_session, phone="(250) 555-4312")
    assert anon_client.post("/webhooks/sms-reply/not-the-secret", data={"From": "+12505554312", "Body": "STOP"}).status_code == 403
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is False
    secret = get_sms_webhook_secret(db_session)
    assert anon_client.post(f"/webhooks/sms-reply/{secret}", data={}).status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# 7. Manual buttons respect STOP, and the workflow hooks
# ═══════════════════════════════════════════════════════════════════════

def test_manual_ready_button_refuses_opted_out_customer(owner_client, db_session):
    cust = new_customer(db_session, optout=True)
    rep = new_repair(db_session, cust, status="READY")
    with patch("app.main.send_sms", return_value=(True, "ok")) as m:
        r = owner_client.post(f"/repairs/{rep.id}/notify", follow_redirects=False)
    assert r.status_code == 303 and m.call_count == 0
    assert db_session.query(OutboundMessage).filter(OutboundMessage.repair_id == rep.id).count() == 0


def test_manual_ready_button_records_the_send_for_dedupe(owner_client, db_session):
    enable(db_session, ready_delay_min=0)
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="READY", ready_ago=timedelta(0), now=datetime.utcnow())
    M.on_repair_ready(db_session, rep); db_session.commit()
    with patch("app.main.send_sms", return_value=(True, "SMS sent successfully.")) as m:
        owner_client.post(f"/repairs/{rep.id}/notify", follow_redirects=False)
    assert m.call_count == 1
    db_session.expire_all()
    ready = [r for r in rows(db_session, repair_id=rep.id) if r.kind == "ready"]
    assert {(r.source, r.status) for r in ready} == {("manual", "sent"), ("auto", "cancelled")}
    s = Sender()
    M.process_due(db_session, now=datetime.utcnow() + timedelta(minutes=1), send=s)
    assert s.calls == []


def test_manual_ready_button_failure_records_nothing(owner_client, db_session):
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="READY")
    with patch("app.main.send_sms", return_value=(False, "SMS is not configured")):
        owner_client.post(f"/repairs/{rep.id}/notify", follow_redirects=False)
    assert db_session.query(OutboundMessage).filter(OutboundMessage.repair_id == rep.id).count() == 0


def test_customer_sms_button_refuses_opted_out_customer(owner_client, db_session):
    stopped, fine = new_customer(db_session, optout=True), new_customer(db_session)
    with patch("app.main.send_sms", return_value=(True, "ok")) as m:
        owner_client.post(f"/customers/{stopped.id}/sms", data={"message": "hello"}, follow_redirects=False)
        assert m.call_count == 0
        owner_client.post(f"/customers/{fine.id}/sms", data={"message": "hello"}, follow_redirects=False)
        assert m.call_count == 1


def test_receipt_sms_refuses_opted_out_customer(owner_client, db_session):
    stopped = new_customer(db_session, optout=True)
    inv = Invoice(id=f"msg-i-{uuid.uuid4().hex[:8]}", number=f"MSG-{uuid.uuid4().hex[:8]}", customer_id=stopped.id, total=10, subtotal=10)
    db_session.add(inv); db_session.commit()
    with patch("app.main.send_sms", return_value=(True, "ok")) as m:
        owner_client.post(f"/invoices/{inv.id}/sms", data={"to_phone": ""}, follow_redirects=False)
        owner_client.post(f"/invoices/{inv.id}/sms", data={"to_phone": stopped.phone}, follow_redirects=False)
        assert m.call_count == 0
        owner_client.post(f"/invoices/{inv.id}/sms", data={"to_phone": "(250) 555-4999"}, follow_redirects=False)   # a different number is fine
        assert m.call_count == 1


def test_advancing_to_ready_queues_texts_and_collecting_cancels_them(owner_client, db_session):
    enable(db_session)
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="IN_PROGRESS", now=datetime.utcnow())
    assert owner_client.post(f"/repairs/{rep.id}/advance", data={"note": ""}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(Repair, rep.id).status == "READY"
    assert [r.kind for r in rows(db_session, repair_id=rep.id)] == ["ready", "reminder", "reminder", "reminder"]
    owner_client.post(f"/repairs/{rep.id}/advance", data={"note": ""}, follow_redirects=False)     # COMPLETED
    assert {r.status for r in rows(db_session, repair_id=rep.id)} == {"pending"}
    owner_client.post(f"/repairs/{rep.id}/advance", data={"note": ""}, follow_redirects=False)     # COLLECTED
    assert {r.status for r in rows(db_session, repair_id=rep.id)} == {"cancelled"}


def test_advancing_with_messaging_off_queues_nothing(owner_client, db_session):
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="IN_PROGRESS", now=datetime.utcnow())
    owner_client.post(f"/repairs/{rep.id}/advance", data={"note": ""}, follow_redirects=False)
    assert rows(db_session, repair_id=rep.id) == []


def test_a_messaging_bug_can_never_block_the_workshop(owner_client, db_session):
    enable(db_session)
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="IN_PROGRESS", now=datetime.utcnow())
    with patch("app.messaging.on_repair_ready", side_effect=RuntimeError("queue exploded")):
        r = owner_client.post(f"/repairs/{rep.id}/advance", data={"note": ""}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Repair, rep.id).status == "READY"
    assert owner_client.get(f"/repairs/{rep.id}").status_code == 200            # and the session is still healthy afterwards


def test_collect_hook_helper_cancels_pending_texts(owner_client, db_session):
    """The POS path marks the ticket Collected without the advance route and calls this same helper."""
    enable(db_session)
    cust, rep = ready_repair(db_session, now=datetime.utcnow())
    import app.main as main
    main._messaging_hook(M.on_repair_collected, db_session, rep)
    assert {r.status for r in rows(db_session, repair_id=rep.id)} == {"cancelled"}


# ═══════════════════════════════════════════════════════════════════════
# 8. send_sms_detailed (the Twilio call itself, with HTTP faked)
# ═══════════════════════════════════════════════════════════════════════

class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text or json.dumps(payload or {})

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def test_twilio_success_sends_e164_and_returns_sid(db_session, twilio_on):
    from app.notifications import send_sms_detailed
    with patch("app.notifications.httpx.post", return_value=FakeResp(201, {"sid": "SM123"})) as post:
        r = send_sms_detailed(db_session, "(250) 555-4401", "hello", M.get_setting)
    assert r["ok"] and r["sid"] == "SM123" and r["to"] == "+12505554401" and r["configured"]
    assert post.call_args.kwargs["data"]["To"] == "+12505554401" and post.call_args.kwargs["data"]["Body"] == "hello"


@pytest.mark.parametrize("status,payload,retryable,code", [
    (429, {"code": 20429}, True, "20429"), (500, {}, True, "500"), (503, None, True, "503"),
    (400, {"code": 21211}, False, "21211"), (400, {"code": 21610}, False, "21610"), (401, {"code": 20003}, False, "20003"),
])
def test_twilio_error_classification(db_session, twilio_on, status, payload, retryable, code):
    from app.notifications import send_sms_detailed
    with patch("app.notifications.httpx.post", return_value=FakeResp(status, payload)):
        r = send_sms_detailed(db_session, "(250) 555-4402", "hello", M.get_setting)
    assert not r["ok"] and r["retryable"] is retryable and r["error_code"] == code


def test_twilio_network_failure_is_retryable(db_session, twilio_on):
    from app.notifications import send_sms_detailed
    with patch("app.notifications.httpx.post", side_effect=OSError("dns down")):
        r = send_sms_detailed(db_session, "(250) 555-4403", "hello", M.get_setting)
    assert not r["ok"] and r["retryable"] and r["error_code"] == "network"


def test_twilio_bad_number_never_calls_the_network(db_session, twilio_on):
    from app.notifications import send_sms_detailed
    with patch("app.notifications.httpx.post") as post:
        r = send_sms_detailed(db_session, "call me maybe", "hello", M.get_setting)
    assert not r["ok"] and r["error_code"] == "bad_number" and not r["retryable"] and post.call_count == 0


def test_twilio_not_configured(db_session, twilio_off):
    from app.notifications import send_sms_detailed, send_sms
    r = send_sms_detailed(db_session, "(250) 555-4404", "hello", M.get_setting)
    assert not r["ok"] and r["configured"] is False
    ok, msg = send_sms(db_session, "(250) 555-4404", "hello", M.get_setting)       # the old interface still works
    assert ok is False and "not configured" in msg.lower()


def test_old_send_sms_interface_still_returns_a_pair(db_session, twilio_on):
    from app.notifications import send_sms
    with patch("app.notifications.httpx.post", return_value=FakeResp(201, {"sid": "SMabc"})):
        assert send_sms(db_session, "250-555-4405", "hi", M.get_setting) == (True, "SMS sent successfully.")


# ═══════════════════════════════════════════════════════════════════════
# 9. Pages and routes
# ═══════════════════════════════════════════════════════════════════════

def seed_history(db):
    cust = new_customer(db, name="History Person")
    rep = new_repair(db, cust, status="READY", now=datetime.utcnow())
    now = datetime.utcnow()
    raw_row(db, "sent", "ready", repair_id=rep.id, customer_id=cust.id, sent_at=now, body="Your phone is ready", to_phone="+12505554500")
    raw_row(db, "pending", "reminder", seq=1, repair_id=rep.id, customer_id=cust.id, due_at=now + timedelta(days=3))
    raw_row(db, "failed", "reminder", seq=2, repair_id=rep.id, customer_id=cust.id, error_code="21211", last_error="Invalid 'To' phone number")
    raw_row(db, "skipped", "winback", customer_id=cust.id, skip_reason="No consent for promotional texts (CASL)")
    raw_row(db, "cancelled", "review", repair_id=rep.id, customer_id=cust.id, skip_reason="Device collected")
    return cust, rep


@pytest.mark.parametrize("url", ["/messaging", "/messaging?tab=history", "/messaging?tab=problems", "/messaging?tab=nonsense",
                                 "/messaging?kind=reminder", "/messaging?tab=problems&kind=winback", "/messaging?kind=zzz"])
def test_messaging_pages_render_for_owner_and_manager(owner_client, manager_client, db_session, url):
    seed_history(db_session)
    for c in (owner_client, manager_client):
        r = c.get(url)
        assert r.status_code == 200 and "Automatic texts" in r.text


def test_messaging_page_shows_rows_and_actions(owner_client, db_session):
    seed_history(db_session)
    assert "History Person" in owner_client.get("/messaging").text
    assert "History Person" in owner_client.get("/messaging?tab=history").text
    problems = owner_client.get("/messaging?tab=problems").text
    assert "Invalid" in problems and "No consent" in problems and "retry" in problems.lower()


def test_messaging_page_banners(owner_client, db_session, twilio_off):
    assert "off" in owner_client.get("/messaging").text.lower()
    enable(db_session)
    assert "twilio" in owner_client.get("/messaging").text.lower()                # not set up yet
    new_repair(db_session, new_customer(db_session), status="READY", ready_ago=timedelta(days=3), now=datetime.utcnow())
    page = owner_client.get("/messaging").text.lower()
    assert "been waiting for pickup" in page and "send one reminder to each" in page


@pytest.mark.parametrize("method,url", [("get", "/messaging"), ("get", "/messaging/settings"), ("post", "/messaging/run-now"),
                                        ("post", "/messaging/catchup"), ("post", "/messaging/abc/cancel"), ("post", "/messaging/abc/retry"),
                                        ("post", "/messaging/settings"), ("post", "/messaging/settings/test"), ("post", "/customers/abc/sms-optout")])
def test_messaging_routes_are_login_protected(anon_client, cashier_client, technician_client, method, url):
    r = getattr(anon_client, method)(url, follow_redirects=False)
    assert r.status_code in (303, 307) and "/login" in r.headers["location"]
    for c in (cashier_client, technician_client):
        assert getattr(c, method)(url, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("method,url", [("get", "/messaging/settings"), ("post", "/messaging/run-now"), ("post", "/messaging/catchup"),
                                        ("post", "/messaging/settings"), ("post", "/messaging/settings/test")])
def test_owner_only_routes_refuse_the_manager(manager_client, method, url):
    assert getattr(manager_client, method)(url, follow_redirects=False).status_code == 403


def test_settings_page_renders_with_live_previews(owner_client):
    r = owner_client.get("/messaging/settings")
    assert r.status_code == 200
    for needle in ("Ready for pickup", "Reply STOP", "msg_daily_cap", "msg_review_link", "text part"):
        assert needle.lower() in r.text.lower(), needle
    assert r.text.count("<form") >= 5                                             # main form + the four test-send forms


def test_settings_save_route(owner_client, db_session):
    r = owner_client.post("/messaging/settings", data=good_form(msg_daily_cap="77"), follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    cfg = M.load_config(db_session)
    assert cfg.enabled and cfg.daily_cap == 77
    assert db_session.query(AuditLog).filter(AuditLog.action == "SETTINGS", AuditLog.detail.contains("switched ON")).count() >= 1
    f = good_form(msg_daily_cap="77"); f.pop("msg_enabled"); f.pop("msg_review_enabled"); f["msg_review_link"] = ""
    owner_client.post("/messaging/settings", data=f, follow_redirects=False)
    db_session.expire_all()
    assert M.load_config(db_session).enabled is False
    assert db_session.query(AuditLog).filter(AuditLog.action == "SETTINGS", AuditLog.detail.contains("switched OFF")).count() >= 1


def test_settings_save_route_shows_errors_and_keeps_what_was_typed(owner_client, db_session):
    M.save_settings(db_session, good_form(msg_daily_cap="55"))
    r = owner_client.post("/messaging/settings", data=good_form(msg_daily_cap="abc", msg_shop_phone="250-555-7777",
                                                                msg_tpl_ready="Typed {nmae} wording"), follow_redirects=False)
    assert r.status_code == 400
    assert "Daily text limit" in r.text and "unknown placeholder" in r.text.lower()
    assert "250-555-7777" in r.text and "Typed" in r.text                        # the form is not wiped
    db_session.expire_all()
    assert M.load_config(db_session).daily_cap == 55


def test_cancel_route(owner_client, manager_client, db_session):
    p = raw_row(db_session, "pending", due_at=datetime.utcnow() + timedelta(days=1))
    sent = raw_row(db_session, "sent")
    manager_client.post(f"/messaging/{p.id}/cancel", follow_redirects=False)
    owner_client.post(f"/messaging/{sent.id}/cancel", follow_redirects=False)
    owner_client.post("/messaging/does-not-exist/cancel", follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(OutboundMessage, p.id).status == "cancelled" and "Cancelled by" in db_session.get(OutboundMessage, p.id).skip_reason
    assert db_session.get(OutboundMessage, sent.id).status == "sent"


def test_retry_route(owner_client, db_session):
    failed = raw_row(db_session, "failed", attempts=3, error_code="500", last_error="boom")
    manual_failed = raw_row(db_session, "failed", source="manual")
    sent = raw_row(db_session, "sent")
    for r in (failed, manual_failed, sent):
        owner_client.post(f"/messaging/{r.id}/retry", follow_redirects=False)
    db_session.expire_all()
    f = db_session.get(OutboundMessage, failed.id)
    assert (f.status, f.attempts, f.error_code, f.last_error) == ("pending", 0, "", "") and f.due_at <= datetime.utcnow() + timedelta(seconds=5)
    assert db_session.get(OutboundMessage, manual_failed.id).status == "failed" and db_session.get(OutboundMessage, sent.id).status == "sent"


def test_run_now_route_reports_setup_problem(owner_client, db_session, twilio_off):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session, now=datetime.utcnow() - timedelta(minutes=1))
    page = owner_client.post("/messaging/run-now").text                          # follows the redirect
    assert "Stopped" in page and "configured" in page.lower()
    assert rows(db_session)[0].status == "pending"


def test_run_now_route_sends_when_everything_is_set(owner_client, db_session):
    enable(db_session, ready_delay_min=0, remind_enabled=False)
    ready_repair(db_session, now=datetime.utcnow() - timedelta(minutes=1))
    s = Sender()
    with patch.object(M, "_default_send", s):
        page = owner_client.post("/messaging/run-now").text
    assert "1 sent" in page and len(s.calls) == 1


def test_run_now_when_off_says_so(owner_client):
    assert "switched off" in owner_client.post("/messaging/run-now").text.lower()


def test_catchup_route(owner_client, db_session):
    enable(db_session)
    c = new_customer(db_session)
    rep = new_repair(db_session, c, status="READY", ready_ago=timedelta(days=3), now=datetime.utcnow())
    page = owner_client.post("/messaging/catchup").text
    assert "Queued" in page
    assert [r.kind for r in rows(db_session, repair_id=rep.id)] == ["reminder"]
    assert db_session.query(AuditLog).filter(AuditLog.action == "SMS_CATCHUP").count() >= 1


def test_settings_test_send_route(owner_client, db_session):
    with patch("app.notifications.send_sms_detailed", return_value={"ok": True, "message": "SMS sent successfully.", "to": "+12505554600"}) as m:
        page = owner_client.post("/messaging/settings/test", data={"kind": "reminder", "to_phone": "(250) 555-4600"}).text
        assert m.call_args.args[2].startswith("[TEST] ") and "Reply STOP" in m.call_args.args[2] and m.call_args.args[1] == "(250) 555-4600"
        assert "Test text sent" in page
        page = owner_client.post("/messaging/settings/test", data={"kind": "nonsense", "to_phone": "250-555-4600"}).text
        assert "Unknown message type" in page and m.call_count == 1
    with patch("app.notifications.send_sms_detailed", return_value={"ok": False, "message": "SMS is not configured", "to": ""}):
        assert "not configured" in owner_client.post("/messaging/settings/test", data={"kind": "ready", "to_phone": "250-555-4600"}).text


def test_customer_opt_out_toggle(owner_client, manager_client, cashier_client, db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session)
    assert cashier_client.post(f"/customers/{cust.id}/sms-optout", data={"opt_out": "1"}, follow_redirects=False).status_code == 403
    page = manager_client.post(f"/customers/{cust.id}/sms-optout", data={"opt_out": "1"}).text
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is True and db_session.get(Customer, cust.id).sms_opt_out_at
    assert {r.status for r in rows(db_session)} == {"cancelled"}
    assert "do not text" in page.lower() or "do-not-text" in page.lower() or "opted out" in page.lower()
    seen = cashier_client.get(f"/customers/{cust.id}").text.lower()
    assert "do not text" in seen or "opted out" in seen
    owner_client.post(f"/customers/{cust.id}/sms-optout", data={"opt_out": "0"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Customer, cust.id).sms_opt_out is False and db_session.get(Customer, cust.id).sms_opt_out_at is None
    assert owner_client.post("/customers/nope/sms-optout", data={"opt_out": "1"}, follow_redirects=False).status_code == 303


def test_repair_and_customer_pages_show_the_new_panels(owner_client, db_session):
    enable(db_session)
    cust, rep = ready_repair(db_session, now=datetime.utcnow())
    page = owner_client.get(f"/repairs/{rep.id}").text
    assert "Automatic texts" in page and "eminder" in page
    stopped = new_customer(db_session, optout=True)
    stopped_rep = new_repair(db_session, stopped, status="READY")
    assert "STOP" in owner_client.get(f"/repairs/{stopped_rep.id}").text
    assert owner_client.get(f"/customers/{stopped.id}").status_code == 200
    assert owner_client.get(f"/customers/{cust.id}").status_code == 200


def test_repair_page_without_messaging_still_renders(owner_client, db_session):
    cust = new_customer(db_session)
    rep = new_repair(db_session, cust, status="READY")
    assert owner_client.get(f"/repairs/{rep.id}").status_code == 200


def test_navigation_and_settings_links(owner_client):
    assert 'href="/messaging"' in owner_client.get("/messaging").text
    assert "/messaging/settings" in owner_client.get("/settings").text


# ═══════════════════════════════════════════════════════════════════════
# 10. Reports, digest, backup, scheduler, upgrade
# ═══════════════════════════════════════════════════════════════════════

def test_customer_messaging_report(owner_client, manager_client, cashier_client, db_session):
    now = datetime.utcnow()
    cust = new_customer(db_session, name="Report Person")
    db_session.query(Customer).filter(Customer.id != cust.id).update({"sms_opt_out_at": None}, synchronize_session=False)   # other tests' STOP replies
    db_session.commit()
    rep = new_repair(db_session, cust, status="COLLECTED", now=now)
    raw_row(db_session, "sent", "ready", repair_id=rep.id, customer_id=cust.id, sent_at=now)
    raw_row(db_session, "sent", "reminder", seq=1, repair_id=rep.id, customer_id=cust.id, sent_at=now)
    raw_row(db_session, "sent", "ready", source="manual", repair_id=rep.id, customer_id=cust.id, sent_at=now)
    raw_row(db_session, "failed", "reminder", repair_id=rep.id, customer_id=cust.id, error_code="21211", last_error="Invalid 'To' phone number", attempts=1)
    raw_row(db_session, "skipped", "ready", skip_reason="Device already collected")
    raw_row(db_session, "cancelled", "reminder", skip_reason="Device collected")
    raw_row(db_session, "pending", "reminder", due_at=now + timedelta(days=2))
    cust.sms_opt_out, cust.sms_opt_out_at = True, now
    db_session.commit()

    rep_obj, p, tz = R.build_report(db_session, "messaging", {"preset": "today"})
    k = {x.label: x.value for x in rep_obj.kpis}
    assert k["Automatic texts sent"] == 2 and k["Failed"] == 1 and k["Skipped"] == 1 and k["Cancelled"] == 1
    assert k["Waiting to send"] == 1 and k["New do-not-text"] == 1 and k["Collected after a reminder"] == 1
    kind = {r[0]: r for r in rep_obj.sections[0].rows}
    assert kind["Ready for pickup"][1] == 1 and kind["Pickup reminder"][1] == 1 and kind["Pickup reminder"][2] == 1
    assert any("Invalid" in str(r) for s in rep_obj.sections for r in s.rows)
    assert any("Report Person" in str(r) for r in rep_obj.sections[-1].rows)

    assert any(r["key"] == "messaging" for r in R.REPORTS)
    assert "Customer Messaging" in owner_client.get("/reports/center").text
    for url in ("/reports/center/messaging?preset=today", "/reports/center/messaging/print?preset=today",
                "/reports/center/messaging?from=2020-01-01&to=2020-01-31"):
        for c in (owner_client, manager_client):
            assert c.get(url).status_code == 200, url
    assert cashier_client.get("/reports/center/messaging").status_code == 403
    csv_ = owner_client.get("/reports/center/messaging/export.csv?preset=today")
    assert csv_.status_code == 200 and "Automatic texts sent" in csv_.text
    wb = load_workbook(io.BytesIO(owner_client.get("/reports/center/messaging/export.xlsx?preset=today").content))
    assert len(wb.sheetnames) >= 3


def test_messaging_report_on_an_empty_period(db_session):
    rep, p, tz = R.build_report(db_session, "messaging", {"from": "2019-01-01", "to": "2019-01-31"})
    assert {x.label: x.value for x in rep.kpis}["Automatic texts sent"] == 0 and rep.notes


def test_daily_digest_mentions_automatic_texts(db_session):
    from app.main import build_daily_digest
    raw_row(db_session, "sent", "ready", sent_at=datetime.utcnow())
    raw_row(db_session, "failed", "reminder", error_code="500")
    subject, text = build_daily_digest(db_session)
    assert "Automatic customer texts today: 1 sent" in text and "1 failed" in text
    db_session.query(OutboundMessage).delete(); db_session.commit()
    assert "Automatic customer texts" not in build_daily_digest(db_session)[1]


def test_scheduler_jobs_do_their_work_and_never_raise(db_session):
    import app.main as main
    enable(db_session, ready_delay_min=0, remind_enabled=False, winback_enabled=True, winback_per_day=1)
    ready_repair(db_session, now=datetime.utcnow() - timedelta(minutes=2))
    lapsed(db_session)
    s = Sender()
    with patch.object(M, "_default_send", s):
        main.run_winback_enqueue_job()
        main.run_messaging_job()
    assert len(s.calls) == 2                                                    # the ready text + the win-back
    with patch.object(M, "process_due", side_effect=RuntimeError("db down")), patch.object(M, "enqueue_winback", side_effect=RuntimeError("db down")):
        main.run_messaging_job()                                                # must swallow the error
        main.run_winback_enqueue_job()


def test_background_timer_is_not_started_under_pytest():
    import app.main as main
    assert "messaging_queue" not in {j.id for j in main._scheduler.get_jobs()}


def test_new_tables_and_columns_exist(db_session):
    insp = sa_inspect(engine)
    assert "outbound_messages" in insp.get_table_names()
    assert {"sms_opt_out", "sms_opt_out_at"} <= {c["name"] for c in insp.get_columns("customers")}
    assert {"dedupe_key", "status", "due_at", "claimed_at", "provider_sid", "skip_reason"} <= {c["name"] for c in insp.get_columns("outbound_messages")}


def test_backup_roundtrip_keeps_opt_out_and_consent_dates(owner_client, db_session):
    """Export then restore on the same database: nothing is lost, dates come back as dates, queued texts are cleared."""
    cust = new_customer(db_session, name="Roundtrip Person", consent=True, optout=True)
    cust.consent_date, cust.sms_opt_out_at = datetime(2031, 1, 2, 3, 4, 5), datetime(2031, 2, 3, 4, 5, 6)
    db_session.commit()
    raw_row(db_session, "pending", customer_id=cust.id)
    before = db_session.query(Customer).count(), db_session.query(Repair).count()
    blob = owner_client.get("/export/backup").content
    r = owner_client.post("/import/backup", files={"file": ("backup.json", blob, "application/json")}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    db_session.expire_all()
    back = db_session.get(Customer, cust.id)
    assert back.sms_opt_out is True and back.marketing_consent is True
    assert back.consent_date == datetime(2031, 1, 2, 3, 4, 5) and back.sms_opt_out_at == datetime(2031, 2, 3, 4, 5, 6)
    assert (db_session.query(Customer).count(), db_session.query(Repair).count()) == before
    # v48: a restore cancels whatever was waiting to be sent (it may describe repairs that no longer exist)
    assert db_session.query(OutboundMessage).filter(OutboundMessage.status.in_(("pending", "sending"))).count() == 0


def test_migration_adds_the_new_columns_to_an_older_database(tmp_path):
    """A v45 database has no sms_opt_out column and no outbound_messages table; opening it with v46 must add both and keep rows."""
    import sqlite3
    from sqlalchemy import create_engine, text
    from app import seed
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, phone VARCHAR, email VARCHAR, notes TEXT, points INTEGER,
                                store_credit FLOAT, spent FLOAT, last_visit VARCHAR, marketing_consent BOOLEAN, consent_date DATETIME);
        INSERT INTO customers (id, name, phone, marketing_consent) VALUES ('old1', 'Old Customer', '(250) 555-0101', 1);
    """)
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{path}")
    with patch.object(seed, "engine", eng):
        seed.Base.metadata.create_all(eng)
        seed.migrate_schema()
    insp = sa_inspect(eng)
    assert {"sms_opt_out", "sms_opt_out_at"} <= {c["name"] for c in insp.get_columns("customers")}
    assert "outbound_messages" in insp.get_table_names()
    with eng.connect() as conn:
        row = conn.execute(text("SELECT name, marketing_consent, sms_opt_out FROM customers WHERE id='old1'")).one()
    assert row[0] == "Old Customer" and row[1] == 1 and not row[2]                # the default is "not opted out"
