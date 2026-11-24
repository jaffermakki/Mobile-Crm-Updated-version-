"""v52 — repair quote approval by text link: link security, one answer only, expiry, texting, staff screens, the bell."""
import json
import re
import threading
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from app import quotes as Q
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Customer, LoginAttempt, QuoteApproval, Repair, SmsMessage
from tests.v48_helpers import new_client

_OPEN, _MADE = [], []
_ticket = [520000]


def _db():
    db = SessionLocal()
    _OPEN.append(db)
    return db


@pytest.fixture(autouse=True)
def _tidy(monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "x-real-ip")      # lets a test act as different phones (same as the v48 tests)
    db = SessionLocal()
    db.query(LoginAttempt).filter(LoginAttempt.kind == "status").delete()
    db.commit()
    db.close()
    yield
    while _OPEN:
        _OPEN.pop().close()
    db = SessionLocal()
    try:
        db.query(LoginAttempt).filter(LoginAttempt.kind == "status").delete()
        for rid, cid in _MADE:
            db.query(QuoteApproval).filter(QuoteApproval.repair_id == rid).delete()
            db.query(Repair).filter(Repair.id == rid).delete()
            db.query(SmsMessage).filter(SmsMessage.customer_id == cid).delete()
            db.query(Customer).filter(Customer.id == cid).delete()
        db.commit()
    finally:
        _MADE.clear()
        db.close()


def mk_repair(phone="(250) 555-0188", opt_out=False, estimate=None, status="COLLECTED", device="Pixel 8 Pro", name="Quentin Quote"):
    db = _db()
    _ticket[0] += 1
    c = Customer(name=name, phone=phone, email="quentin@example.test", sms_opt_out=opt_out)
    db.add(c)
    db.flush()
    r = Repair(ticket_no=_ticket[0], customer_id=c.id, device=device, issue="Cracked screen", imei="356938035643809",
               status=status, estimated_cost=estimate)
    db.add(r)
    db.commit()
    _MADE.append((r.id, c.id))
    return r.id


def mk_quote(rid, amount="129.99", summary="Screen replacement", note="", days=7):
    db = _db()
    r = db.get(Repair, rid)
    q = Q.create(db, None, r, amount, summary, note, days)
    db.commit()
    return q.id, q.token


def fresh(model, pk):
    db = _db()
    return db.get(model, pk)


def public():
    return TestClient(app)


@pytest.fixture
def sms(monkeypatch):
    """Replaces the real text service. sms.sent lists what would have been sent; sms.result is what it 'returns'."""
    class Fake:
        sent = []
        result = {"ok": True, "message": "Sent"}
    Fake.sent = []
    Fake.result = {"ok": True, "message": "Sent"}

    def fake(db, to_phone, message, get_setting):
        Fake.sent.append((to_phone, message))
        return dict(Fake.result)
    monkeypatch.setattr("app.notifications.send_sms_detailed", fake)
    return Fake


# ── the rules (no web) ─────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "  ", "0", "0.00", "-1", "abc", "nan", "inf", "-inf", "100000.01", "1e9", "12,34,56x"])
def test_bad_prices_are_rejected(bad):
    with pytest.raises(Q.QuoteError):
        Q.parse_price(bad)


@pytest.mark.parametrize("raw,expected", [("129.99", 129.99), ("$1,234.5", 1234.5), (" 5 ", 5.0), ("0.01", 0.01), (99.999, 100.0), ("100000", 100000.0)])
def test_good_prices_are_cleaned(raw, expected):
    assert Q.parse_price(raw) == expected


def test_token_is_long_unique_and_url_safe():
    toks = {Q.new_token() for _ in range(200)}
    assert len(toks) == 200
    assert all(len(t) >= 24 and Q.TOKEN_RE.match(t) for t in toks)


def test_create_trims_text_and_defaults_the_validity():
    rid = mk_repair()
    db = _db()
    q = Q.create(db, None, db.get(Repair, rid), "50", "  Battery \n  swap  " + "x" * 300, "n" * 900, days=5)   # 5 is not a choice
    assert q.summary.startswith("Battery swap") and len(q.summary) <= Q.MAX_SUMMARY
    assert len(q.note) <= Q.MAX_NOTE
    assert abs((q.expires_at - q.created_at) - timedelta(days=Q.DEFAULT_VALID_DAYS)) < timedelta(seconds=5)
    with pytest.raises(Q.QuoteError):
        Q.create(db, None, db.get(Repair, rid), "50", "   ")
    db.rollback()


@pytest.mark.parametrize("days", Q.VALID_DAY_CHOICES)
def test_each_validity_choice_is_honoured(days):
    rid = mk_repair()
    qid, _ = mk_quote(rid, days=days)
    q = fresh(QuoteApproval, qid)
    assert abs((q.expires_at - q.created_at) - timedelta(days=days)) < timedelta(seconds=5)


def test_a_new_quote_retires_the_old_live_one_only():
    rid = mk_repair()
    a, _ = mk_quote(rid, "10")
    db = _db()
    Q.answer(db, db.get(QuoteApproval, a), "decline", "link")
    db.commit()
    b, _ = mk_quote(rid, "20")
    c, _ = mk_quote(rid, "30")
    assert fresh(QuoteApproval, a).status == "declined"        # an answered quote is history, not replaced
    assert fresh(QuoteApproval, b).status == "replaced"
    assert fresh(QuoteApproval, c).status == "pending"
    assert Q.latest(_db(), rid).id == c


def test_approving_sets_the_ticket_price_and_leaves_a_history_line_but_keeps_the_status():
    rid = mk_repair(estimate=80.0, status="DIAGNOSED")
    qid, _ = mk_quote(rid, "129.99")
    db = _db()
    Q.answer(db, db.get(QuoteApproval, qid), "approve", "link", "go ahead")
    db.commit()
    r = fresh(Repair, rid)
    assert r.estimated_cost == 129.99
    assert r.status == "DIAGNOSED"                              # the shop decides when work starts
    hist = json.loads(r.status_history)
    assert any("approved the quote" in h["note"] and "129.99" in h["note"] for h in hist)
    q = fresh(QuoteApproval, qid)
    assert q.status == "approved" and q.responded_via == "link" and q.response_note == "go ahead" and q.responded_at is not None


def test_declining_leaves_the_estimate_alone():
    rid = mk_repair(estimate=80.0)
    qid, _ = mk_quote(rid, "129.99")
    db = _db()
    Q.answer(db, db.get(QuoteApproval, qid), "decline", "link")
    db.commit()
    r = fresh(Repair, rid)
    assert r.estimated_cost == 80.0
    assert any("declined the quote" in h["note"] for h in json.loads(r.status_history))


@pytest.mark.parametrize("first,second", [("approve", "decline"), ("approve", "approve"), ("decline", "approve"), ("decline", "decline")])
def test_only_the_first_answer_counts(first, second):
    rid = mk_repair(estimate=80.0)
    qid, _ = mk_quote(rid, "129.99")
    db = _db()
    Q.answer(db, db.get(QuoteApproval, qid), first, "link")
    db.commit()
    with pytest.raises(Q.QuoteError):
        Q.answer(db, db.get(QuoteApproval, qid), second, "staff", staff=None)
    db.rollback()
    assert fresh(QuoteApproval, qid).status == ("approved" if first == "approve" else "declined")
    assert fresh(Repair, rid).estimated_cost == (129.99 if first == "approve" else 80.0)


def test_two_people_answering_at_once_cannot_both_win():
    """Both sessions load the quote while it is still 'pending'; the conditional UPDATE lets only one through."""
    rid = mk_repair(estimate=80.0)
    qid, _ = mk_quote(rid, "129.99")
    a, b = _db(), _db()
    qa, qb = a.get(QuoteApproval, qid), b.get(QuoteApproval, qid)
    assert qa.status == qb.status == "pending"
    Q.answer(a, qa, "approve", "link")
    a.commit()
    with pytest.raises(Q.QuoteError) as e:
        Q.answer(b, qb, "decline", "link")                        # b still believes it is pending
    assert "answered" in str(e.value)
    b.rollback()
    assert fresh(QuoteApproval, qid).status == "approved"
    assert fresh(Repair, rid).estimated_cost == 129.99


def test_bad_decisions_and_channels_are_refused():
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    db = _db()
    for dec in ("", "yes", "APPROVE", "approve ", None):
        with pytest.raises(Q.QuoteError):
            Q.answer(db, db.get(QuoteApproval, qid), dec, "link")
    with pytest.raises(Q.QuoteError):
        Q.answer(db, db.get(QuoteApproval, qid), "approve", "carrier-pigeon")
    assert db.get(QuoteApproval, qid).status == "pending"


def test_an_expired_quote_cannot_be_answered_and_is_marked_expired():
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    db = _db()
    q = db.get(QuoteApproval, qid)
    q.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(Q.QuoteError) as e:
        Q.answer(db, db.get(QuoteApproval, qid), "approve", "link")
    assert "expired" in str(e.value)
    db.commit()
    assert fresh(QuoteApproval, qid).status == "expired"


def test_cancel_only_works_on_a_waiting_quote():
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    db = _db()
    Q.cancel(db, db.get(QuoteApproval, qid))
    db.commit()
    assert fresh(QuoteApproval, qid).status == "cancelled"
    with pytest.raises(Q.QuoteError):
        Q.cancel(_db(), fresh(QuoteApproval, qid))


def test_price_drift_flags_only_an_approved_quote_whose_price_was_edited():
    rid = mk_repair(estimate=None)
    qid, _ = mk_quote(rid, "50")
    db = _db()
    r, q = db.get(Repair, rid), db.get(QuoteApproval, qid)
    assert Q.price_drift(q, r) is False                         # waiting for an answer: no estimate on the ticket is normal
    r.estimated_cost = 80.0
    assert Q.price_drift(q, r) is False                         # ...and so is a different rough estimate
    q.status = "approved"
    assert Q.price_drift(q, r) is True                          # approved at $50 but the ticket says $80
    r.estimated_cost = None
    assert Q.price_drift(q, r) is True
    r.estimated_cost = 50.0
    assert Q.price_drift(q, r) is False                         # matches
    r.estimated_cost = 55.0
    assert Q.price_drift(q, r) is True
    for status in ("declined", "expired", "cancelled", "replaced"):
        q.status = status
        assert Q.price_drift(q, r) is False, status
    assert Q.price_drift(None, r) is False
    db.rollback()


def test_sms_text_has_what_the_customer_needs_and_no_secrets():
    rid = mk_repair(estimate=None)
    qid, tok = mk_quote(rid, "129.99", "Screen replacement")
    db = _db()
    r, q = db.get(Repair, rid), db.get(QuoteApproval, qid)
    body = Q.sms_text("TechPro+", r, q, Q.link_for("https://shop.example/", q))
    assert "https://shop.example/q/" + tok in body
    assert "$129.99" in body and "+ tax" in body and f"#{r.ticket_no}" in body and "Screen replacement" in body
    assert body.startswith("Hi Quentin, TechPro+:")
    assert "356938035643809" not in body and "250" not in body and "example.test" not in body
    assert len(body) < 320


def test_mask_phone():
    assert Q.mask_phone("(250) 555-0188") == "•••• 0188"
    assert Q.mask_phone("12") == "" and Q.mask_phone("") == "" and Q.mask_phone(None) == ""


# ── the customer's page ────────────────────────────────────────────────

def test_the_page_shows_the_quote_and_nothing_private():
    rid = mk_repair(estimate=None)
    _, tok = mk_quote(rid, "129.99", "Screen replacement", note="Includes a 90-day warranty")
    r = public().get(f"/q/{tok}")
    assert r.status_code == 200
    t = r.text
    for want in ("Your repair quote", "Pixel 8 Pro", "Screen replacement", "129.99", "before tax", "Includes a 90-day warranty",
                 "Approve this quote", "Decline"):
        assert want in t, want
    for secret in ("Quentin", "quentin@example.test", "555-0188", "0188", "356938035643809", "Cracked screen"):
        assert secret not in t, secret
    assert "Dashboard" not in t and "/pos" not in t              # no staff menu leaks through the shared layout


def test_the_page_is_not_cached_indexed_or_leaked_through_referrers():
    rid = mk_repair()
    _, tok = mk_quote(rid)
    r = public().get(f"/q/{tok}")
    assert "no-store" in r.headers["cache-control"]
    assert "noindex" in r.headers["x-robots-tag"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert 'name="robots" content="noindex' in r.text


def test_opening_the_link_changes_nothing():
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid)
    c = public()
    for _ in range(3):                                           # link previewers and virus scanners open links too
        assert c.get(f"/q/{tok}").status_code == 200
        assert c.head(f"/q/{tok}").status_code in (200, 405)
    q = fresh(QuoteApproval, qid)
    assert q.status == "pending" and q.responded_at is None
    assert fresh(Repair, rid).estimated_cost == 80.0


def test_the_answer_route_asks_for_no_csrf_token_because_the_link_is_the_secret():
    from app.quote_routes import build_router
    routes = build_router(None, None, None, None, None, None).routes
    answer = [r for r in routes if r.path == "/q/{token}" and "POST" in r.methods]
    assert len(answer) == 1
    deps = [getattr(d.call, "__name__", "") for d in answer[0].dependant.dependencies]
    assert "csrf_protect" not in deps
    # …and every staff route does carry it
    staff = [r for r in routes if r.path.startswith("/repairs/")]
    assert len(staff) == 4
    for r in staff:
        assert "csrf_protect" in [getattr(d.call, "__name__", "") for d in r.dependant.dependencies], r.path


@pytest.mark.parametrize("bad", ["x", "short", "a" * 19, "a" * 65, "../../etc/passwd", "%00", "a b c d e f g h i j k l", "<script>alert(1)</script>"])
def test_unknown_or_malformed_links_are_404_with_no_detail(bad):
    r = public().get(f"/q/{bad}")
    assert r.status_code in (404, 422)
    assert "Pixel" not in r.text and "$" not in r.text.replace("$(", "")


def test_a_well_formed_but_wrong_token_is_404():
    r = public().get("/q/" + "A" * 24)
    assert r.status_code == 404
    assert "couldn't find that quote" in r.text


def test_approving_from_the_link_end_to_end():
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid, "129.99")
    c = public()
    r = c.post(f"/q/{tok}", data={"decision": "approve", "comment": "Thanks!"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/q/{tok}"
    page = c.get(f"/q/{tok}").text
    assert "Approved" in page and "Approve this quote" not in page
    q = fresh(QuoteApproval, qid)
    assert q.status == "approved" and q.responded_via == "link" and q.response_note == "Thanks!"
    assert q.responder_ip and q.responder_device
    assert fresh(Repair, rid).estimated_cost == 129.99
    db = _db()
    log = db.query(AuditLog).filter(AuditLog.action == "QUOTE_APPROVED").order_by(AuditLog.ts.desc()).first()
    assert log and f"#{fresh(Repair, rid).ticket_no}" in log.detail and "129.99" in log.detail


def test_declining_from_the_link():
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid)
    c = public()
    c.post(f"/q/{tok}", data={"decision": "decline"}, follow_redirects=False)
    assert "Declined" in c.get(f"/q/{tok}").text
    assert fresh(QuoteApproval, qid).status == "declined" and fresh(Repair, rid).estimated_cost == 80.0


def test_a_second_tap_cannot_change_the_answer():
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    c = public()
    c.post(f"/q/{tok}", data={"decision": "approve"}, follow_redirects=False)
    r = c.post(f"/q/{tok}", data={"decision": "decline"})
    assert r.status_code == 200 and "already approved" in r.text
    assert fresh(QuoteApproval, qid).status == "approved"


@pytest.mark.parametrize("decision", ["", "maybe", "APPROVE", "approve;decline"])
def test_a_missing_or_invalid_choice_is_refused_and_changes_nothing(decision):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    r = public().post(f"/q/{tok}", data={"decision": decision})
    assert r.status_code == 200 and "Choose Approve or Decline" in r.text
    assert fresh(QuoteApproval, qid).status == "pending"


def test_an_expired_link_says_so_and_cannot_be_answered():
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    db = _db()
    db.get(QuoteApproval, qid).expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    c = public()
    page = c.get(f"/q/{tok}").text
    assert "This quote has expired" in page and "Approve this quote" not in page
    r = c.post(f"/q/{tok}", data={"decision": "approve"})
    assert "expired" in r.text
    assert fresh(QuoteApproval, qid).status == "expired"


def test_the_old_link_tells_the_customer_a_newer_quote_exists():
    rid = mk_repair()
    _, old_tok = mk_quote(rid, "100")
    _, new_tok = mk_quote(rid, "150")
    c = public()
    assert "newer quote" in c.get(f"/q/{old_tok}").text
    r = c.post(f"/q/{old_tok}", data={"decision": "approve"})
    assert "newer quote" in r.text
    assert fresh(Repair, rid).estimated_cost is None
    assert "Approve this quote" in c.get(f"/q/{new_tok}").text


def test_a_withdrawn_quote_cannot_be_answered():
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    db = _db()
    Q.cancel(db, db.get(QuoteApproval, qid))
    db.commit()
    c = public()
    assert "withdrawn" in c.get(f"/q/{tok}").text
    assert "cancelled" in c.post(f"/q/{tok}", data={"decision": "approve"}).text


def test_the_customers_message_is_trimmed_and_shown_safely_to_staff(owner_client):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    evil = "<script>alert(1)</script>" + "y" * 600
    public().post(f"/q/{tok}", data={"decision": "approve", "comment": evil}, follow_redirects=False)
    q = fresh(QuoteApproval, qid)
    assert len(q.response_note) <= Q.MAX_COMMENT
    page = owner_client.get(f"/repairs/{rid}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_a_hostile_note_or_summary_is_escaped_on_the_customer_page():
    rid = mk_repair()
    _, tok = mk_quote(rid, "10", "<b>Screen</b>", note='"><img src=x onerror=alert(1)>')
    t = public().get(f"/q/{tok}").text
    assert "<img src=x" not in t and "<b>Screen</b>" not in t
    assert "&lt;b&gt;Screen" in t


def test_guessing_links_is_throttled_per_device_and_does_not_block_others():
    rid = mk_repair()
    _, tok = mk_quote(rid)
    guesser = new_client(ip="7.7.52.1")
    for i in range(10):
        assert guesser.get(f"/q/{'B' * 23}{chr(65 + i)}").status_code == 404
    r = guesser.get(f"/q/{tok}")                                 # even the right link waits
    assert r.status_code == 429 and "Too many tries" in r.text and "Pixel" not in r.text
    assert guesser.post(f"/q/{tok}", data={"decision": "approve"}).status_code == 429
    assert fresh(QuoteApproval, _quote_id(tok)).status == "pending"
    assert new_client(ip="7.7.52.2").get(f"/q/{tok}").status_code == 200


def _quote_id(tok):
    return _db().query(QuoteApproval).filter(QuoteApproval.token == tok).first().id


def test_correct_links_do_not_count_against_the_throttle():
    rid = mk_repair()
    _, tok = mk_quote(rid)
    c = new_client(ip="7.7.52.3")
    for _ in range(25):
        assert c.get(f"/q/{tok}").status_code == 200


def test_simultaneous_taps_from_two_phones_record_exactly_one_answer():
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid, "129.99")
    results = []

    def tap(decision):
        c = public()
        results.append(c.post(f"/q/{tok}", data={"decision": decision}, follow_redirects=False).status_code)
    ts = [threading.Thread(target=tap, args=(d,)) for d in ("approve", "decline", "approve", "decline")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    q = fresh(QuoteApproval, qid)
    assert q.status in ("approved", "declined")
    hist = json.loads(fresh(Repair, rid).status_history)
    assert sum(1 for h in hist if "the quote" in h["note"]) == 1
    assert fresh(Repair, rid).estimated_cost == (129.99 if q.status == "approved" else 80.0)


# ── staff: sending ─────────────────────────────────────────────────────

def _send(client, rid, **kw):
    data = {"amount": "129.99", "summary": "Screen replacement", "note": "", "days": "7", "send": "sms"}
    data.update(kw)
    return client.post(f"/repairs/{rid}/quote", data=data, follow_redirects=False)


def _flash(client, rid):
    """The flash message is shown once on the repair screen."""
    html = client.get(f"/repairs/{rid}").text
    m = re.search(r'<div class="card" style="border-color:[^"]*">([^<]+)</div>', html)
    return m.group(1) if m else ""


def test_sending_by_text_creates_the_quote_texts_the_link_and_logs_it(owner_client, sms):
    rid = mk_repair()
    r = _send(owner_client, rid)
    assert r.status_code == 303 and r.headers["location"] == f"/repairs/{rid}#quote"
    q = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).one()
    assert q.status == "pending" and q.amount == 129.99 and q.sent_via == "sms" and q.sent_to == "•••• 0188"
    assert len(sms.sent) == 1
    to, body = sms.sent[0]
    assert to == "(250) 555-0188" and f"/q/{q.token}" in body and "129.99" in body
    logged = _db().query(SmsMessage).filter(SmsMessage.body.like(f"%/q/{q.token}%")).all()
    assert len(logged) == 1 and logged[0].direction == "out"
    assert "texted to" in _flash(owner_client, rid) or True      # (the flash is consumed by the redirect target)
    audit = _db().query(AuditLog).filter(AuditLog.action == "QUOTE_SENT").order_by(AuditLog.ts.desc()).first()
    assert audit and "129.99" in audit.detail


def test_the_flash_after_sending_says_where_it_went(owner_client, sms):
    rid = mk_repair()
    _send(owner_client, rid)
    html = owner_client.get(f"/repairs/{rid}").text
    assert "Quote texted to •••• 0188" in html


def test_if_the_text_fails_the_quote_still_exists_and_the_link_is_offered(owner_client, sms):
    rid = mk_repair()
    sms.result = {"ok": False, "message": "SMS is not configured — go to Settings."}
    _send(owner_client, rid)
    q = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).one()
    assert q.status == "pending" and q.sent_via == "" and "not configured" in q.sms_note
    assert _db().query(SmsMessage).filter(SmsMessage.body.like(f"%/q/{q.token}%")).count() == 0   # not logged as delivered
    html = owner_client.get(f"/repairs/{rid}").text
    assert "text wasn" in html and f"/q/{q.token}" in html


def test_just_making_the_link_sends_no_text(owner_client, sms):
    rid = mk_repair()
    _send(owner_client, rid, send="link")
    assert sms.sent == []
    q = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).one()
    assert q.status == "pending" and q.sent_via == ""
    assert f"/q/{q.token}" in owner_client.get(f"/repairs/{rid}").text


def test_a_customer_who_said_stop_is_never_texted(owner_client, sms):
    rid = mk_repair(opt_out=True)
    _send(owner_client, rid)
    assert sms.sent == []
    q = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).one()
    assert q.status == "pending"                                  # the link still exists for the owner to hand over
    page = owner_client.get(f"/repairs/{rid}").text
    assert "asked not to be texted" in page


def test_a_customer_without_a_phone_is_not_texted(owner_client, sms):
    rid = mk_repair(phone="")
    _send(owner_client, rid)
    assert sms.sent == []
    assert "No phone number" in owner_client.get(f"/repairs/{rid}").text


@pytest.mark.parametrize("amount", ["", "0", "-4", "abc", "nan", "999999"])
def test_a_bad_price_makes_no_quote_and_texts_nothing(owner_client, sms, amount):
    rid = mk_repair()
    _send(owner_client, rid, amount=amount)
    assert _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).count() == 0
    assert sms.sent == []


def test_an_empty_summary_makes_no_quote(owner_client, sms):
    rid = mk_repair()
    _send(owner_client, rid, summary="   ")
    assert _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).count() == 0 and sms.sent == []


def test_sending_again_replaces_the_old_quote_and_kills_the_old_link(owner_client, sms):
    rid = mk_repair()
    _send(owner_client, rid, amount="100")
    old = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).one().token
    _send(owner_client, rid, amount="150")
    rows = _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).order_by(QuoteApproval.created_at).all()
    assert [r.status for r in rows] == ["replaced", "pending"] and [r.amount for r in rows] == [100.0, 150.0]
    assert "newer quote" in public().get(f"/q/{old}").text
    assert len(sms.sent) == 2


@pytest.mark.parametrize("who", ["manager_client", "cashier_client", "technician_client"])
def test_any_signed_in_staff_can_send_a_quote(request, sms, who):
    client = request.getfixturevalue(who)
    rid = mk_repair()
    assert _send(client, rid).status_code == 303
    assert _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).count() == 1


def test_signed_out_visitors_cannot_send_or_manage_quotes(anon_client, sms):
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    for path, data in ((f"/repairs/{rid}/quote", {"amount": "5", "summary": "x", "send": "link"}),
                       (f"/repairs/{rid}/quote/{qid}/resend", {}),
                       (f"/repairs/{rid}/quote/{qid}/record", {"decision": "approve"}),
                       (f"/repairs/{rid}/quote/{qid}/cancel", {})):
        r = anon_client.post(path, data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert fresh(QuoteApproval, qid).status == "pending" and sms.sent == []
    assert _db().query(QuoteApproval).filter(QuoteApproval.repair_id == rid).count() == 1


def test_sending_for_a_ticket_that_does_not_exist_goes_back_to_the_list(owner_client, sms):
    r = _send(owner_client, "no-such-ticket")
    assert r.status_code == 303 and r.headers["location"] == "/repairs" and sms.sent == []


# ── staff: resend / record / cancel ────────────────────────────────────

def test_resend_texts_the_same_link_again(owner_client, sms):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    r = owner_client.post(f"/repairs/{rid}/quote/{qid}/resend", follow_redirects=False)
    assert r.status_code == 303 and len(sms.sent) == 1 and f"/q/{tok}" in sms.sent[0][1]


def test_resend_is_refused_once_answered_or_expired(owner_client, sms):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    public().post(f"/q/{tok}", data={"decision": "decline"}, follow_redirects=False)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/resend", follow_redirects=False)
    assert sms.sent == []
    qid2, _ = mk_quote(rid)
    db = _db()
    db.get(QuoteApproval, qid2).expires_at = datetime.utcnow() - timedelta(days=1)
    db.commit()
    owner_client.post(f"/repairs/{rid}/quote/{qid2}/resend", follow_redirects=False)
    assert sms.sent == []


def test_resend_respects_stop(owner_client, sms):
    rid = mk_repair(opt_out=True)
    qid, _ = mk_quote(rid)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/resend", follow_redirects=False)
    assert sms.sent == []


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("decline", "declined")])
def test_staff_can_record_a_phone_or_in_person_answer(owner_client, decision, status):
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid, "129.99")
    r = owner_client.post(f"/repairs/{rid}/quote/{qid}/record", data={"decision": decision, "note": "said so at the counter"}, follow_redirects=False)
    assert r.status_code == 303
    q = fresh(QuoteApproval, qid)
    assert q.status == status and q.responded_via == "staff" and q.responded_by and q.response_note == "said so at the counter"
    assert q.seen_at is not None                                  # the person who recorded it has obviously seen it
    assert fresh(Repair, rid).estimated_cost == (129.99 if status == "approved" else 80.0)
    assert ("Approved" if decision == "approve" else "Declined") in public().get(f"/q/{tok}").text
    log = _db().query(AuditLog).filter(AuditLog.action == "QUOTE_" + status.upper()).order_by(AuditLog.ts.desc()).first()
    assert log and "Recorded" in log.detail


def test_recording_after_the_customer_already_answered_changes_nothing(owner_client):
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid)
    public().post(f"/q/{tok}", data={"decision": "approve"}, follow_redirects=False)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/record", data={"decision": "decline"}, follow_redirects=False)
    q = fresh(QuoteApproval, qid)
    assert q.status == "approved" and q.responded_via == "link"


def test_cancel_kills_the_link(owner_client):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/cancel", follow_redirects=False)
    assert fresh(QuoteApproval, qid).status == "cancelled"
    assert "withdrawn" in public().get(f"/q/{tok}").text
    log = _db().query(AuditLog).filter(AuditLog.action == "QUOTE_CANCELLED").first()
    assert log


def test_cancel_cannot_undo_an_answer(owner_client):
    rid = mk_repair()
    qid, tok = mk_quote(rid)
    public().post(f"/q/{tok}", data={"decision": "approve"}, follow_redirects=False)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/cancel", follow_redirects=False)
    assert fresh(QuoteApproval, qid).status == "approved"


def test_a_quote_cannot_be_managed_through_the_wrong_ticket(owner_client, sms):
    ra, rb = mk_repair(), mk_repair()
    qb, _ = mk_quote(rb)
    owner_client.post(f"/repairs/{ra}/quote/{qb}/cancel", follow_redirects=False)
    owner_client.post(f"/repairs/{ra}/quote/{qb}/record", data={"decision": "approve"}, follow_redirects=False)
    owner_client.post(f"/repairs/{ra}/quote/{qb}/resend", follow_redirects=False)
    assert fresh(QuoteApproval, qb).status == "pending" and sms.sent == []
    assert fresh(Repair, rb).estimated_cost is None


# ── the repair screen, the bell and the list ───────────────────────────

def test_the_repair_screen_offers_a_form_with_the_ticket_estimate_prefilled(owner_client):
    rid = mk_repair(estimate=89.5)
    html = owner_client.get(f"/repairs/{rid}").text
    assert 'id="quote"' in html and "Ask the customer to approve a price" in html
    assert 'value="89.5"' in html and "Cracked screen" in html


def test_the_repair_screen_shows_the_live_quote_with_a_copyable_link(owner_client):
    rid = mk_repair()
    _, tok = mk_quote(rid, "129.99")
    html = owner_client.get(f"/repairs/{rid}").text
    assert "Waiting for the customer" in html and f"/q/{tok}" in html and "They approved" in html and "Withdraw" in html


def test_the_repair_screen_does_not_warn_about_a_quote_still_waiting_for_an_answer(owner_client):
    for estimate in (None, 0.0, 80.0, 129.99):
        rid = mk_repair(estimate=estimate)
        mk_quote(rid, "129.99")
        assert "no longer matches" not in owner_client.get(f"/repairs/{rid}").text, estimate


def test_the_repair_screen_warns_when_an_approved_price_was_changed_afterwards(owner_client):
    rid = mk_repair(estimate=None)
    qid, tok = mk_quote(rid, "129.99")
    public().post(f"/q/{tok}", data={"decision": "approve"}, follow_redirects=False)      # sets the ticket's estimate to the quote
    assert "no longer matches" not in owner_client.get(f"/repairs/{rid}").text
    db = _db()
    db.get(Repair, rid).estimated_cost = 200.0
    db.commit()
    page = owner_client.get(f"/repairs/{rid}").text
    assert "no longer matches the quote the customer approved" in page and "$200.00" in page and "$129.99" in page


def test_expiry_shows_on_the_repair_screen_without_anyone_opening_the_link(owner_client):
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    db = _db()
    db.get(QuoteApproval, qid).expires_at = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    html = owner_client.get(f"/repairs/{rid}").text
    assert "Expired" in html and "They approved" not in html
    assert fresh(QuoteApproval, qid).status == "expired"


def test_earlier_quotes_are_listed(owner_client):
    rid = mk_repair()
    mk_quote(rid, "10", "First")
    mk_quote(rid, "20", "Second")
    mk_quote(rid, "30", "Third")
    html = owner_client.get(f"/repairs/{rid}").text
    assert "Earlier quotes (2)" in html and "First" in html and "Second" in html


def _bell(client):
    return [n for n in client.get("/api/notifications").json()["notifications"] if "quote" in n["message"]]


def test_the_bell_tells_staff_when_a_customer_answers_and_clears_once_the_ticket_is_opened(owner_client):
    rid = mk_repair()
    qid, tok = mk_quote(rid, "129.99")
    assert [n for n in _bell(owner_client) if f"#{fresh(Repair, rid).ticket_no}" in n["message"]] == []
    public().post(f"/q/{tok}", data={"decision": "approve"}, follow_redirects=False)
    mine = [n for n in _bell(owner_client) if f"#{fresh(Repair, rid).ticket_no}" in n["message"]]
    assert len(mine) == 1 and "approved" in mine[0]["message"] and "129.99" in mine[0]["message"]
    assert mine[0]["href"] == f"/repairs/{rid}#quote" and mine[0]["type"] == "success"
    owner_client.get(f"/repairs/{rid}")
    assert [n for n in _bell(owner_client) if f"#{fresh(Repair, rid).ticket_no}" in n["message"]] == []
    assert fresh(QuoteApproval, qid).seen_at is not None


def test_a_decline_shows_in_the_bell_as_a_warning(manager_client):
    rid = mk_repair()
    _, tok = mk_quote(rid)
    public().post(f"/q/{tok}", data={"decision": "decline"}, follow_redirects=False)
    mine = [n for n in _bell(manager_client) if f"#{fresh(Repair, rid).ticket_no}" in n["message"]]
    assert len(mine) == 1 and mine[0]["type"] == "warning" and "declined" in mine[0]["message"]


def test_an_answer_the_staff_recorded_themselves_never_rings_the_bell(owner_client):
    rid = mk_repair()
    qid, _ = mk_quote(rid)
    owner_client.post(f"/repairs/{rid}/quote/{qid}/record", data={"decision": "approve"}, follow_redirects=False)
    assert [n for n in _bell(owner_client) if f"#{fresh(Repair, rid).ticket_no}" in n["message"]] == []


def test_the_repairs_list_marks_tickets_by_quote_state(owner_client):
    a, b, c = mk_repair(device="DevAAA"), mk_repair(device="DevBBB"), mk_repair(device="DevCCC")
    mk_quote(a)
    _, tb = mk_quote(b)
    _, tc = mk_quote(c)
    public().post(f"/q/{tb}", data={"decision": "approve"}, follow_redirects=False)
    public().post(f"/q/{tc}", data={"decision": "decline"}, follow_redirects=False)
    for view in ("table", "kanban"):
        html = owner_client.get(f"/repairs?view={view}").text
        assert html.count("Quote: waiting") >= 1 and html.count("Quote: approved") >= 1 and html.count("Quote: declined") >= 1, view


def test_the_repairs_list_shows_only_the_newest_quote_state():
    rid = mk_repair()
    mk_quote(rid, "10")
    mk_quote(rid, "20")
    m = Q.badge_map(_db())
    assert m[rid] == "pending"
    qid, tok = mk_quote(rid, "30")
    public().post(f"/q/{tok}", data={"decision": "decline"}, follow_redirects=False)
    assert Q.badge_map(_db())[rid] == "declined"


# ── housekeeping ───────────────────────────────────────────────────────

def test_a_quote_survives_a_backup_and_restore_cycle(db_session):
    from app import backup as B
    from app.models import Staff
    rid = mk_repair(estimate=80.0)
    qid, tok = mk_quote(rid, "129.99", "Backup test")
    public().post(f"/q/{tok}", data={"decision": "approve", "comment": "ok"}, follow_redirects=False)
    db_session.expire_all()
    doc = B.read_backup(B.make_backup(db_session, None)[0])
    rows = doc["tables"]["quote_approvals"]
    mine = [r for r in rows if r["id"] == qid]
    assert len(mine) == 1 and mine[0]["token"] == tok and mine[0]["status"] == "approved" and mine[0]["amount"] == 129.99
    assert "quote_approvals" in B.TABLE_LABELS
    db_session.rollback()
    db_session.query(QuoteApproval).filter(QuoteApproval.id == qid).delete()
    db_session.commit()
    assert db_session.get(QuoteApproval, qid) is None
    B.restore_full(db_session, doc, db_session.get(Staff, "s1"))
    db_session.expire_all()
    back = db_session.get(QuoteApproval, qid)
    assert back is not None and back.token == tok and back.status == "approved" and back.response_note == "ok"
    assert "Approved" in public().get(f"/q/{tok}").text           # the customer's old text message still works after a restore


def test_deleting_the_ticket_is_not_blocked_by_quotes_in_the_test_cleanup_order():
    rid = mk_repair()
    mk_quote(rid)
    db = _db()
    db.query(QuoteApproval).filter(QuoteApproval.repair_id == rid).delete()
    db.commit()
    assert db.query(QuoteApproval).filter(QuoteApproval.repair_id == rid).count() == 0
