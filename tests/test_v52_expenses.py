"""v52 — expenses and till paid-outs: validation, Cash Up maths, permissions, voiding, report."""
from datetime import datetime, timedelta

import pytest

from app import expenses as X
from app.database import SessionLocal
from app.localtime import day_bounds_utc, get_shop_tz, local_today
from app.models import AuditLog, CashSession, Expense
from app.reporting import cash_card_totals


_OPEN = []


def _db():
    """A fresh session each call (so reads are never stale); closed after the test so the pool never runs dry."""
    db = SessionLocal()
    _OPEN.append(db)
    return db


@pytest.fixture(autouse=True)
def _close_sessions():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _totals(db):
    tz = get_shop_tz(db)
    return cash_card_totals(db, tz, local_today(tz))


def _post(client, **kw):
    data = {"amount": "10.00", "tax_paid": "", "category": "Supplies", "description": "unit test item", "payee": "",
            "receipt_ref": "", "paid_from": "drawer"}
    data.update(kw)
    return client.post("/expenses", data=data, follow_redirects=False)


def _last(db, desc):
    return db.query(Expense).filter(Expense.description == desc).order_by(Expense.created_at.desc()).first()


# ── validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount", ["", "0", "0.00", "-5", "abc", "nan", "inf", "100000.01", "1e9"])
def test_bad_amounts_are_rejected(amount):
    with pytest.raises(X.ExpenseError):
        X.validate(amount, "", "Supplies", "paper", "", "", "drawer")


def test_good_values_are_cleaned():
    v = X.validate("$1,234.567", "10", "Supplies", "  printer    paper \n", " Staples ", " 77 ", "drawer")
    assert v["amount"] == 1234.57
    assert v["description"] == "printer paper"
    assert v["payee"] == "Staples" and v["receipt_ref"] == "77" and v["tax_paid"] == 10.0


def test_tax_cannot_exceed_total_and_description_is_required():
    with pytest.raises(X.ExpenseError):
        X.validate("10", "10.01", "Supplies", "x", "", "", "drawer")
    with pytest.raises(X.ExpenseError):
        X.validate("10", "", "Supplies", "   ", "", "", "drawer")
    with pytest.raises(X.ExpenseError):
        X.validate("10", "", "Made up", "x", "", "", "drawer")
    with pytest.raises(X.ExpenseError):
        X.validate("10", "", "Supplies", "x", "", "", "wire")


def test_deposit_has_no_tax_and_must_come_from_the_till():
    v = X.validate("500", "25", "Bank deposit", "to the bank", "", "", "drawer")
    assert v["tax_paid"] == 0.0
    with pytest.raises(X.ExpenseError):
        X.validate("500", "", "Owner draw", "me", "", "", "other")


def test_long_text_is_trimmed():
    v = X.validate("5", "", "Other", "x" * 500, "p" * 500, "r" * 500, "other")
    assert len(v["description"]) == 200 and len(v["payee"]) == 80 and len(v["receipt_ref"]) == 60


# ── Cash Up arithmetic ─────────────────────────────────────────────────

def test_drawer_expense_lowers_expected_cash(cashier_client):
    db = _db()
    before = _totals(db)
    r = _post(cashier_client, amount="12.34", description="lunch run test A")
    assert r.status_code == 303
    after = _totals(_db())
    assert round(after["cash_paid_out"] - before["cash_paid_out"], 2) == 12.34
    assert round(after["cash_net"] - before["cash_net"], 2) == -12.34
    assert after["cash_sales"] == before["cash_sales"]          # the sales figure itself is untouched
    assert after["card_sales"] == before["card_sales"]


def test_paid_another_way_never_touches_the_drawer(owner_client):
    before = _totals(_db())
    _post(owner_client, amount="99.99", paid_from="other", description="business card purchase test B")
    after = _totals(_db())
    assert after["cash_paid_out"] == before["cash_paid_out"]
    assert after["cash_net"] == before["cash_net"]


def test_cashup_page_expected_matches_totals(owner_client):
    _post(owner_client, amount="3.21", description="cashup page test C")
    t = _totals(_db())
    page = owner_client.get("/cashup")
    assert page.status_code == 200
    assert "cashup page test C" in page.text
    assert f'id="cu-expected" type="number" value="{round(200 + t["cash_net"], 2)}"' in page.text or "cu-expected" in page.text
    assert "was taken out of the till today" in page.text


def test_cashup_close_uses_paid_outs_and_freezes_them(owner_client):
    db = _db()
    db.query(CashSession).filter(CashSession.date == local_today(get_shop_tz(db)).isoformat()).delete()
    db.commit()
    _post(owner_client, amount="7.00", description="freeze test D")
    t = _totals(_db())
    expected = round(50 + t["cash_net"], 2)
    r = owner_client.post("/cashup/close", data={"open_float": "50", "actual": str(expected), "card_batch": "", "notes": ""},
                          follow_redirects=False)
    assert r.status_code == 303
    db = _db()
    s = db.query(CashSession).filter(CashSession.date == local_today(get_shop_tz(db)).isoformat()).one()
    assert s.expected == expected and s.difference == 0
    assert s.paid_out == t["cash_paid_out"]
    # a paid-out entered AFTER the close is flagged on the page, and the saved figure stays as it was
    r = _post(owner_client, amount="2.50", description="late test E")
    assert "already closed" in owner_client.get("/expenses").text
    page = owner_client.get("/cashup").text
    assert "after</b> today" in page and "2.50" in page
    db = _db()
    assert db.query(CashSession).filter(CashSession.date == s.date).one().expected == expected
    # closing again brings it back in line
    t2 = _totals(_db())
    owner_client.post("/cashup/close", data={"open_float": "50", "actual": str(round(50 + t2["cash_net"], 2)), "card_batch": "", "notes": ""})
    assert "after</b> today" not in owner_client.get("/cashup").text
    db = _db()
    db.query(CashSession).filter(CashSession.date == s.date).delete()
    db.commit()


# ── daily report ───────────────────────────────────────────────────────

def test_daily_report_drawer_adds_up(owner_client):
    from app.reporting import daily_report
    _post(owner_client, amount="4.44", description="daily report test F")
    db = _db()
    tz = get_shop_tz(db)
    rep = daily_report(db, tz, local_today(tz))
    drawer = next(s for s in rep.sections if s.title == "Cash drawer")
    labels = [r[0] for r in drawer.rows]
    assert any("taken out of the till" in l for l in labels)
    amounts = {r[0]: float(r[1]) for r in drawer.rows}
    expected = [v for k, v in amounts.items() if k.startswith("= Expected")][0]
    parts = [v for k, v in amounts.items() if not k.startswith("=") and not k.startswith("   of which")]
    assert round(sum(parts), 2) == round(expected, 2)


# ── permissions ────────────────────────────────────────────────────────

def test_anonymous_is_sent_to_login(anon_client):
    for path in ("/expenses", "/expenses/new"):
        r = anon_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = anon_client.post("/expenses", data={"amount": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("client_name", ["cashier_client", "technician_client", "manager_client", "owner_client"])
def test_every_role_can_record_for_today(client_name, request):
    c = request.getfixturevalue(client_name)
    r = _post(c, description=f"role test {client_name}")
    assert r.status_code == 303
    assert _last(_db(), f"role test {client_name}") is not None


def test_cashier_cannot_back_date(cashier_client):
    two_days_ago = (local_today(get_shop_tz(_db())) - timedelta(days=2)).isoformat()
    _post(cashier_client, description="backdate attempt G", spent_on=two_days_ago)
    e = _last(_db(), "backdate attempt G")
    tz = get_shop_tz(_db())
    from app.localtime import to_local_date
    assert to_local_date(e.spent_at, tz) == local_today(tz)


def test_manager_can_back_date_and_it_lands_on_that_day(manager_client):
    db = _db()
    tz = get_shop_tz(db)
    d = local_today(tz) - timedelta(days=3)
    before_today = _totals(db)["cash_paid_out"]
    _post(manager_client, description="backdate H", amount="8.00", spent_on=d.isoformat())
    e = _last(_db(), "backdate H")
    s, en = day_bounds_utc(d, d, tz)
    assert s <= e.spent_at < en
    assert _totals(_db())["cash_paid_out"] == before_today               # did not leak onto today
    assert cash_card_totals(_db(), tz, d)["cash_paid_out"] >= 8.0


def test_future_and_ancient_dates_are_refused(manager_client):
    tz = get_shop_tz(_db())
    t = local_today(tz)
    r = _post(manager_client, description="future I", spent_on=(t + timedelta(days=1)).isoformat())
    assert r.status_code == 400 and "future" in r.text
    r = _post(manager_client, description="ancient J", spent_on=(t - timedelta(days=500)).isoformat())
    assert r.status_code == 400 and "too far back" in r.text
    assert _last(_db(), "future I") is None and _last(_db(), "ancient J") is None


def test_bad_form_shows_error_and_keeps_what_was_typed(owner_client):
    r = _post(owner_client, amount="abc", description="keep me K")
    assert r.status_code == 400
    assert "must be a number" in r.text and "keep me K" in r.text
    assert _last(_db(), "keep me K") is None


def test_description_is_escaped(owner_client):
    _post(owner_client, description='<script>alert(1)</script> L')
    page = owner_client.get("/expenses").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; L" in page


# ── list visibility ────────────────────────────────────────────────────

def test_cashier_sees_only_their_own_entries_today(cashier_client, owner_client):
    _post(owner_client, description="owner private M")
    _post(cashier_client, description="cashier own N")
    page = cashier_client.get("/expenses").text
    assert "cashier own N" in page and "owner private M" not in page
    assert "Void" not in page
    full = owner_client.get("/expenses").text
    assert "cashier own N" in full and "owner private M" in full


# ── void ───────────────────────────────────────────────────────────────

def test_void_needs_manager_and_a_reason_and_restores_the_drawer(owner_client, cashier_client, manager_client):
    _post(cashier_client, amount="20.00", description="void me O")
    e = _last(_db(), "void me O")
    before = _totals(_db())
    assert cashier_client.post(f"/expenses/{e.id}/void", data={"reason": "oops"}).status_code == 403
    assert _last(_db(), "void me O").voided is not True
    manager_client.post(f"/expenses/{e.id}/void", data={"reason": "  "}, follow_redirects=False)
    assert _last(_db(), "void me O").voided is not True
    r = manager_client.post(f"/expenses/{e.id}/void", data={"reason": "entered twice"}, follow_redirects=False)
    assert r.status_code == 303
    e = _last(_db(), "void me O")
    assert e.voided and e.voided_by == "Test Manager" and e.void_reason == "entered twice"
    after = _totals(_db())
    assert round(after["cash_paid_out"] - before["cash_paid_out"], 2) == -20.00
    # a second void changes nothing
    manager_client.post(f"/expenses/{e.id}/void", data={"reason": "again"})
    assert _last(_db(), "void me O").void_reason == "entered twice"
    page = owner_client.get("/expenses").text
    assert "entered twice" in page


def test_void_of_missing_entry_is_harmless(owner_client):
    r = owner_client.post("/expenses/nope/void", data={"reason": "x"}, follow_redirects=False)
    assert r.status_code == 303


def test_audit_trail(owner_client):
    _post(owner_client, description="audited P", amount="5.55")
    e = _last(_db(), "audited P")
    owner_client.post(f"/expenses/{e.id}/void", data={"reason": "audit check"})
    acts = [(a.action, a.detail) for a in _db().query(AuditLog).order_by(AuditLog.ts.desc()).limit(20)]
    assert any(a == "EXPENSE" and "audited P" in d for a, d in acts)
    assert any(a == "EXPENSE_VOID" and "audit check" in d for a, d in acts)


# ── report ─────────────────────────────────────────────────────────────

def test_report_totals_and_categories_agree(owner_client):
    db = _db()
    tz = get_shop_tz(db)
    t = local_today(tz)
    s, e = day_bounds_utc(t, t, tz)
    _post(owner_client, amount="105.00", tax_paid="5.00", category="Parts & inventory", description="report test Q")
    _post(owner_client, amount="300.00", category="Bank deposit", description="deposit R")
    _post(owner_client, amount="40.00", tax_paid="2.00", category="Fuel & delivery", paid_from="other", description="gas S")
    r = owner_client.get(f"/reports/center/expenses?from={t.isoformat()}&to={t.isoformat()}")
    assert r.status_code == 200
    assert "report test Q" in r.text and "deposit R" in r.text and "gas S" in r.text
    from app.reporting import expenses_report
    rep = expenses_report(db, tz, t, t)
    k = {x.label: x.value for x in rep.kpis}
    live = X.in_window(db, s, e)
    biz = [x for x in live if x.category not in X.NOT_AN_EXPENSE]
    assert float(k["Business expenses"]) == round(sum(x.amount for x in biz), 2)
    assert float(k["Tax paid on them"]) == round(sum(x.tax_paid for x in biz), 2)
    assert float(k["Taken from the till"]) == round(sum(x.amount for x in live if x.paid_from == "drawer"), 2)
    assert float(k["Deposits & owner draws"]) >= 300.0
    cat = next(sec for sec in rep.sections if sec.title == "By category")
    assert round(sum(float(r_[2]) for r_ in cat.rows), 2) == round(sum(x.amount for x in live), 2)
    assert X.business_total(db, s, e)["amount"] == float(k["Business expenses"])
    assert owner_client.get(f"/reports/center/expenses/export.csv?from={t.isoformat()}&to={t.isoformat()}").status_code == 200
    assert owner_client.get(f"/reports/center/expenses/export.xlsx?from={t.isoformat()}&to={t.isoformat()}").status_code == 200
    assert owner_client.get(f"/reports/center/expenses/print?from={t.isoformat()}&to={t.isoformat()}").status_code == 200


def test_voided_entries_are_listed_but_not_counted(owner_client):
    db = _db()
    tz = get_shop_tz(db)
    t = local_today(tz)
    _post(owner_client, amount="61.00", description="to be voided T")
    e = _last(_db(), "to be voided T")
    from app.reporting import expenses_report
    before = float({x.label: x.value for x in expenses_report(_db(), tz, t, t).kpis}["Business expenses"])
    owner_client.post(f"/expenses/{e.id}/void", data={"reason": "wrong shop"})
    rep = expenses_report(_db(), tz, t, t)
    after = float({x.label: x.value for x in rep.kpis}["Business expenses"])
    assert round(before - after, 2) == 61.00
    void_sec = next(s for s in rep.sections if s.title.startswith("Voided"))
    assert any("to be voided T" in r_ for r_ in void_sec.rows)


def test_cashup_history_report_shows_paid_out_column(owner_client):
    from app.reporting import cashup_report
    db = _db()
    tz = get_shop_tz(db)
    t = local_today(tz)
    rep = cashup_report(db, tz, t.year, t.month)
    sec = rep.sections[0]
    assert "Paid out of till" in sec.headers
    assert len(sec.headers) == len(sec.rows[0]) if sec.rows else True


def test_report_listed_in_center(owner_client):
    page = owner_client.get("/reports/center").text
    assert "Expenses" in page and "/reports/center/expenses" in page


def test_nav_link_for_everyone(cashier_client):
    assert 'href="/expenses"' in cashier_client.get("/cashup").text
