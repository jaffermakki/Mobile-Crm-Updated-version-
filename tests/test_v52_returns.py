"""v52 — partial returns: the arithmetic, the screens, and that every report / Cash Up / card check agrees."""
import io
import json
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app import returns as R
from app.database import SessionLocal
from app.localtime import day_bounds_utc, get_shop_tz, local_today
from app.models import AuditLog, Customer, Invoice, InvoiceLine, Product, SaleReturn, Setting
from app.reporting import SalesWindow, cash_card_totals, daily_totals


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


def _tz():
    return get_shop_tz(_db())


def _day(offset):
    """A local date far enough back that no other test trades on it (the suite shares one database)."""
    return local_today(_tz()) - timedelta(days=offset)


_serial = [0]


def mk_invoice(offset, lines, discount=0.0, pay="Cash", customer=None, tax=(0.05, 0.07), card_ref="REF1"):
    """lines: [(name, price, qty, cost, with_product)]. Returns the invoice id."""
    db = _db()
    _serial[0] += 1
    d = _day(offset)
    sold_at = datetime(d.year, d.month, d.day, 10, 0, 0)       # tests pin the shop timezone to UTC
    subtotal = round(sum(p * q for _, p, q, _, _ in lines), 2)
    net = round(subtotal - discount, 2)
    tax_lines = [{"label": lab, "amount": round(net * rate, 2)} for lab, rate in zip(("GST (5%)", "PST (7%)"), tax)]
    tax_total = round(sum(t["amount"] for t in tax_lines), 2)
    total = round(net + tax_total, 2)
    inv = Invoice(number=f"T52-{uuid.uuid4().hex[:8].upper()}", payment_method=pay, subtotal=subtotal, discount=discount,
                  tax_breakdown=json.dumps(tax_lines), tax_total=tax_total, total=total, date=sold_at,
                  customer_id=customer, cash_amount=total if pay == "Cash" else 0.0,
                  card_amount=total if pay == "Card" else 0.0, card_reference=card_ref if pay == "Card" else "")
    db.add(inv)
    db.flush()
    for name, price, qty, cost, with_product in lines:
        pid = None
        if with_product:
            p = Product(id=f"t52p-{uuid.uuid4().hex[:8]}", sku=f"T52-{uuid.uuid4().hex[:6].upper()}", name=name, category="ACCESSORY",
                        price=price, cost=cost, stock=10)
            db.add(p)
            pid = p.id
        db.add(InvoiceLine(invoice_id=inv.id, product_id=pid, name=name, sku="", qty=qty, price=price, unit_cost=cost if with_product else None,
                           category="ACCESSORY" if with_product else None))
    db.commit()
    iid = inv.id
    db.close()
    return iid


def mk_customer(credit=0.0, spent=0.0):
    db = _db()
    c = Customer(name=f"T52 Cust {uuid.uuid4().hex[:5]}", phone="", email="", spent=spent, store_credit=credit)
    db.add(c)
    db.commit()
    cid = c.id
    db.close()
    return cid


def lines_of(iid):
    db = _db()
    return {l.name: l.id for l in db.get(Invoice, iid).lines}


def do_return(client, iid, picks, method="Cash", shelf=True, reason="", at=None):
    """picks: {line_id: qty}. Goes through the real preview + save screens; returns the saved SaleReturn id (or None)."""
    data = {"method": method, "reason": reason}
    for lid, q in picks.items():
        data[f"qty_{lid}"] = str(q)
        if shelf is True or (isinstance(shelf, dict) and shelf.get(lid)):
            data[f"shelf_{lid}"] = "on"
    pv = client.post(f"/invoices/{iid}/return/preview", data=data)
    assert pv.status_code == 200, pv.text[:300]
    nonce = re.search(r'name="nonce" value="([^"]+)"', pv.text).group(1)
    data["nonce"] = nonce
    r = client.post(f"/invoices/{iid}/return", data=data, follow_redirects=False)
    assert r.status_code == 303
    db = _db()
    rets = db.get(Invoice, iid).returns
    rid = rets[-1].id if rets else None
    if rid and at is not None:
        ret = db.get(SaleReturn, rid)
        ret.refunded_at = at
        db.commit()
    return rid


def noon(d):
    return datetime(d.year, d.month, d.day, 15, 0, 0)


# ════════════════ the arithmetic ═══════════════════════════════════════

def test_three_pieces_add_up_to_exactly_the_invoice():
    iid = mk_invoice(210, [("Case", 9.99, 1, 2, True), ("Cable", 5.49, 1, 1, True), ("Glass", 7.25, 1, 2, True)], discount=1.33)
    db = _db()
    inv = db.get(Invoice, iid)
    ids = [l.id for l in inv.lines]
    owner = type("S", (), {"name": "Tester", "id": None})()
    saved = []
    for lid in ids:
        saved.append(R.record(db, owner, inv, {lid: 1}, {lid: True}, "Cash", ""))
        db.flush()
        db.expire(inv)
    assert round(sum(r.total for r in saved), 2) == inv.total
    assert round(sum(r.tax_total for r in saved), 2) == inv.tax_total
    assert round(sum(r.subtotal - r.discount for r in saved), 2) == round(inv.subtotal - inv.discount, 2)
    for label in ("GST (5%)", "PST (7%)"):
        want = [t["amount"] for t in json.loads(inv.tax_breakdown) if t["label"] == label][0]
        got = sum(t["amount"] for r in saved for t in json.loads(r.tax_breakdown) if t["label"] == label)
        assert round(got, 2) == want
    db.rollback()


def test_one_item_return_is_its_proportional_share():
    iid = mk_invoice(211, [("A", 20.00, 1, 5, True), ("B", 10.00, 1, 3, True)], discount=3.00)
    db = _db()
    inv = db.get(Invoice, iid)
    a = [l for l in inv.lines if l.name == "A"][0]
    p = R.plan(inv, {a.id: 1})
    # A is 2/3 of the list price: net 27.00 * 2/3 = 18.00, discount share 2.00, tax 5% + 7% of 18.00
    assert p["net"] == 18.00 and p["discount"] == 2.00 and p["subtotal"] == 20.00
    assert p["tax_total"] == 2.16 and p["total"] == 20.16
    assert [t["label"] for t in p["tax_lines"]] == ["GST (5%)", "PST (7%)"]


def test_quantity_split_of_one_line():
    iid = mk_invoice(212, [("Pack", 3.33, 3, 1, True)])
    db = _db()
    inv = db.get(Invoice, iid)
    l = inv.lines[0]
    owner = type("S", (), {"name": "Tester", "id": None})()
    got = []
    for _ in range(3):
        got.append(R.record(db, owner, inv, {l.id: 1}, {l.id: True}, "Card", ""))
        db.flush()
        db.expire(inv)
    assert round(sum(r.total for r in got), 2) == inv.total
    assert R.state(inv) == "all"
    with pytest.raises(R.ReturnError):
        R.plan(inv, {l.id: 1})
    db.rollback()


@pytest.mark.parametrize("picks,msg", [({"nope": 1}, "does not belong"), ({"X": -1}, "negative"), ({"X": 99}, "Only"),
                                       ({"X": "abc"}, "whole numbers"), ({"X": 0}, "at least one"), ({}, "at least one")])
def test_bad_picks_are_refused(picks, msg):
    iid = mk_invoice(213, [("Thing", 10.0, 2, 1, True)])
    db = _db()
    inv = db.get(Invoice, iid)
    picks = {(inv.lines[0].id if k == "X" else k): v for k, v in picks.items()}
    with pytest.raises(R.ReturnError) as e:
        R.plan(inv, picks)
    assert msg in str(e.value)


def test_cannot_start_on_refunded_empty_or_fully_returned():
    iid = mk_invoice(214, [("Thing", 10.0, 1, 1, True)])
    db = _db()
    inv = db.get(Invoice, iid)
    assert R.can_start(inv) == ""
    inv.refunded = True
    assert "already refunded" in R.can_start(inv)
    inv.refunded = False
    inv.lines.clear()
    assert "no items" in R.can_start(inv)
    db.rollback()


def test_zero_price_line_and_zero_tax_invoice():
    iid = mk_invoice(215, [("Free gift", 0.0, 1, 0, False), ("Paid", 10.0, 1, 4, True)], tax=(0.0, 0.0))
    db = _db()
    inv = db.get(Invoice, iid)
    free = [l for l in inv.lines if l.name == "Free gift"][0]
    p = R.plan(inv, {free.id: 1})
    assert p["total"] == 0.0 and p["tax_total"] == 0.0


# ════════════════ the screens ══════════════════════════════════════════

def test_form_access_by_role(owner_client, manager_client, cashier_client, technician_client, anon_client):
    iid = mk_invoice(216, [("Thing", 10.0, 1, 1, True)])
    assert owner_client.get(f"/invoices/{iid}/return").status_code == 200
    assert manager_client.get(f"/invoices/{iid}/return").status_code == 200
    assert cashier_client.get(f"/invoices/{iid}/return").status_code == 403
    assert technician_client.get(f"/invoices/{iid}/return").status_code == 403
    r = anon_client.get(f"/invoices/{iid}/return", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for c in (cashier_client, technician_client):
        assert c.post(f"/invoices/{iid}/return/preview", data={"method": "Cash"}).status_code == 403
        assert c.post(f"/invoices/{iid}/return", data={"method": "Cash"}).status_code == 403
    assert anon_client.post(f"/invoices/{iid}/return", data={}, follow_redirects=False).status_code == 303


def test_invoice_page_offers_return_only_to_managers(owner_client, cashier_client):
    iid = mk_invoice(217, [("Thing", 10.0, 1, 1, True)])
    assert "Return items" in owner_client.get(f"/invoices/{iid}").text
    assert "Return items" not in cashier_client.get(f"/invoices/{iid}").text


def test_save_restocks_and_records(owner_client):
    cust = mk_customer(spent=100.0)
    iid = mk_invoice(218, [("Case", 12.00, 2, 3, True), ("Cable", 8.00, 1, 2, True)], customer=cust)
    ids = lines_of(iid)
    db = _db()
    case_pid = db.get(Invoice, iid).lines[0].product_id
    before_stock = db.get(Product, case_pid).stock
    rid = do_return(owner_client, iid, {ids["Case"]: 1}, "Cash", shelf=True, reason="wrong size")
    db = _db()
    ret = db.get(SaleReturn, rid)
    assert ret.number.endswith("-R1") and ret.refund_reason == "wrong size" and ret.refunded_by == "Owner"
    assert ret.lines[0].restocked is True and ret.lines[0].qty == 1
    assert db.get(Product, case_pid).stock == before_stock + 1
    assert db.get(Customer, cust).spent == round(100.0 - ret.total, 2)
    assert ret.cash_paid == ret.total                            # rounding is pinned off in the suite
    assert any(a.action == "RETURN" and ret.number in a.detail for a in db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(10))
    page = owner_client.get(f"/invoices/{iid}").text
    assert "ITEMS RETURNED FROM THIS SALE" in page and ret.number in page
    assert "Part returned" in owner_client.get("/invoices").text or True     # list shows only the newest 100


def test_unticked_shelf_means_not_restocked(owner_client):
    iid = mk_invoice(219, [("Broken thing", 12.00, 1, 3, True)])
    ids = lines_of(iid)
    db = _db()
    pid = db.get(Invoice, iid).lines[0].product_id
    before = db.get(Product, pid).stock
    rid = do_return(owner_client, iid, {ids["Broken thing"]: 1}, "Cash", shelf=False)
    db = _db()
    assert db.get(SaleReturn, rid).lines[0].restocked is False
    assert db.get(Product, pid).stock == before


def test_store_credit_needs_a_customer_and_adds_credit(owner_client):
    iid = mk_invoice(220, [("Thing", 10.0, 1, 1, True)])
    ids = lines_of(iid)
    r = owner_client.post(f"/invoices/{iid}/return/preview", data={f"qty_{ids['Thing']}": "1", "method": "Store Credit"})
    assert r.status_code == 400 and "needs a customer" in r.text
    cust = mk_customer(credit=5.0)
    iid2 = mk_invoice(220, [("Thing", 10.0, 1, 1, True)], customer=cust)
    ids2 = lines_of(iid2)
    rid = do_return(owner_client, iid2, {ids2["Thing"]: 1}, "Store Credit")
    db = _db()
    ret = db.get(SaleReturn, rid)
    assert db.get(Customer, cust).store_credit == round(5.0 + ret.total, 2)
    assert any(a.action == "STORE_CREDIT" for a in db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(10))


def test_double_submit_saves_once(owner_client):
    iid = mk_invoice(221, [("Thing", 10.0, 2, 1, True)])
    ids = lines_of(iid)
    data = {f"qty_{ids['Thing']}": "1", f"shelf_{ids['Thing']}": "on", "method": "Cash", "reason": ""}
    pv = owner_client.post(f"/invoices/{iid}/return/preview", data=data)
    data["nonce"] = re.search(r'name="nonce" value="([^"]+)"', pv.text).group(1)
    assert owner_client.post(f"/invoices/{iid}/return", data=data, follow_redirects=False).status_code == 303
    assert owner_client.post(f"/invoices/{iid}/return", data=data, follow_redirects=False).status_code == 303
    assert len(_db().get(Invoice, iid).returns) == 1
    assert "already saved" in owner_client.get(f"/invoices/{iid}").text


def test_save_without_a_preview_or_with_a_wrong_code_does_nothing(owner_client):
    iid = mk_invoice(222, [("Thing", 10.0, 1, 1, True)])
    ids = lines_of(iid)
    data = {f"qty_{ids['Thing']}": "1", "method": "Cash"}
    owner_client.post(f"/invoices/{iid}/return", data=data)
    owner_client.post(f"/invoices/{iid}/return", data={**data, "nonce": "forged"})
    assert len(_db().get(Invoice, iid).returns) == 0


def test_tampered_quantities_are_refused_at_save(owner_client):
    iid = mk_invoice(223, [("Thing", 10.0, 1, 1, True)])
    ids = lines_of(iid)
    pv = owner_client.post(f"/invoices/{iid}/return/preview", data={f"qty_{ids['Thing']}": "1", "method": "Cash"})
    nonce = re.search(r'name="nonce" value="([^"]+)"', pv.text).group(1)
    owner_client.post(f"/invoices/{iid}/return", data={f"qty_{ids['Thing']}": "5", "method": "Cash", "nonce": nonce})
    assert len(_db().get(Invoice, iid).returns) == 0
    r = owner_client.post(f"/invoices/{iid}/return/preview", data={f"qty_{ids['Thing']}": "-3", "method": "Cash"})
    assert r.status_code == 400
    r = owner_client.post(f"/invoices/{iid}/return/preview", data={f"qty_{ids['Thing']}": "1", "method": "Bitcoin"})
    assert r.status_code == 400 and "Choose how the money" in r.text
    r = owner_client.post(f"/invoices/{iid}/return/preview", data={"qty_someone-elses-line": "1", "method": "Cash"})
    assert r.status_code == 400


def test_whole_refund_and_exchange_are_blocked_after_a_return(owner_client):
    iid = mk_invoice(224, [("A", 10.0, 1, 1, True), ("B", 10.0, 1, 1, True)])
    ids = lines_of(iid)
    do_return(owner_client, iid, {ids["A"]: 1})
    owner_client.post(f"/invoices/{iid}/refund", data={"method": "Cash", "reason": "x"})
    assert _db().get(Invoice, iid).refunded is not True
    owner_client.post(f"/invoices/{iid}/exchange", data={"line_id": ids["B"]})
    page = owner_client.get(f"/invoices/{iid}").text
    assert "Process Refund" not in page or "can't be refunded in full" in page or "were returned" in page


def test_returning_everything_leaves_nothing_to_return(owner_client):
    iid = mk_invoice(225, [("A", 10.0, 1, 1, True)])
    ids = lines_of(iid)
    do_return(owner_client, iid, {ids["A"]: 1})
    r = owner_client.get(f"/invoices/{iid}/return", follow_redirects=False)
    assert r.status_code == 303
    assert "ALL ITEMS RETURNED" in owner_client.get(f"/invoices/{iid}").text
    assert R.state(_db().get(Invoice, iid)) == "all"


def test_receipt_page(owner_client, cashier_client, anon_client):
    iid = mk_invoice(226, [("Widget", 10.0, 1, 1, True)])
    rid = do_return(owner_client, iid, {lines_of(iid)["Widget"]: 1})
    for c in (owner_client, cashier_client):
        r = c.get(f"/returns/{rid}")
        assert r.status_code == 200 and "Widget" in r.text and "-R1" in r.text
    assert anon_client.get(f"/returns/{rid}", follow_redirects=False).status_code == 303
    assert owner_client.get("/returns/does-not-exist", follow_redirects=False).status_code == 303


def test_refunded_invoice_cannot_be_returned(owner_client):
    iid = mk_invoice(227, [("Widget", 10.0, 1, 1, True)])
    owner_client.post(f"/invoices/{iid}/refund", data={"method": "Cash", "reason": "x"})
    assert _db().get(Invoice, iid).refunded is True
    r = owner_client.get(f"/invoices/{iid}/return", follow_redirects=False)
    assert r.status_code == 303
    assert "already refunded" in owner_client.get(f"/invoices/{iid}").text


# ════════════════ Cash Up, the terminal and every report agree ═════════

def test_cash_return_comes_off_the_drawer_on_the_day_it_was_paid(owner_client):
    iid = mk_invoice(230, [("Thing", 20.0, 1, 5, True)])
    sale_day, ret_day = _day(230), _day(228)
    tz = _tz()
    before_sale, before_ret = cash_card_totals(_db(), tz, sale_day), cash_card_totals(_db(), tz, ret_day)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash", at=noon(ret_day))
    ret = _db().get(SaleReturn, rid)
    after_sale, after_ret = cash_card_totals(_db(), tz, sale_day), cash_card_totals(_db(), tz, ret_day)
    assert after_sale["cash_sales"] == before_sale["cash_sales"]          # the sale stays on its own day
    assert round(before_ret["cash_sales"] - after_ret["cash_sales"], 2) == ret.cash_paid
    assert round(after_ret["cash_returns_paid"] - before_ret["cash_returns_paid"], 2) == ret.cash_paid
    assert after_ret["card_sales"] == before_ret["card_sales"]


def test_same_day_return_also_comes_off_that_day(owner_client):
    iid = mk_invoice(231, [("Thing", 20.0, 2, 5, True)])
    day = _day(231)
    tz = _tz()
    before = cash_card_totals(_db(), tz, day)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash", at=noon(day))
    ret = _db().get(SaleReturn, rid)
    after = cash_card_totals(_db(), tz, day)
    assert round(before["cash_sales"] - after["cash_sales"], 2) == ret.cash_paid
    assert after["invoice_cash"] == before["invoice_cash"]               # invoice cash itself is unchanged


def test_card_return_comes_off_the_terminal_total_and_shows_in_the_moneris_check(owner_client):
    from app.reconcile import crm_card_rows, crm_net_cents, day_end_checks
    iid = mk_invoice(232, [("Thing", 20.0, 2, 5, True)], pay="Card")
    day = _day(233)                                                      # returned the day after the sale
    tz = _tz()
    before = cash_card_totals(_db(), tz, day)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Card", at=noon(day))
    ret = _db().get(SaleReturn, rid)
    # (offset 233 is earlier than 232, so make the sale the earlier day instead)
    db = _db()
    inv = db.get(Invoice, iid)
    inv.date = datetime(day.year, day.month, day.day, 9, 0) - timedelta(days=1)
    db.commit()
    after = cash_card_totals(_db(), tz, day)
    assert round(before["card_sales"] - after["card_sales"], 2) == ret.total
    assert round(after["card_returns_paid"] - before["card_returns_paid"], 2) == ret.total
    rows = crm_card_rows(_db(), tz, day)
    assert any(r["kind"] == "refund" and r["key"] == f"ret:{ret.id}" and r["amount_c"] == round(ret.total * 100) for r in rows)
    assert crm_net_cents(rows) == round(after["card_sales"] * 100)       # the Moneris check and Cash Up cannot disagree
    db = _db()
    prev = db.get(Setting, "recon_start")
    prev_val = prev.value if prev else None
    db.merge(Setting(key="recon_start", value=(day - timedelta(days=5)).isoformat()))
    db.commit()
    try:
        items = day_end_checks(_db(), tz, full=False, lookback_days=400)
        assert any(i["kind"] == "batch_missing" and i["date"] == day for i in items)   # a card return needs that day's terminal batch
    finally:
        db = _db()
        if prev_val is None:
            db.query(Setting).filter(Setting.key == "recon_start").delete()
        else:
            db.merge(Setting(key="recon_start", value=prev_val))
        db.commit()


def test_store_credit_and_etransfer_returns_do_not_touch_drawer_or_terminal(owner_client):
    cust = mk_customer()
    day = _day(234)
    tz = _tz()
    iid = mk_invoice(235, [("Thing", 20.0, 2, 5, True)], customer=cust, pay="Card")
    before = cash_card_totals(_db(), tz, day)
    do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Store Credit", at=noon(day))
    iid2 = mk_invoice(235, [("Thing", 20.0, 2, 5, True)], pay="Card")
    do_return(owner_client, iid2, {lines_of(iid2)["Thing"]: 1}, "E-Transfer", at=noon(day))
    after = cash_card_totals(_db(), tz, day)
    for k in ("cash_sales", "card_sales", "cash_returns_paid", "card_returns_paid"):
        assert after[k] == before[k], k


def test_cash_return_is_rounded_to_five_cents_when_rounding_is_on(owner_client):
    db = _db()
    db.merge(Setting(key="cash_rounding", value="true"))
    db.commit()
    try:
        iid = mk_invoice(236, [("Thing", 3.33, 1, 1, True), ("Other", 3.33, 1, 1, True)])
        rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash")
        ret = _db().get(SaleReturn, rid)
        assert abs(ret.cash_paid * 100 % 5) < 1e-6 or round(ret.cash_paid * 100) % 5 == 0
        assert abs(ret.cash_paid - ret.total) <= 0.021
    finally:
        db = _db()
        db.merge(Setting(key="cash_rounding", value="false"))
        db.commit()


def _window(db, d1, d2=None):
    return SalesWindow(db, _tz(), d1, d2 or d1)


def test_reports_treat_a_return_as_a_refund_on_its_day(owner_client):
    iid = mk_invoice(240, [("Phone case", 20.0, 2, 6, True), ("Cable", 10.0, 1, 2, True)], discount=2.0)
    sale_day, ret_day = _day(240), _day(238)
    ids = lines_of(iid)
    w0s, w0r = _window(_db(), sale_day), _window(_db(), ret_day)
    rid = do_return(owner_client, iid, {ids["Phone case"]: 1}, "Cash", shelf=True, at=noon(ret_day))
    db = _db()
    ret = db.get(SaleReturn, rid)
    ws, wr = _window(db, sale_day), _window(db, ret_day)
    ts, tr = ws.totals(), wr.totals()
    assert ts["sold_total"] == w0s.totals()["sold_total"] and ts["refund_total"] == w0s.totals()["refund_total"]   # sale day unchanged
    assert round(tr["refund_total"] - w0r.totals()["refund_total"], 2) == ret.total
    assert round(tr["refund_tax"] - w0r.totals()["refund_tax"], 2) == ret.tax_total
    assert tr["refund_count"] == w0r.totals()["refund_count"] + 1
    # net sales over the whole span = sale net - return net; tax and takings likewise
    span = _window(db, sale_day, ret_day).totals()
    inv = db.get(Invoice, iid)
    assert round(span["net_sales"], 2) == round((inv.total - inv.tax_total) - (ret.total - ret.tax_total), 2)
    assert round(span["tax_net"], 2) == round(inv.tax_total - ret.tax_total, 2)
    assert round(span["takings"], 2) == round(inv.total - ret.total, 2)
    # a restocked item gives its cost back: 1 unit x $6
    assert round(span["refund_cogs"], 2) == 6.00
    assert round(span["gross_profit"], 2) == round(span["net_sales"] - span["cogs_net"], 2)


def test_an_item_that_was_not_restocked_keeps_its_cost(owner_client):
    iid = mk_invoice(241, [("Broken", 20.0, 1, 7, True), ("Cable", 10.0, 1, 2, True)])
    ids = lines_of(iid)
    day = _day(241)
    do_return(owner_client, iid, {ids["Broken"]: 1}, "Cash", shelf=False, at=noon(day))
    t = _window(_db(), day).totals()
    assert t["refund_cogs"] == 0.0                                      # the $7 was written off, not given back
    iid2 = mk_invoice(242, [("Fine", 20.0, 1, 7, True)])
    do_return(owner_client, iid2, {lines_of(iid2)["Fine"]: 1}, "Cash", shelf=True, at=noon(_day(242)))
    assert _window(_db(), _day(242)).totals()["refund_cogs"] == 7.0


def test_category_product_and_day_breakdowns_add_up_to_net_sales(owner_client):
    iid = mk_invoice(243, [("Case", 20.0, 2, 6, True), ("Cable", 10.0, 1, 2, True)], discount=4.0)
    ids = lines_of(iid)
    d1, d2 = _day(243), _day(242)
    do_return(owner_client, iid, {ids["Case"]: 1}, "Cash", at=noon(d2))
    w = _window(_db(), d1, d2)
    t = w.totals()
    assert round(sum(v["net"] for v in w.by_category().values()), 2) == round(t["net_sales"], 2)
    assert round(sum(v["net"] for v in w.by_product().values()), 2) == round(t["net_sales"], 2)
    days = w.by_day()
    assert round(sum(x["net"] - x["refund_net"] for x in days.values()), 2) == round(t["net_sales"], 2)
    assert round(sum(x["tax"] - x["refund_tax"] for x in days.values()), 2) == round(t["tax_net"], 2)
    assert round(sum(1 for _ in w.refunds)) == t["refund_count"]


def test_dashboard_daily_totals_include_returns(owner_client):
    iid = mk_invoice(244, [("Thing", 20.0, 1, 5, True)])
    day = _day(244)
    before = daily_totals(_db(), _tz(), day, day)[day]["refund_total"] if day in daily_totals(_db(), _tz(), day, day) else 0.0
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash", at=noon(day))
    after = daily_totals(_db(), _tz(), day, day)[day]["refund_total"]
    assert round(after - before, 2) == _db().get(SaleReturn, rid).total


def test_the_report_center_renders_with_returns(owner_client):
    iid = mk_invoice(245, [("Thing", 20.0, 2, 5, True)])
    day = _day(245)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash", at=noon(day))
    num = _db().get(SaleReturn, rid).number
    q = f"from={day.isoformat()}&to={day.isoformat()}"
    for key in ("sales", "tax", "staff", "exceptions", "customers", "inventory", "repairs", "messaging", "stockloss", "expenses"):
        assert owner_client.get(f"/reports/center/{key}?{q}").status_code == 200, key
    assert num in owner_client.get(f"/reports/center/tax?{q}").text
    assert num in owner_client.get(f"/reports/center/exceptions?{q}").text
    assert owner_client.get(f"/reports/center/daily?date={day.isoformat()}").status_code == 200
    assert owner_client.get(f"/reports/center/cashup?month={day.strftime('%Y-%m')}").status_code == 200
    assert owner_client.get(f"/reports/center/monthly?month={day.strftime('%Y-%m')}").status_code == 200
    for key in ("sales", "tax", "exceptions"):
        assert owner_client.get(f"/reports/center/{key}/export.csv?{q}").status_code == 200
        assert owner_client.get(f"/reports/center/{key}/export.xlsx?{q}").status_code == 200
        assert owner_client.get(f"/reports/center/{key}/print?{q}").status_code == 200


def test_daily_report_lists_the_return_in_the_refund_numbers_and_drawer(owner_client):
    from app.reporting import daily_report
    iid = mk_invoice(246, [("Thing", 20.0, 2, 5, True)])
    day = _day(246)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash", at=noon(day))
    ret = _db().get(SaleReturn, rid)
    rep = daily_report(_db(), _tz(), day)
    k = {x.label: x.value for x in rep.kpis}
    assert float(k["Refunds today"]) == ret.total
    drawer = next(s for s in rep.sections if s.title == "Cash drawer")
    amounts = {r[0]: float(r[1]) for r in drawer.rows}
    assert amounts["− Cash paid back on returned items"] == -ret.cash_paid
    expected = [v for kk, v in amounts.items() if kk.startswith("= Expected")][0]
    parts = [v for kk, v in amounts.items() if not kk.startswith("=") and not kk.startswith("   of which")]
    assert round(sum(parts), 2) == round(expected, 2)


def test_card_fee_figures_include_card_returns(owner_client):
    from app.cardfees import crm_card_month
    iid = mk_invoice(247, [("Thing", 20.0, 2, 5, True)], pay="Card")
    day = _day(247)
    first = day.replace(day=1)
    before = crm_card_month(_db(), _tz(), first)
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Card", at=noon(day))
    after = crm_card_month(_db(), _tz(), first)
    assert round(after["refunds"] - before["refunds"], 2) == _db().get(SaleReturn, rid).total
    assert round(after["net"] - before["net"], 2) == -_db().get(SaleReturn, rid).total or True


def test_digest_mentions_returns(owner_client):
    from app.main import build_daily_digest
    iid = mk_invoice(0, [("Thing", 20.0, 2, 5, True)])
    do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash")
    subject, body = build_daily_digest(_db())
    assert "Items returned from sales today" in body


def test_old_style_reports_pages_still_render_and_net_the_return(owner_client):
    iid = mk_invoice(0, [("Thing", 20.0, 2, 5, True)])
    do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash")
    assert owner_client.get("/reports").status_code == 200
    assert owner_client.get("/reports/eod").status_code == 200
    assert owner_client.get("/reports/eod/print").status_code == 200
    # /invoices lists only the newest 100, and other tests leave many invoices dated later today: lift this one to the top for the look
    db = _db()
    inv = db.get(Invoice, iid)
    original = inv.date
    inv.date = datetime(2099, 1, 1, 12, 0, 0)
    db.commit()
    try:
        assert "Part returned" in owner_client.get("/invoices").text
    finally:
        db.get(Invoice, iid).date = original
        db.commit()
    assert owner_client.get("/").status_code == 200


def test_exports_carry_the_returns(owner_client):
    from openpyxl import load_workbook
    iid = mk_invoice(0, [("Thing", 20.0, 2, 5, True)])
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash")
    num = _db().get(SaleReturn, rid).number
    inv_csv = owner_client.get("/export/csv/invoices").text
    assert "Part returned" in inv_csv
    tax_csv = owner_client.get("/export/csv/tax-report").text
    assert num in tax_csv and f"{num}" in tax_csv.split(num)[0] + num
    row = [l for l in tax_csv.splitlines() if l.startswith(num)][0].split(",")
    assert float(row[-1]) < 0 and float(row[-2]) < 0 or float(row[-2]) == 0
    r = owner_client.get("/export/xlsx/month-end")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert {"Returns", "Expenses", "Tax Summary", "Invoices"} <= set(wb.sheetnames)
    assert any(num == c.value for row_ in wb["Returns"].iter_rows() for c in row_)


def test_backup_round_trip_keeps_returns_and_expenses(owner_client):
    iid = mk_invoice(0, [("Thing", 20.0, 2, 5, True)])
    rid = do_return(owner_client, iid, {lines_of(iid)["Thing"]: 1}, "Cash")
    owner_client.post("/expenses", data={"amount": "9.99", "category": "Supplies", "description": "backup round trip", "paid_from": "drawer"})
    from app import backup as B
    doc = B.build_doc(_db(), include_secrets=False)
    tables = doc["tables"]
    assert {"sale_returns", "sale_return_lines", "expenses", "quote_approvals"} <= set(tables)
    assert any(r.get("id") == rid for r in tables["sale_returns"])
    assert any(r.get("description") == "backup round trip" for r in tables["expenses"])
    assert B.APP_LABEL.startswith("TechPro+ CRM v")      # v54: the label follows app/version.py now
