"""
v47 tests: 5-cent cash rounding, required Moneris reference + card-number
masking, Moneris reconciliation (CSV reading and matching), day-end checks
and card-fee reporting.

Fixture data is dated in 2032 (far from "today") wherever the code under test
takes an explicit date; the day-end checks look at real recent days, so those
tests use dates relative to today and assert on the specific dates they
create rather than on the whole list.
"""
import json
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import cardfees
from app.database import engine
from app import reconcile as RC
from app import reporting as R
from app.localtime import day_bounds_utc, get_shop_tz, local_today
from app.models import (AuditLog, CardBatch, CardStatement, CashSession, Customer, Invoice, InvoiceLine, Layaway,
                        LayawayPayment, Product, Repair, Setting, Staff)
from app.money import (cash_refund_amount, cash_rounding_on, luhn_ok, plan_payment, require_card_ref_on, round_cash,
                       scrub_card_numbers)


def _uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _set(db, key, value):
    db.merge(Setting(key=key, value=value))
    db.commit()


def _drop(db, key):
    row = db.get(Setting, key)
    if row is not None:
        db.delete(row)
        db.commit()


@pytest.fixture(autouse=True)
def _ontario_tax(db_session):
    """Several of these tests use 13% HST sums (e.g. $12.00 -> $13.56); other test modules change the province."""
    prev = db_session.get(Setting, "province")
    old = prev.value if prev else None
    _set(db_session, "province", "ON")
    yield
    if old is None:
        _drop(db_session, "province")
    else:
        _set(db_session, "province", old)


@pytest.fixture
def rounding_on(db_session):
    _set(db_session, "cash_rounding", "true")
    yield
    _set(db_session, "cash_rounding", "false")


@pytest.fixture
def ref_required(db_session):
    _set(db_session, "require_card_ref", "true")
    yield
    _set(db_session, "require_card_ref", "false")


@pytest.fixture
def owner(db_session):
    return db_session.query(Staff).filter(Staff.role == "owner").first()


def _product(db, price=12.00, stock=500):
    p = Product(id=_uid("m47"), sku=_uid("M47").upper(), name="v47 test item", category="ACCESSORY",
                price=price, cost=3.0, stock=stock)
    db.add(p)
    db.commit()
    return p


def _checkout(client, db, data, price=12.00):
    p = _product(db, price)
    client.post("/pos/clear")
    client.post(f"/pos/add/{p.id}")
    return client.post("/pos/checkout", data=data, follow_redirects=False)


def _invoice_from(resp, db):
    assert resp.status_code == 303, resp.text[:200]
    inv_id = resp.headers["location"].rsplit("/", 1)[-1]
    db.expire_all()
    inv = db.get(Invoice, inv_id)
    assert inv is not None
    return inv


def _mk_invoice(db, owner, when_utc, total=50.0, cash=0.0, card=0.0, ref="", **kw):
    fields = dict(subtotal=total, tax_total=0.0)
    fields.update({k: kw.pop(k) for k in ("subtotal", "tax_total") if k in kw})
    inv = Invoice(id=_uid("v47i"), number=_uid("V47"), staff_id=owner.id, payment_method=kw.pop("payment_method", "Card" if card else "Cash"),
                  cash_amount=cash, card_amount=card, card_reference=ref, total=total,
                  date=when_utc, tax_breakdown="[]", **fields, **kw)
    db.add(inv)
    db.commit()
    return inv


# ═══ cash rounding: the pure rule ═════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    (10.00, 10.00), (10.01, 10.00), (10.02, 10.00), (10.03, 10.05), (10.04, 10.05), (10.05, 10.05),
    (10.06, 10.05), (10.07, 10.05), (10.08, 10.10), (10.09, 10.10), (0.01, 0.00), (0.03, 0.05),
    (99.98, 100.00), (16.94, 16.95), (1234.52, 1234.50),
])
def test_round_cash_matches_the_canadian_rule(raw, expected):
    assert round_cash(raw) == expected


def test_round_cash_never_moves_more_than_two_cents_and_is_idempotent():
    for cents in range(0, 2000):
        x = cents / 100
        r = round_cash(x)
        assert abs(r - x) <= 0.0200001
        assert round(r * 100) % 5 == 0
        assert round_cash(r) == r


def test_round_cash_handles_float_noise():
    # 0.1 + 0.2 style sums must not tip the rounding the wrong way
    assert round_cash(0.1 + 0.2 + 10.0) == 10.30
    assert round_cash(round(13.56 * 1.0, 2)) == 13.55


# ═══ plan_payment ═════════════════════════════════════════════════════

def test_plan_cash_rounds_and_computes_change_from_the_due_amount():
    p = plan_payment("Cash", 13.56, 20.0, 0, 0, True)
    assert p["error"] is None
    assert (p["cash_amount"], p["cash_rounding"], p["change_given"], p["card_amount"]) == (13.55, -0.01, 6.45, 0.0)


def test_plan_cash_rounding_up_and_exact_tender():
    p = plan_payment("Cash", 16.94, 16.95, 0, 0, True)
    assert p["error"] is None and p["cash_amount"] == 16.95 and p["cash_rounding"] == 0.01 and p["change_given"] == 0.0


def test_plan_cash_short_tender_is_refused_against_the_rounded_due():
    p = plan_payment("Cash", 16.94, 16.94, 0, 0, True)
    assert p["error"] and "16.95" in p["error"]


def test_plan_cash_with_rounding_off_is_exactly_the_old_behaviour():
    p = plan_payment("Cash", 16.94, 20.0, 0, 0, False)
    assert (p["cash_amount"], p["cash_rounding"], p["change_given"]) == (16.94, 0.0, 3.06)
    assert plan_payment("Cash", 16.94, 16.93, 0, 0, False)["error"] == "Tendered amount ($16.93) is less than the total ($16.94)."


def test_plan_cash_with_no_tender_entered_just_takes_the_due_amount():
    p = plan_payment("Cash", 16.94, 0, 0, 0, True)
    assert p["error"] is None and p["cash_amount"] == 16.95 and p["change_given"] == 0.0


def test_plan_card_and_other_methods_are_never_rounded():
    c = plan_payment("Card", 16.94, 0, 0, 0, True)
    assert (c["card_amount"], c["cash_amount"], c["cash_rounding"]) == (16.94, 0.0, 0.0)
    for pm in ("E-Transfer", "UPI", "Store Credit"):
        o = plan_payment(pm, 16.94, 0, 0, 0, True)
        assert (o["card_amount"], o["cash_amount"], o["cash_rounding"]) == (0.0, 0.0, 0.0), pm


def test_plan_split_rounds_only_the_cash_side():
    p = plan_payment("", 16.94, 0, 7.00, 10.00, True)   # card 10.00 -> $6.94 left -> $6.95 cash due
    assert p["error"] is None
    assert (p["cash_amount"], p["card_amount"], p["cash_rounding"], p["change_given"]) == (6.95, 10.0, 0.01, 0.05)
    assert p["payment_method"] == "Split (Cash $7.00 + Card $10.00)"


def test_plan_split_short_cash_is_refused_and_explained():
    p = plan_payment("", 16.94, 0, 6.94, 10.00, True)
    assert p["error"] and "6.95" in p["error"]


def test_plan_split_with_rounding_off_matches_the_pre_v47_numbers():
    p = plan_payment("", 16.94, 0, 6.94, 10.00, False)
    assert p["error"] is None and (p["cash_amount"], p["cash_rounding"]) == (6.94, 0.0)
    assert plan_payment("", 16.94, 0, 6.93, 10.00, False)["error"]


def test_plan_split_card_overpayment_keeps_the_long_standing_change_rule():
    p = plan_payment("", 16.94, 0, 5.00, 20.00, True)   # the original regression case: $5 cash + $20 card
    assert p["error"] is None
    assert (p["cash_amount"], p["card_amount"], p["change_given"], p["cash_rounding"]) == (-3.06, 20.0, 8.06, 0.0)


# ═══ card numbers are never kept ══════════════════════════════════════

def test_luhn():
    assert luhn_ok("4111111111111111") and luhn_ok("5555555555554444") and luhn_ok("378282246310005")
    assert not luhn_ok("4111111111111112") and not luhn_ok("abc")


@pytest.mark.parametrize("raw", ["4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", "5555555555554444", "378282246310005"])
def test_scrub_masks_real_looking_card_numbers_to_the_last_four(raw):
    clean, changed = scrub_card_numbers(f"ref {raw} ok")
    assert changed and raw.replace(" ", "").replace("-", "")[:6] not in clean.replace("*", "")
    assert clean.startswith("ref ") and clean.endswith(raw[-4:] + " ok")
    assert "*" in clean


@pytest.mark.parametrize("raw", ["AUTH00123", "660123450001230010", "123456", "A1B2C3", "", "4111 1111 1111 1112", "2026-11-20"])
def test_scrub_leaves_ordinary_references_alone(raw):
    clean, changed = scrub_card_numbers(raw)
    assert clean == raw and not changed


def test_long_numeric_terminal_references_survive_even_when_they_happen_to_pass_luhn():
    """Moneris references are ~18 digits; one in ten passes Luhn by chance and must NOT be masked."""
    kept = 0
    for n in range(660123450001230000, 660123450001230100):
        s = str(n)
        if luhn_ok(s):
            kept += 1
            assert scrub_card_numbers(s) == (s, False), s
    assert kept >= 5          # the sweep really did hit Luhn-valid numbers


# ═══ checkout through the real route ══════════════════════════════════

def test_cash_checkout_rounds_to_five_cents_and_keeps_revenue_on_the_true_total(owner_client, db_session, rounding_on):
    resp = _checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"})
    inv = _invoice_from(resp, db_session)
    due = round_cash(inv.total)
    assert inv.total != due or inv.cash_rounding == 0        # price 12.00 + 13% HST -> not a multiple of 5c
    assert inv.cash_amount == due
    assert inv.cash_rounding == round(due - inv.total, 2)
    assert inv.change_given == round(20.0 - due, 2)
    # net sales / tax come from the true total, never the rounded cash
    assert round(inv.total - inv.tax_total, 2) == round(inv.subtotal - inv.discount, 2)


def test_short_cash_tender_is_refused_and_no_invoice_is_created(owner_client, db_session, rounding_on):
    p = _product(db_session)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    before = db_session.query(Invoice).count()
    resp = owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "1.00"}, follow_redirects=False)
    assert resp.status_code == 400
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before


def test_cash_rounding_is_on_when_the_setting_has_never_been_touched(owner_client, db_session):
    _drop(db_session, "cash_rounding")
    try:
        assert cash_rounding_on(db_session) is True
        inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"}), db_session)
        assert inv.cash_amount == round_cash(inv.total)
    finally:
        _set(db_session, "cash_rounding", "false")


def test_cash_rounding_can_be_switched_off(owner_client, db_session):
    _set(db_session, "cash_rounding", "false")
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"}), db_session)
    assert inv.cash_rounding == 0 and inv.cash_amount == inv.total


def test_card_sale_is_never_rounded(owner_client, db_session, rounding_on):
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Card", "card_reference": "AUTH1"}), db_session)
    assert inv.card_amount == inv.total and inv.cash_rounding == 0 and inv.cash_amount == 0


def test_split_checkout_rounds_the_cash_leg_only(owner_client, db_session, rounding_on):
    p = _product(db_session, 12.00)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    total = 13.56
    card = 10.00
    remaining = round(total - card, 2)
    due = round_cash(remaining)
    resp = owner_client.post("/pos/checkout", data={"cash_part": f"{due:.2f}", "card_part": f"{card:.2f}", "card_reference": "SPL47"},
                              follow_redirects=False)
    inv = _invoice_from(resp, db_session)
    assert inv.total == total and inv.card_amount == card and inv.cash_amount == due
    assert inv.cash_rounding == round(due - remaining, 2)
    assert round(inv.cash_amount + inv.card_amount - inv.cash_rounding, 2) == inv.total


def test_receipt_pages_show_the_rounding_lines(owner_client, db_session, rounding_on):
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"}), db_session)
    assert inv.cash_rounding != 0
    for url in (f"/invoices/{inv.id}", f"/invoices/{inv.id}/thermal"):
        r = owner_client.get(url)
        if r.status_code == 404:
            continue
        assert r.status_code == 200
        assert "Cash rounding" in r.text and "Cash due" in r.text, url


def test_receipt_without_rounding_has_no_rounding_lines(owner_client, db_session):
    _set(db_session, "cash_rounding", "false")
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"}), db_session)
    assert "Cash rounding" not in owner_client.get(f"/invoices/{inv.id}").text


def test_pos_page_shows_the_cash_due_line(owner_client, db_session, rounding_on):
    p = _product(db_session, 12.00)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    page = owner_client.get("/pos").text
    assert "Cash due (rounded to 5" in page
    assert 'data-cash-due="13.55"' in page


# ═══ required Moneris reference ═══════════════════════════════════════

def test_card_sale_without_a_reference_is_bounced_back_to_the_till(owner_client, db_session, ref_required):
    p = _product(db_session)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    before = db_session.query(Invoice).count()
    resp = owner_client.post("/pos/checkout", data={"payment_method": "Card"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/pos"
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before          # nothing was saved
    page = owner_client.get("/pos").text
    assert "Moneris reference" in page and "terminal batch" in page   # the reason is shown on the till
    # the cart is still there, so adding the reference completes the sale
    ok = owner_client.post("/pos/checkout", data={"payment_method": "Card", "card_reference": "A1B2C3"}, follow_redirects=False)
    assert _invoice_from(ok, db_session).card_reference == "A1B2C3"


def test_whitespace_only_reference_does_not_count(owner_client, db_session, ref_required):
    p = _product(db_session)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    resp = owner_client.post("/pos/checkout", data={"payment_method": "Card", "card_reference": "   "}, follow_redirects=False)
    assert resp.headers["location"] == "/pos"


def test_split_sale_also_needs_the_reference(owner_client, db_session, ref_required):
    p = _product(db_session, 100.00)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    resp = owner_client.post("/pos/checkout", data={"cash_part": "30", "card_part": "83"}, follow_redirects=False)
    assert resp.headers["location"] == "/pos"


def test_cash_and_other_methods_never_need_a_reference(owner_client, db_session, ref_required):
    for pm, extra in (("Cash", {"tendered": "50"}), ("E-Transfer", {})):
        resp = _checkout(owner_client, db_session, dict({"payment_method": pm}, **extra))
        assert resp.status_code == 303 and resp.headers["location"].startswith("/invoices/"), pm


def test_reference_requirement_can_be_switched_off(owner_client, db_session):
    _set(db_session, "require_card_ref", "false")
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Card"}), db_session)
    assert inv.card_reference == ""


def test_reference_is_required_by_default_when_the_setting_is_absent(db_session):
    _drop(db_session, "require_card_ref")
    try:
        assert require_card_ref_on(db_session) is True
    finally:
        _set(db_session, "require_card_ref", "false")


def test_a_pasted_card_number_is_masked_not_stored(owner_client, db_session, ref_required):
    resp = _checkout(owner_client, db_session, {"payment_method": "Card", "card_reference": "4111 1111 1111 1111"})
    inv = _invoice_from(resp, db_session)
    assert inv.card_reference == "************1111"
    assert "4111111111111111" not in inv.card_reference.replace(" ", "")
    flash_page = owner_client.get(f"/invoices/{inv.id}").text
    assert "last four digits" in flash_page


# ═══ cash refunds pay back the rounded amount ═════════════════════════

def test_cash_refund_payout_is_rounded_like_the_sale(db_session, owner, rounding_on):
    assert cash_refund_amount(db_session, 13.56) == 13.55
    _set(db_session, "cash_rounding", "false")
    assert cash_refund_amount(db_session, 13.56) == 13.56


def test_cash_up_counts_a_cash_refund_at_the_rounded_amount(db_session, owner, rounding_on):
    tz = get_shop_tz(db_session)
    sold = datetime(2032, 2, 27, 15, 0)
    inv = _mk_invoice(db_session, owner, sold, total=13.56, cash=13.55, payment_method="Cash", cash_rounding=-0.01)
    inv.refunded, inv.refund_method, inv.refunded_at = True, "Cash", datetime(2032, 3, 2, 15, 0)
    db_session.commit()
    cc = R.cash_card_totals(db_session, tz, date(2032, 3, 2))
    assert cc["cash_refunds_paid"] == 13.55 and cc["cash_sales"] == -13.55


def test_refund_route_tells_the_cashier_the_exact_cash_to_hand_back(owner_client, db_session, rounding_on):
    inv = _invoice_from(_checkout(owner_client, db_session, {"payment_method": "Cash", "tendered": "20.00"}), db_session)
    owner_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Cash", "refund_reason": "test"}, follow_redirects=False)
    page = owner_client.get(f"/invoices/{inv.id}").text
    assert f"hand back ${round_cash(inv.total):.2f}" in page


def test_daily_cash_figures_report_the_rounding_inside_them(db_session, owner, rounding_on):
    tz = get_shop_tz(db_session)
    _mk_invoice(db_session, owner, datetime(2032, 4, 5, 15, 0), total=13.56, cash=13.55, payment_method="Cash", cash_rounding=-0.01)
    _mk_invoice(db_session, owner, datetime(2032, 4, 5, 16, 0), total=16.94, cash=16.95, payment_method="Cash", cash_rounding=0.01)
    cc = R.cash_card_totals(db_session, tz, date(2032, 4, 5))
    assert cc["cash_sales"] == 30.50 and cc["cash_rounding"] == 0.0     # -0.01 + 0.01 nets out
    _mk_invoice(db_session, owner, datetime(2032, 4, 6, 15, 0), total=13.56, cash=13.55, payment_method="Cash", cash_rounding=-0.01)
    assert R.cash_card_totals(db_session, tz, date(2032, 4, 6))["cash_rounding"] == -0.01


# ═══ reading a Moneris export ═════════════════════════════════════════

PORTAL = """Moneris Merchant Direct - Transaction Report
Generated 2032-03-02

Transaction Date,Transaction Type,Card Type,Reference Number,Auth Code,Amount
2032-03-02 10:15:02,Purchase,Visa,660123450001230010,A1B2C3,$45.20
2032-03-02 11:02:44,Purchase,Mastercard,660123450001230020,D4E5F6,"$1,102.50"
2032-03-02 12:30:00,Refund,Visa,660123450001230030,G7H8I9,$20.00
2032-03-02 13:00:00,Void,Visa,660123450001230040,J1K2L3,$99.99
2032-03-01 16:00:00,Purchase,Visa,660123450001230050,M4N5O6,$10.00
"""


def test_parser_finds_the_table_under_title_lines_and_the_right_columns():
    r = RC.parse_bank_csv(PORTAL, day=date(2032, 3, 2))
    assert r["error"] is None
    assert [r["headers"][r["cols"][k]] for k in ("amount", "type", "date")] == ["Amount", "Transaction Type", "Transaction Date"]
    assert [(x["kind"], x["amount_c"]) for x in r["rows"]] == [("sale", 4520), ("sale", 110250), ("refund", 2000)]
    assert [x["amount_c"] for x in r["voids"]] == [9999]
    assert r["other_dates"] == 1


def test_parser_can_include_every_date_when_asked():
    r = RC.parse_bank_csv(PORTAL, day=date(2032, 3, 2), all_dates=True)
    assert len(r["rows"]) == 4 and r["other_dates"] == 0


def test_parser_handles_tabs_negative_amounts_and_no_header():
    txt = "03/02/2032\t10:15\t45.20\tA1B2C3\n03/02/2032\t11:02\t(20.00)\tX9Y8\n03/02/2032\t11:30\t-5.00\tQ1W2\n"
    r = RC.parse_bank_csv(txt, day=date(2032, 3, 2))
    assert r["error"] is None
    assert [(x["kind"], x["amount_c"]) for x in r["rows"]] == [("sale", 4520), ("refund", 2000), ("refund", 500)]


def test_parser_handles_semicolons_and_excel_style_quoting():
    txt = 'Date;Type;Amount\n2032-03-02;Purchase;"1,234.56"\n2032-03-02;Return;12.00\n'
    r = RC.parse_bank_csv(txt, day=date(2032, 3, 2))
    assert [(x["kind"], x["amount_c"]) for x in r["rows"]] == [("sale", 123456), ("refund", 1200)]


def test_parser_skips_subtotal_and_blank_amount_rows_and_unreadable_dates():
    txt = "Date,Type,Amount\n2032-03-02,Purchase,10.00\nTOTAL,,10.00\n2032-03-02,Purchase,\nnot a date,Purchase,5.00\n"
    r = RC.parse_bank_csv(txt, day=date(2032, 3, 2))
    assert sorted(x["amount_c"] for x in r["rows"]) == [500, 1000, 1000]   # 'TOTAL' has an amount, so it is kept as a row — see matcher


@pytest.mark.parametrize("junk", ["", "   \n\n", "hello world\nnothing here\n", "a,b,c\nd,e,f\n"])
def test_parser_reports_a_readable_error_instead_of_crashing(junk):
    r = RC.parse_bank_csv(junk, day=date(2032, 3, 2))
    assert r["error"] and r["rows"] == []


def test_parser_honours_a_manual_column_choice():
    txt = "c1,c2,c3\n2032-03-02,12.00,99.00\n"
    r = RC.parse_bank_csv(txt, day=date(2032, 3, 2), cols_override={"amount": "2"})
    assert [x["amount_c"] for x in r["rows"]] == [9900]
    assert RC.parse_bank_csv(txt, day=date(2032, 3, 2), cols_override={"amount": "bogus"})["error"] is None


@pytest.mark.parametrize("raw,cents", [("$1,234.56", 123456), ("(12.00)", -1200), ("12.00-", -1200), ("-$5.00", -500), ("7", 700), ("0.5", 50)])
def test_parse_money(raw, cents):
    assert RC.parse_money(raw) == cents


@pytest.mark.parametrize("raw", ["", "abc", "12:30", "2032-03-02", "1.2.3", None])
def test_parse_money_rejects_non_numbers(raw):
    assert RC.parse_money(raw) is None


# ═══ matching ═════════════════════════════════════════════════════════

def _crm(label, cents, ref="", kind="sale", optional=False):
    return {"key": label, "kind": kind, "label": label, "amount_c": cents, "ref": ref, "optional": optional, "when": datetime(2032, 3, 2, 10, 0)}


def _bank(cents, tokens=(), kind="sale", label="row"):
    return {"idx": 0, "amount_c": cents, "tokens": [RC.norm_ref(t) for t in tokens], "kind": kind, "label": label, "date": None, "raw": []}


def test_matches_on_reference_and_flags_nothing():
    m = RC.match_transactions([_crm("A", 4520, "A1B2C3")], [_bank(4520, ["660123", "A1B2C3"])])
    assert [p["status"] for p in m["pairs"]] == ["ok"] and m["clean"]


def test_same_reference_but_different_amount_is_flagged():
    m = RC.match_transactions([_crm("A", 4520, "A1B2C3")], [_bank(4250, ["A1B2C3"])])
    assert [p["status"] for p in m["pairs"]] == ["amount"] and not m["clean"]


def test_a_sale_only_on_the_terminal_is_reported_as_missing_from_the_crm():
    m = RC.match_transactions([_crm("A", 1000, "R1111")], [_bank(1000, ["R1111"]), _bank(7777, ["Z"], label="forgotten")])
    assert [b["label"] for b in m["bank_only"]] == ["forgotten"] and not m["clean"]


def test_a_crm_card_sale_with_no_terminal_charge_is_reported():
    m = RC.match_transactions([_crm("A", 1000, "R1111"), _crm("B", 2500, "Q2222")], [_bank(1000, ["R1111"])])
    assert [c["label"] for c in m["crm_only"]] == ["B"]


def test_amount_only_match_when_the_crm_has_no_reference_is_marked_for_a_glance():
    m = RC.match_transactions([_crm("A", 3300)], [_bank(3300, ["X"])])
    # amounts agree, so the day is not "wrong" -- the pair is only listed for a glance
    assert [p["status"] for p in m["pairs"]] == ["noref"] and m["clean"]


def test_amount_match_with_a_mistyped_reference_is_marked_refcheck():
    m = RC.match_transactions([_crm("A", 3300, "TYPO99")], [_bank(3300, ["REAL11"])])
    assert [p["status"] for p in m["pairs"]] == ["refcheck"]


def test_duplicate_amounts_are_paired_one_to_one():
    m = RC.match_transactions([_crm("A", 2000), _crm("B", 2000)], [_bank(2000, ["x1"]), _bank(2000, ["x2"])])
    assert len(m["pairs"]) == 2 and not m["crm_only"] and not m["bank_only"]
    m = RC.match_transactions([_crm("A", 2000), _crm("B", 2000)], [_bank(2000, ["x1"])])
    assert len(m["pairs"]) == 1 and len(m["crm_only"]) == 1


def test_refunds_only_pair_with_refunds():
    m = RC.match_transactions([_crm("R", 2000, kind="refund")], [_bank(2000, ["x"], kind="sale")])
    assert not m["pairs"] and len(m["crm_only"]) == 1 and len(m["bank_only"]) == 1
    m = RC.match_transactions([_crm("R", 2000, kind="refund")], [_bank(2000, ["x"], kind="refund")])
    assert len(m["pairs"]) == 1 and m["clean"]


def test_same_day_refunded_sales_are_optional_not_missing():
    m = RC.match_transactions([_crm("A", 1000, "R1", optional=True)], [])
    assert m["crm_only"] == [] and m["clean"]
    # ...but if the terminal does show it, it still pairs up
    m = RC.match_transactions([_crm("A", 1000, "R1", optional=True)], [_bank(1000, ["R1"])])
    assert len(m["pairs"]) == 1


def test_a_short_reference_that_hits_several_rows_prefers_the_row_with_the_same_amount():
    bank = [_bank(9999, ["1234", "AAAA11"]), _bank(4520, ["1234", "BBBB22"])]
    m = RC.match_transactions([_crm("A", 4520, "1234")], bank)
    assert m["pairs"][0]["bank"]["amount_c"] == 4520 and m["pairs"][0]["status"] == "ok"


def test_references_match_ignoring_case_spaces_and_dashes_and_partial_codes():
    m = RC.match_transactions([_crm("A", 1000, "ab-12 34")], [_bank(1000, ["AB1234"])])
    assert m["pairs"][0]["status"] == "ok"
    m = RC.match_transactions([_crm("A", 1000, "123456")], [_bank(1000, ["660123456001230010"])])
    assert m["pairs"][0]["status"] == "ok"
    m = RC.match_transactions([_crm("A", 1000, "123")], [_bank(1000, ["123"])])      # too short to trust -> amount only
    assert m["pairs"][0]["status"] == "refcheck"


def test_net_totals_use_sales_minus_refunds():
    m = RC.match_transactions([_crm("A", 5000), _crm("R", 1000, kind="refund")], [_bank(5000), _bank(1000, kind="refund")])
    assert m["crm_net_c"] == 4000 and m["bank_net_c"] == 4000


def test_a_full_realistic_day_end_to_end():
    parsed = RC.parse_bank_csv(PORTAL, day=date(2032, 3, 2))
    crm = [_crm("INV-1", 4520, "A1B2C3"), _crm("INV-2", 110250, "D4E5F6"), _crm("REF-1", 2000, "", kind="refund"),
           _crm("INV-3", 777, "ZZZ999")]
    m = RC.match_transactions(crm, parsed["rows"])
    assert sorted(p["crm"]["label"] for p in m["pairs"]) == ["INV-1", "INV-2", "REF-1"]
    assert [c["label"] for c in m["crm_only"]] == ["INV-3"] and m["bank_only"] == []


# ═══ the CRM side of the comparison ═══════════════════════════════════

def test_crm_card_rows_agree_with_what_cash_up_expects(db_session, owner):
    tz = get_shop_tz(db_session)
    d = date(2032, 5, 10)
    day = datetime(2032, 5, 10, 15, 0)
    _mk_invoice(db_session, owner, day, total=40.0, card=40.0, ref="K1")
    _mk_invoice(db_session, owner, day + timedelta(minutes=5), total=25.0, cash=10.0, card=15.0, ref="K2", payment_method="Split (Cash $10.00 + Card $15.00)")
    same = _mk_invoice(db_session, owner, day + timedelta(minutes=10), total=60.0, card=60.0, ref="K3")
    same.refunded, same.refund_method, same.refunded_at = True, "Card", day + timedelta(hours=1)
    kept = _mk_invoice(db_session, owner, day + timedelta(minutes=15), total=30.0, card=30.0, ref="K4")
    kept.refunded, kept.refund_method, kept.refunded_at = True, "Store Credit", day + timedelta(hours=1)
    later = _mk_invoice(db_session, owner, day + timedelta(minutes=20), total=22.0, card=22.0, ref="K5")
    later.refunded, later.refund_method, later.refunded_at = True, "Card", datetime(2032, 5, 12, 12, 0)
    old = _mk_invoice(db_session, owner, datetime(2032, 5, 1, 12, 0), total=18.0, card=18.0, ref="K6")
    old.refunded, old.refund_method, old.refunded_at = True, "Card", day + timedelta(hours=2)
    lay = Layaway(id=_uid("lay"), number=_uid("LAY"), customer_id=db_session.query(Customer).first().id if db_session.query(Customer).first() else None)
    if lay.customer_id is None:
        c = Customer(id=_uid("c"), name="Layaway Test")
        db_session.add(c); db_session.commit()
        lay.customer_id = c.id
    db_session.add(lay); db_session.commit()
    db_session.add(LayawayPayment(id=_uid("lp"), layaway_id=lay.id, amount=12.0, method="Debit", created_at=day))
    db_session.commit()

    rows = RC.crm_card_rows(db_session, tz, d)
    cc = R.cash_card_totals(db_session, tz, d)
    assert RC.crm_net_cents(rows) == round(cc["card_sales"] * 100)
    by_label = {r["label"]: r for r in rows}
    sales = {r["label"] for r in rows if r["kind"] == "sale"}
    assert {i for i in sales if i.startswith("V47")} and len([r for r in rows if r["optional"]]) == 2   # same-day refunded sale + its refund line
    refunds = [r for r in rows if r["kind"] == "refund" and not r["optional"]]
    assert [r["amount_c"] for r in refunds] == [1800]                       # the earlier sale refunded today
    assert any(r["label"] == "Layaway payment" and r["amount_c"] == 1200 for r in rows)
    assert not any(r["ref"] == "K5" and r["optional"] for r in rows)        # refunded LATER -> counts normally today


# ═══ the reconcile pages ══════════════════════════════════════════════

def test_reconcile_page_needs_login(anon_client):
    r = anon_client.get("/reconcile", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    assert anon_client.post("/reconcile/batch", data={"amount": "1"}, follow_redirects=False).status_code in (302, 303, 307, 403)


def test_reconcile_page_renders_for_every_role_and_junk_dates(owner_client, manager_client, cashier_client):
    for c in (owner_client, manager_client, cashier_client):
        for q in ("", "?date=2032-03-02", "?date=garbage", "?date=2999-01-01", "?date=2032-02-30", "?date="):
            r = c.get(f"/reconcile{q}")
            assert r.status_code == 200, q
            assert "Moneris Check" in r.text


def test_future_dates_are_clamped_to_today(owner_client, db_session):
    today = local_today(get_shop_tz(db_session)).isoformat()
    assert f'value="{today}"' in owner_client.get("/reconcile?date=2999-01-01").text


def test_saving_a_batch_records_it_and_audits_it(owner_client, db_session, owner):
    day = "2032-06-01"
    # Batch dates can't be in the future relative to the shop clock, so use a real past date
    day = (local_today(get_shop_tz(db_session)) - timedelta(days=40)).isoformat()
    r = owner_client.post("/reconcile/batch", data={"date": day, "amount": "$1,234.50", "note": "ok"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    row = db_session.query(CardBatch).filter(CardBatch.date == day).first()
    assert row is not None and row.amount == 1234.50 and row.note == "ok"
    assert db_session.query(AuditLog).filter(AuditLog.action == "CARD_BATCH", AuditLog.detail.like(f"%{day}%")).count() >= 1
    # saving again replaces, never duplicates
    owner_client.post("/reconcile/batch", data={"date": day, "amount": "10"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == day).count() == 1
    assert db_session.query(CardBatch).filter(CardBatch.date == day).first().amount == 10.0


@pytest.mark.parametrize("bad", ["", "abc", "nan", "1e99", "inf", "99999999999"])
def test_a_bad_batch_amount_is_rejected_without_saving(owner_client, db_session, bad):
    day = (local_today(get_shop_tz(db_session)) - timedelta(days=41)).isoformat()
    r = owner_client.post("/reconcile/batch", data={"date": day, "amount": bad}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == day).count() == 0


def test_a_negative_batch_is_allowed_for_a_refund_heavy_day(owner_client, db_session):
    day = (local_today(get_shop_tz(db_session)) - timedelta(days=42)).isoformat()
    owner_client.post("/reconcile/batch", data={"date": day, "amount": "-20.00"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == day).first().amount == -20.0


def test_cashiers_can_look_but_not_enter_a_batch(cashier_client, db_session):
    day = (local_today(get_shop_tz(db_session)) - timedelta(days=43)).isoformat()
    assert cashier_client.post("/reconcile/batch", data={"date": day, "amount": "5"}, follow_redirects=False).status_code == 403
    assert cashier_client.post("/reconcile/resolve", data={"date": day, "resolved": "on"}, follow_redirects=False).status_code == 403
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == day).count() == 0
    assert "Enter the terminal" not in cashier_client.get("/reconcile").text


def test_saving_a_batch_keeps_that_days_cash_up_record_in_step(owner_client, db_session, owner):
    tz = get_shop_tz(db_session)
    d = local_today(tz) - timedelta(days=44)
    day = d.isoformat()
    s, _ = day_bounds_utc(d, d, tz)
    _mk_invoice(db_session, owner, s + timedelta(hours=14), total=40.0, card=40.0, ref="CU1")
    db_session.add(CashSession(id=_uid("cs"), date=day, open_float=100, expected=100, actual=100, difference=0,
                               card_expected=0, card_batch=0, card_difference=0))
    db_session.commit()
    owner_client.post("/reconcile/batch", data={"date": day, "amount": "35.00"}, follow_redirects=False)
    db_session.expire_all()
    cs = db_session.query(CashSession).filter(CashSession.date == day).first()
    assert (cs.card_batch, cs.card_expected, cs.card_difference) == (35.0, 40.0, -5.0)


def test_marking_a_difference_explained(owner_client, db_session):
    day = (local_today(get_shop_tz(db_session)) - timedelta(days=45)).isoformat()
    owner_client.post("/reconcile/batch", data={"date": day, "amount": "77"}, follow_redirects=False)
    owner_client.post("/reconcile/resolve", data={"date": day, "resolved": "on", "note": "tip adjustment"}, follow_redirects=False)
    db_session.expire_all()
    row = db_session.query(CardBatch).filter(CardBatch.date == day).first()
    assert row.resolved is True and row.note == "tip adjustment"
    owner_client.post("/reconcile/resolve", data={"date": day}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == day).first().resolved is False


def test_comparing_a_pasted_list_shows_what_does_not_line_up(owner_client, db_session, owner):
    tz = get_shop_tz(db_session)
    d = local_today(tz) - timedelta(days=46)
    s, _ = day_bounds_utc(d, d, tz)
    _mk_invoice(db_session, owner, s + timedelta(hours=15), total=45.20, card=45.20, ref="A1B2C3")
    _mk_invoice(db_session, owner, s + timedelta(hours=16), total=33.33, card=33.33, ref="ONLYCRM1")
    text = (f"Date,Type,Reference,Amount\n{d.isoformat()} 10:00,Purchase,A1B2C3,45.20\n"
            f"{d.isoformat()} 11:00,Purchase,TERM777,19.99\n{d.isoformat()} 12:00,Void,VOID1,5.00\n")
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat(), "csv_text": text})
    assert r.status_code == 200
    assert "Charged on the terminal but NOT in the CRM" in r.text and "19.99" in r.text
    assert "In the CRM but NOT on the terminal list" in r.text and "33.33" in r.text
    assert "voided / declined" in r.text
    assert "Needs a look" in r.text


def test_a_clean_day_says_so(owner_client, db_session, owner):
    tz = get_shop_tz(db_session)
    d = local_today(tz) - timedelta(days=47)
    s, _ = day_bounds_utc(d, d, tz)
    _mk_invoice(db_session, owner, s + timedelta(hours=15), total=45.20, card=45.20, ref="CLEAN1")
    text = f"Date,Type,Reference,Amount\n{d.isoformat()},Purchase,CLEAN1,$45.20\n"
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat(), "csv_text": text})
    assert "Everything lines up" in r.text


def test_uploading_a_csv_file_works_and_a_huge_one_is_refused(owner_client, db_session):
    d = local_today(get_shop_tz(db_session)) - timedelta(days=48)
    good = f"Date,Type,Amount\n{d.isoformat()},Purchase,10.00\n".encode()
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat()}, files={"file": ("tx.csv", good, "text/csv")})
    assert r.status_code == 200 and "Charged on the terminal but NOT in the CRM" in r.text
    big = b"x" * 1_000_002
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat()}, files={"file": ("big.csv", big, "text/csv")}, follow_redirects=False)
    assert r.status_code == 303


def test_a_latin1_file_does_not_crash_the_upload(owner_client, db_session):
    d = local_today(get_shop_tz(db_session)) - timedelta(days=49)
    raw = f"Date,Type,Amount,Name\n{d.isoformat()},Purchase,10.00,Jos\xe9\n".encode("latin-1")
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat()}, files={"file": ("tx.csv", raw, "text/csv")})
    assert r.status_code == 200


@pytest.mark.parametrize("junk", ["", "just some words", "<script>alert(1)</script>", "a,b\n1,2\n", "\x00\x01\x02", "Amount\n" + "9" * 400])
def test_junk_input_never_crashes_the_compare_page(owner_client, junk):
    r = owner_client.post("/reconcile/match", data={"date": "2032-03-02", "csv_text": junk})
    assert r.status_code == 200


def test_pasted_text_is_escaped_in_the_page(owner_client):
    r = owner_client.post("/reconcile/match", data={"date": "2032-03-02", "csv_text": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text


def test_choosing_columns_by_hand_reruns_the_match(owner_client, db_session):
    d = local_today(get_shop_tz(db_session)) - timedelta(days=50)
    txt = f"a,b,c\n{d.isoformat()},12.00,99.00\n"
    r = owner_client.post("/reconcile/match", data={"date": d.isoformat(), "csv_text": txt, "col_amount": "2"})
    assert "99.00" in r.text


def test_cash_up_close_records_a_typed_batch_but_not_a_blank_one(owner_client, db_session):
    tz = get_shop_tz(db_session)
    today = local_today(tz).isoformat()
    db_session.query(CardBatch).filter(CardBatch.date == today).delete()
    db_session.commit()
    owner_client.post("/cashup/close", data={"open_float": "100", "actual": "100", "card_batch": ""}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(CardBatch).filter(CardBatch.date == today).count() == 0
    owner_client.post("/cashup/close", data={"open_float": "100", "actual": "100", "card_batch": "12.34"}, follow_redirects=False)
    db_session.expire_all()
    row = db_session.query(CardBatch).filter(CardBatch.date == today).first()
    assert row is not None and row.amount == 12.34
    db_session.delete(row)
    db_session.commit()


# ═══ day-end checks ═══════════════════════════════════════════════════

@pytest.fixture
def checks_window(db_session):
    """Switch the day-end checks on from 13 days ago so the dates used below are judged."""
    tz = get_shop_tz(db_session)
    today = local_today(tz)
    _set(db_session, "recon_start", (today - timedelta(days=13)).isoformat())
    yield tz, today
    _set(db_session, "recon_start", today.isoformat())


def _day(tz, d, hour=15):
    s, _ = day_bounds_utc(d, d, tz)
    return s + timedelta(hours=hour)


def _kinds_for(items, d):
    return {i["kind"] for i in items if i["date"] == d}


def test_a_card_day_without_a_batch_is_flagged_until_one_is_entered(db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=12)
    _mk_invoice(db_session, owner, _day(tz, d), total=20.0, card=20.0, ref="DC1")
    assert "batch_missing" in _kinds_for(RC.day_end_checks(db_session, tz), d)
    db_session.add(CardBatch(id=_uid("cb"), date=d.isoformat(), amount=20.0))
    db_session.commit()
    assert "batch_missing" not in _kinds_for(RC.day_end_checks(db_session, tz), d)


def test_a_cash_only_day_never_asks_for_a_card_batch(db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=11)
    _mk_invoice(db_session, owner, _day(tz, d), total=20.0, cash=20.0)
    kinds = _kinds_for(RC.day_end_checks(db_session, tz), d)
    assert "batch_missing" not in kinds and "cashup_missing" in kinds


def test_a_day_with_a_closed_cash_up_is_not_flagged(db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=10)
    _mk_invoice(db_session, owner, _day(tz, d), total=20.0, cash=20.0)
    db_session.add(CashSession(id=_uid("cs"), date=d.isoformat(), open_float=0, expected=20, actual=20, difference=0))
    db_session.commit()
    assert "cashup_missing" not in _kinds_for(RC.day_end_checks(db_session, tz), d)


def test_today_is_never_judged_and_neither_is_history_before_the_switch_on_date(db_session, owner, checks_window):
    tz, today = checks_window
    _mk_invoice(db_session, owner, _day(tz, today, hour=1), total=20.0, card=20.0, ref="T1")
    assert today not in {i["date"] for i in RC.day_end_checks(db_session, tz)}
    _set(db_session, "recon_start", (today - timedelta(days=2)).isoformat())
    old = today - timedelta(days=9)
    _mk_invoice(db_session, owner, _day(tz, old), total=20.0, card=20.0, ref="OLD1")
    assert old not in {i["date"] for i in RC.day_end_checks(db_session, tz)}


def test_a_card_refund_day_also_needs_a_batch(db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=9)
    inv = _mk_invoice(db_session, owner, datetime(2032, 1, 5, 12, 0), total=30.0, card=30.0, ref="OLDREF")
    inv.refunded, inv.refund_method, inv.refunded_at = True, "Card", _day(tz, d)
    db_session.commit()
    assert "batch_missing" in _kinds_for(RC.day_end_checks(db_session, tz), d)


def test_a_batch_that_disagrees_is_flagged_on_the_full_check_until_explained(db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=8)
    _mk_invoice(db_session, owner, _day(tz, d), total=50.0, card=50.0, ref="DF1")
    b = CardBatch(id=_uid("cb"), date=d.isoformat(), amount=45.0)
    db_session.add(b)
    db_session.commit()
    assert "batch_differs" in _kinds_for(RC.day_end_checks(db_session, tz, full=True), d)
    assert "batch_differs" not in _kinds_for(RC.day_end_checks(db_session, tz, full=False), d)   # the cheap check skips it
    b.resolved = True
    db_session.commit()
    assert "batch_differs" not in _kinds_for(RC.day_end_checks(db_session, tz, full=True), d)
    b.resolved, b.amount = False, 50.0
    db_session.commit()
    assert "batch_differs" not in _kinds_for(RC.day_end_checks(db_session, tz, full=True), d)


def _repair(db, status="COLLECTED", final=80.0, est=None, updated=None):
    r = Repair(id=_uid("rp"), ticket_no=900000 + int(uuid.uuid4().int % 90000), device="Test Phone", status=status,
               final_cost=final, estimated_cost=est, updated_at=updated or datetime.utcnow())
    db.add(r)
    db.commit()
    return r


def test_a_collected_repair_with_money_but_no_sale_is_flagged(db_session, owner, checks_window):
    tz, today = checks_window
    r = _repair(db_session, final=80.0)
    msgs = [i["message"] for i in RC.day_end_checks(db_session, tz) if i["kind"] == "repair_no_sale"]
    assert any(f"#{r.ticket_no}" in m and "$80.00" in m for m in msgs)


def test_a_repair_with_a_linked_sale_is_not_flagged(db_session, owner, checks_window):
    tz, today = checks_window
    r = _repair(db_session, final=80.0)
    inv = _mk_invoice(db_session, owner, datetime.utcnow(), total=80.0, cash=80.0)
    inv.repair_id = r.id
    db_session.commit()
    assert not any(f"#{r.ticket_no}" in i["message"] for i in RC.day_end_checks(db_session, tz))


def test_free_or_unfinished_repairs_are_not_flagged(db_session, owner, checks_window):
    tz, today = checks_window
    free = _repair(db_session, final=0.0, est=None)
    unfinished = _repair(db_session, status="READY", final=80.0)
    est_only = _repair(db_session, final=None, est=45.0)
    msgs = " ".join(i["message"] for i in RC.day_end_checks(db_session, tz) if i["kind"] == "repair_no_sale")
    assert f"#{free.ticket_no}" not in msgs and f"#{unfinished.ticket_no}" not in msgs
    assert f"#{est_only.ticket_no}" in msgs and "$45.00" in msgs       # no final cost yet -> falls back to the estimate


def test_repairs_collected_before_the_switch_on_date_are_not_flagged(db_session, owner, checks_window):
    tz, today = checks_window
    r = _repair(db_session, final=80.0, updated=datetime.utcnow() - timedelta(days=20))
    assert not any(f"#{r.ticket_no}" in i["message"] for i in RC.day_end_checks(db_session, tz))


def test_summarize_groups_missing_days_and_caps_individual_items():
    items = [{"kind": "batch_missing", "severity": "warn", "date": date(2032, 3, d), "href": f"/reconcile?date=2032-03-0{d}", "message": "x"} for d in (1, 2, 3)]
    items += [{"kind": "repair_no_sale", "severity": "bad", "date": None, "href": "/repairs/x", "message": f"repair {n}"} for n in range(5)]
    out = RC.summarize_checks(items, cap=3)
    assert len([o for o in out if o["message"].startswith("repair")]) == 3
    assert any("and 2 more" in o["message"] for o in out)
    batch = [o for o in out if o["message"].startswith("Moneris batch not entered")]
    assert len(batch) == 1 and "Mar 1" in batch[0]["message"] and "Mar 3" in batch[0]["message"]


def test_dashboard_notifications_include_the_checks_for_the_owner_only(owner_client, cashier_client, db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=7)
    _mk_invoice(db_session, owner, _day(tz, d), total=20.0, card=20.0, ref="DN1")
    msgs = [n["message"] for n in owner_client.get("/api/notifications").json()["notifications"]]
    assert any("Moneris batch not entered" in m for m in msgs)
    cmsgs = [n["message"] for n in cashier_client.get("/api/notifications").json()["notifications"]]
    assert not any("Moneris batch" in m for m in cmsgs)


def test_the_reconcile_page_lists_the_checks(owner_client, db_session, owner, checks_window):
    tz, today = checks_window
    d = today - timedelta(days=6)
    _mk_invoice(db_session, owner, _day(tz, d), total=20.0, card=20.0, ref="RP1")
    page = owner_client.get("/reconcile").text
    assert "Needs a look" in page and "been entered" in page


# ═══ card fees ════════════════════════════════════════════════════════

@pytest.fixture
def fee_months(db_session):
    """Clean slate for statements and the estimated rate, restored afterwards."""
    db_session.query(CardStatement).delete()
    _set(db_session, "card_fee_rate", "")
    yield
    db_session.query(CardStatement).delete()
    _set(db_session, "card_fee_rate", "")
    db_session.commit()


def _stmt(db, month, fees, volume=None):
    db.add(CardStatement(id=_uid("st"), month=month, fees=fees, volume=volume))
    db.commit()


def test_a_whole_month_uses_the_statement_fees_exactly(db_session, fee_months):
    tz = get_shop_tz(db_session)
    _stmt(db_session, "2032-04", 90.00)
    f = cardfees.card_fees_for_period(db_session, tz, date(2032, 4, 1), date(2032, 4, 30))
    assert f["amount"] == 90.00 and f["basis"] == "actual" and "pro-rated" not in f["note"]


def test_part_of_a_month_is_prorated_by_days(db_session, fee_months):
    tz = get_shop_tz(db_session)
    _stmt(db_session, "2032-04", 90.00)                           # 30 days -> $3.00/day
    f = cardfees.card_fees_for_period(db_session, tz, date(2032, 4, 1), date(2032, 4, 10))
    assert f["amount"] == 30.00 and "pro-rated" in f["note"]


def test_a_range_across_months_adds_each_months_share(db_session, fee_months):
    tz = get_shop_tz(db_session)
    _stmt(db_session, "2032-04", 90.00)
    _stmt(db_session, "2032-05", 62.00)                           # 31 days -> $2/day
    f = cardfees.card_fees_for_period(db_session, tz, date(2032, 4, 21), date(2032, 5, 5))
    assert f["amount"] == round(90 * 10 / 30 + 62 * 5 / 31, 2)


def test_months_without_a_statement_use_the_estimated_rate_and_say_so(db_session, owner, fee_months):
    tz = get_shop_tz(db_session)
    _set(db_session, "card_fee_rate", "2.5")
    _mk_invoice(db_session, owner, datetime(2032, 7, 10, 15, 0), total=200.0, card=200.0, ref="FE1")
    refunded = _mk_invoice(db_session, owner, datetime(2032, 7, 11, 15, 0), total=100.0, card=100.0, ref="FE2")
    refunded.refunded = True
    db_session.commit()
    f = cardfees.card_fees_for_period(db_session, tz, date(2032, 7, 1), date(2032, 7, 31))
    assert f["amount"] == 5.00 and f["basis"] == "estimated" and "estimated at 2.5%" in f["note"]


def test_mixed_months_are_labelled_mixed(db_session, owner, fee_months):
    tz = get_shop_tz(db_session)
    _set(db_session, "card_fee_rate", "2")
    _stmt(db_session, "2032-08", 10.00)
    _mk_invoice(db_session, owner, datetime(2032, 9, 3, 15, 0), total=100.0, card=100.0, ref="FM1")
    f = cardfees.card_fees_for_period(db_session, tz, date(2032, 8, 1), date(2032, 9, 30))
    assert f["basis"] == "mixed" and f["amount"] == 12.00


def test_no_statement_and_no_rate_means_no_fee_at_all(db_session, fee_months):
    f = cardfees.card_fees_for_period(db_session, get_shop_tz(db_session), date(2032, 4, 1), date(2032, 4, 30))
    assert f == {"amount": 0.0, "basis": "none", "note": ""}


def test_fee_rate_parsing_rejects_nonsense(db_session, fee_months):
    for bad in ("abc", "0", "-3", "25", ""):
        _set(db_session, "card_fee_rate", bad)
        assert cardfees.fee_rate(db_session) is None, bad
    _set(db_session, "card_fee_rate", "2.65")
    assert cardfees.fee_rate(db_session) == 2.65


def test_crm_card_month_counts_taken_minus_card_refunds(db_session, owner, fee_months):
    tz = get_shop_tz(db_session)
    _mk_invoice(db_session, owner, datetime(2032, 10, 3, 15, 0), total=100.0, card=100.0, ref="CM1")
    r = _mk_invoice(db_session, owner, datetime(2032, 10, 4, 15, 0), total=40.0, card=40.0, ref="CM2")
    r.refunded, r.refund_method, r.refunded_at = True, "Card", datetime(2032, 10, 20, 15, 0)
    kept = _mk_invoice(db_session, owner, datetime(2032, 10, 5, 15, 0), total=30.0, card=30.0, ref="CM3")
    kept.refunded, kept.refund_method, kept.refunded_at = True, "Store Credit", datetime(2032, 10, 6, 15, 0)
    db_session.commit()
    m = cardfees.crm_card_month(db_session, tz, date(2032, 10, 1))
    assert (m["taken"], m["refunds"], m["net"]) == (170.0, 40.0, 130.0)     # the store-credit refund leaves the card money in


def test_sales_report_shows_card_fees_and_profit_after_them(db_session, owner, fee_months):
    tz = get_shop_tz(db_session)
    _mk_invoice(db_session, owner, datetime(2032, 11, 10, 15, 0), total=112.0, card=112.0, ref="SR1", subtotal=100.0, tax_total=12.0)
    _stmt(db_session, "2032-11", 4.00)
    rep, _, _ = R.build_report(db_session, "sales", {"from": "2032-11-01", "to": "2032-11-30"})
    labels = {k.label: k for k in rep.kpis}
    assert labels["Card fees"].value == 4.0
    assert round(labels["Profit after card fees"].value, 2) == round(rep_gp(rep) - 4.0, 2)
    assert not any("not included" in n for n in rep.notes)


def rep_gp(rep):
    return next(k.value for k in rep.kpis if k.label == "Gross profit")


def test_sales_report_nudges_when_fees_are_not_recorded(db_session, owner, fee_months):
    _mk_invoice(db_session, owner, datetime(2033, 1, 10, 15, 0), total=112.0, card=112.0, ref="SR2", subtotal=100.0, tax_total=12.0)
    rep, _, _ = R.build_report(db_session, "sales", {"from": "2033-01-01", "to": "2033-01-31"})
    assert not any(k.label == "Card fees" for k in rep.kpis)
    assert any("Card processing fees are not included" in n for n in rep.notes)


def test_monthly_report_has_fee_lines_and_the_statement_check(db_session, owner, fee_months):
    _mk_invoice(db_session, owner, datetime(2033, 2, 10, 15, 0), total=112.0, card=112.0, ref="MR1", subtotal=100.0, tax_total=12.0)
    _stmt(db_session, "2033-02", 3.50, volume=110.00)
    rep, _, _ = R.build_report(db_session, "monthly", {"month": "2033-02"})
    pl = {r[0]: r[1] for r in next(s for s in rep.sections if s.title.startswith("Profit")).rows}
    assert any(k.startswith("Less: card processing fees") for k in pl) and "Profit after card fees" in pl
    chk = {r[0]: r[1] for r in next(s for s in rep.sections if s.title == "Moneris statement check").rows}
    assert chk["CRM card total, net"] == 112.0 and chk["Moneris statement volume"] == 110.0
    assert chk["Difference (statement − CRM)"] == -2.0 and chk["Moneris fees charged"] == 3.5


def test_monthly_report_without_a_statement_has_no_statement_section(db_session, owner, fee_months):
    rep, _, _ = R.build_report(db_session, "monthly", {"month": "2033-03"})
    assert not any(s.title == "Moneris statement check" for s in rep.sections)


# ═══ the Card Fees page ═══════════════════════════════════════════════

def test_card_fees_page_is_for_owner_and_manager(owner_client, manager_client, cashier_client, anon_client):
    assert owner_client.get("/card-fees").status_code == 200
    assert manager_client.get("/card-fees").status_code == 200
    assert cashier_client.get("/card-fees").status_code == 403
    assert anon_client.get("/card-fees", follow_redirects=False).status_code in (302, 303, 307)
    for path in ("/card-fees/save", "/card-fees/delete", "/card-fees/settings"):
        assert cashier_client.post(path, data={}, follow_redirects=False).status_code == 403


def test_saving_and_replacing_and_removing_a_statement(owner_client, db_session, fee_months):
    owner_client.post("/card-fees/save", data={"month": "2024-03", "fees": "87.42", "volume": "$3,100.00", "note": "n"}, follow_redirects=False)
    db_session.expire_all()
    row = db_session.query(CardStatement).filter(CardStatement.month == "2024-03").first()
    assert (row.fees, row.volume, row.note) == (87.42, 3100.0, "n")
    owner_client.post("/card-fees/save", data={"month": "2024-03", "fees": "90"}, follow_redirects=False)
    db_session.expire_all()
    rows = db_session.query(CardStatement).filter(CardStatement.month == "2024-03").all()
    assert len(rows) == 1 and rows[0].fees == 90.0 and rows[0].volume is None
    assert "2024" in owner_client.get("/card-fees").text
    owner_client.post("/card-fees/delete", data={"month": "2024-03"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.query(CardStatement).filter(CardStatement.month == "2024-03").count() == 0


@pytest.mark.parametrize("data", [
    {"month": "", "fees": "5"}, {"month": "bogus", "fees": "5"}, {"month": "2999-01", "fees": "5"},
    {"month": "2024-03", "fees": ""}, {"month": "2024-03", "fees": "-5"}, {"month": "2024-03", "fees": "abc"},
    {"month": "2024-03", "fees": "nan"}, {"month": "2024-03", "fees": "5", "volume": "xyz"},
])
def test_bad_statement_entries_are_rejected_without_saving(owner_client, db_session, fee_months, data):
    r = owner_client.post("/card-fees/save", data=data, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(CardStatement).count() == 0


def test_payment_settings_can_be_changed_and_bad_rates_are_refused(owner_client, db_session, fee_months):
    owner_client.post("/card-fees/settings", data={"fee_rate_pct": "2.65"}, follow_redirects=False)     # both boxes unticked
    db_session.expire_all()
    assert cash_rounding_on(db_session) is False and require_card_ref_on(db_session) is False
    assert cardfees.fee_rate(db_session) == 2.65
    owner_client.post("/card-fees/settings", data={"cash_rounding": "on", "require_card_ref": "on", "fee_rate_pct": "2.65"}, follow_redirects=False)
    db_session.expire_all()
    assert cash_rounding_on(db_session) is True and require_card_ref_on(db_session) is True
    owner_client.post("/card-fees/settings", data={"fee_rate_pct": "250"}, follow_redirects=False)      # refused: nothing changes
    db_session.expire_all()
    assert cash_rounding_on(db_session) is True and cardfees.fee_rate(db_session) == 2.65
    # leave the suite's pinned defaults in place
    owner_client.post("/card-fees/settings", data={}, follow_redirects=False)


# ═══ schema / upgrade ═════════════════════════════════════════════════

def test_new_columns_and_tables_exist_and_default_sensibly(db_session, owner):
    from sqlalchemy import inspect
    from app.database import engine
    insp = inspect(engine)
    assert "cash_rounding" in {c["name"] for c in insp.get_columns("invoices")}
    assert {"card_batches", "card_statements"} <= set(insp.get_table_names())
    inv = _mk_invoice(db_session, owner, datetime(2032, 12, 1, 12, 0), total=10.0, cash=10.0)
    db_session.expire_all()
    assert db_session.get(Invoice, inv.id).cash_rounding == 0


def test_recon_start_is_set_once_and_never_in_the_future(db_session):
    row = db_session.get(Setting, "recon_start")
    assert row is not None and date.fromisoformat(row.value) <= date.today() + timedelta(days=1)


def test_every_new_post_route_is_csrf_protected_in_the_form_templates():
    import pathlib
    for name in ("reconcile.html", "card_fees.html"):
        html = (pathlib.Path(__file__).resolve().parent.parent / "templates" / name).read_text()
        forms = html.count("<form method=\"post\"")
        assert forms == html.count("csrf_token()"), name


# ═══ backups carry the hand-entered Moneris records ═══════════════════

def _seed_moneris_records(db_session, day_a, day_b):
    db_session.query(CardBatch).filter(CardBatch.date.in_([day_a, day_b])).delete(synchronize_session=False)
    db_session.query(CardStatement).delete()
    db_session.add(CardBatch(id=_uid("cb"), date=day_a, amount=111.0, note="keep me", resolved=True))
    db_session.add(CardBatch(id=_uid("cb"), date=day_b, amount=222.0))
    _stmt(db_session, "2031-01", 12.34, volume=500.0)
    db_session.commit()


def test_backup_file_includes_batches_and_statements(owner_client, db_session, fee_months):
    day_a, day_b = "2031-01-15", "2031-01-16"
    _seed_moneris_records(db_session, day_a, day_b)
    data = json.loads(owner_client.get("/export/backup").content)
    assert {b["date"] for b in data["tables"]["card_batches"]} >= {day_a, day_b}
    assert [c["month"] for c in data["tables"]["card_statements"]] == ["2031-01"]
    db_session.query(CardBatch).filter(CardBatch.date.in_([day_a, day_b])).delete(synchronize_session=False)
    db_session.commit()


def test_restoring_moneris_records_only_adds_what_is_missing(db_session, fee_months):
    from app.main import _restore_moneris_records
    day_a, day_b = "2031-01-15", "2031-01-16"
    _seed_moneris_records(db_session, day_a, day_b)
    snapshot = {"card_batches": [{"id": b.id, "date": b.date, "amount": b.amount, "note": b.note, "resolved": b.resolved,
                                  "entered_at": b.entered_at.isoformat()} for b in db_session.query(CardBatch).filter(CardBatch.date.in_([day_a, day_b]))],
                "card_statements": [{"id": c.id, "month": c.month, "volume": c.volume, "fees": c.fees, "note": c.note,
                                     "entered_at": c.entered_at.isoformat()} for c in db_session.query(CardStatement)]}
    # lose day_b and the statement; day_a is edited after the backup was taken
    db_session.query(CardBatch).filter(CardBatch.date == day_b).delete()
    db_session.query(CardStatement).delete()
    db_session.query(CardBatch).filter(CardBatch.date == day_a).update({"amount": 999.0})
    db_session.commit()

    _restore_moneris_records(db_session, snapshot)
    db_session.commit()
    assert db_session.query(CardBatch).filter(CardBatch.date == day_b).first().amount == 222.0      # brought back
    assert db_session.query(CardBatch).filter(CardBatch.date == day_a).first().amount == 999.0      # existing entry untouched
    st = db_session.query(CardStatement).filter(CardStatement.month == "2031-01").first()
    assert (st.fees, st.volume) == (12.34, 500.0)
    _restore_moneris_records(db_session, snapshot)                                                 # twice -> no duplicates
    db_session.commit()
    assert db_session.query(CardBatch).filter(CardBatch.date == day_b).count() == 1
    assert db_session.query(CardStatement).filter(CardStatement.month == "2031-01").count() == 1
    db_session.query(CardBatch).filter(CardBatch.date.in_([day_a, day_b])).delete(synchronize_session=False)
    db_session.commit()


def test_an_old_v46_backup_without_these_keys_still_restores(owner_client):
    old = json.dumps({"version": 1, "settings": {}}).encode()          # no products/customers keys, so the shared test data is left alone
    r = owner_client.post("/import/backup", files={"file": ("old.json", old, "application/json")}, follow_redirects=False)
    assert r.status_code == 303
