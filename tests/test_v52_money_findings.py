"""v52.2 — money problems found by the whole-build end-to-end run.

 * Paying a sale with the "Store Credit" pill took nothing off anyone's balance, and worked with no customer attached.
 * A layaway payment bigger than the balance was recorded in full, so Cash Up expected the change that was handed back.
 * Refunding a whole invoice kept the loyalty points that sale earned and did not give back the points / store credit the
   customer had spent on it; a partial return kept the earned points too.
"""
import re

import pytest

from app.models import Customer, Invoice, Layaway, LayawayPayment, Product, Setting


@pytest.fixture
def gadget(owner_client, db_session):
    owner_client.post("/products/add", data={"name": "F-Gadget", "sku": "FGAD-1", "category": "Accessories", "price": "50", "cost": "20", "stock": "40"})
    db_session.expire_all()
    p = db_session.query(Product).filter_by(sku="FGAD-1").first()
    assert p is not None
    return p


def _customer(db, name, phone, points=0, credit=0.0):
    c = Customer(name=name, phone=phone, points=points, store_credit=credit)
    db.add(c)
    db.commit()
    return c


def _ppd(db):
    db.expire_all()
    row = db.query(Setting).filter_by(key="points_per_dollar").first()
    return float(row.value) if row else 1.0


def _start_sale(client, product, customer=None, qty=1):
    client.post("/pos/clear")
    client.post("/pos/customer", data={"customer_id": customer.id if customer else ""})
    for _ in range(qty):
        client.post(f"/pos/add/{product.id}")


# ── store credit as the way of paying ────────────────────────────────

def test_store_credit_payment_needs_a_customer(owner_client, db_session, gadget):
    before = db_session.query(Invoice).count()
    _start_sale(owner_client, gadget)
    r = owner_client.post("/pos/checkout", data={"payment_method": "Store Credit"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pos"
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before
    owner_client.post("/pos/clear")


def test_store_credit_payment_refused_when_balance_is_short(owner_client, db_session, gadget):
    c = _customer(db_session, "Short Credit", "555-0301", credit=5.0)
    before = db_session.query(Invoice).count()
    _start_sale(owner_client, gadget, c)
    r = owner_client.post("/pos/checkout", data={"payment_method": "Store Credit"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before
    assert db_session.get(Customer, c.id).store_credit == 5.0
    owner_client.post("/pos/clear")


def test_store_credit_payment_takes_the_sale_off_the_balance(owner_client, db_session, gadget):
    c = _customer(db_session, "Big Credit", "555-0302", credit=500.0)
    _start_sale(owner_client, gadget, c)
    r = owner_client.post("/pos/checkout", data={"payment_method": "Store Credit"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    assert inv.payment_method == "Store Credit" and inv.total > 50
    assert db_session.get(Customer, c.id).store_credit == round(500.0 - inv.total, 2)
    assert (inv.cash_amount or 0) == 0 and (inv.card_amount or 0) == 0


# ── layaway payment bigger than the balance ──────────────────────────

def _layaway(client, db, product, customer, deposit="0"):
    client.post("/pos/clear")
    client.post("/pos/customer", data={"customer_id": customer.id})
    client.post(f"/pos/add/{product.id}")
    r = client.post("/pos/layaway/new", data={"deposit": deposit, "due_date": ""}, follow_redirects=False)
    lid = r.headers["location"].rsplit("/", 1)[-1]
    db.expire_all()
    return db.get(Layaway, lid)


def test_layaway_cash_overpayment_records_only_the_balance(owner_client, db_session, gadget):
    c = _customer(db_session, "Lay Over", "555-0303")
    lay = _layaway(owner_client, db_session, gadget, c, deposit="10")
    owing = round(lay.total - 10, 2)
    r = owner_client.post(f"/layaway/{lay.id}/payment", data={"amount": "200", "method": "Cash"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    lay = db_session.get(Layaway, lay.id)
    assert lay.status == "completed" and lay.paid_total == lay.total
    pays = db_session.query(LayawayPayment).filter_by(layaway_id=lay.id).order_by(LayawayPayment.created_at).all()
    assert round(sum(p.amount for p in pays), 2) == lay.total
    assert pays[-1].amount == owing
    # the change due is on the confirmation message
    page = owner_client.get(f"/layaway/{lay.id}")
    assert f"Give ${200 - owing:.2f} back as change" in page.text


def test_layaway_card_payment_over_the_balance_is_refused(owner_client, db_session, gadget):
    c = _customer(db_session, "Lay Card", "555-0304")
    lay = _layaway(owner_client, db_session, gadget, c, deposit="10")
    for method in ("Debit", "Credit", "E-Transfer"):
        owner_client.post(f"/layaway/{lay.id}/payment", data={"amount": "500", "method": method})
    db_session.expire_all()
    lay = db_session.get(Layaway, lay.id)
    assert lay.status == "active" and lay.paid_total == 10
    assert db_session.query(LayawayPayment).filter_by(layaway_id=lay.id).count() == 1


# ── refunds and loyalty ──────────────────────────────────────────────

def _sale_with_redemptions(client, db, product, customer):
    _start_sale(client, product, customer)
    client.post("/pos/redeem-points", data={"points": "100"})
    client.post("/pos/redeem-credit", data={"amount": "5"})
    r = client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    return db.query(Invoice).filter_by(customer_id=customer.id).one()


def test_whole_refund_takes_back_earned_points_and_gives_back_spent_ones(owner_client, db_session, gadget):
    c = _customer(db_session, "Refund Rita", "555-0305", points=500, credit=20.0)
    inv = _sale_with_redemptions(owner_client, db_session, gadget, c)
    cust = db_session.get(Customer, c.id)
    earned = int(inv.total * _ppd(db_session))
    assert cust.points == 500 - 100 + earned and cust.store_credit == 15.0
    r = owner_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Cash", "refund_reason": "test"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    cust = db_session.get(Customer, c.id)
    assert cust.points == 500 and cust.store_credit == 20.0
    # refunding the same invoice twice does nothing more
    owner_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Cash"})
    db_session.expire_all()
    cust = db_session.get(Customer, c.id)
    assert cust.points == 500 and cust.store_credit == 20.0


def test_whole_refund_to_store_credit_still_adds_the_money_back(owner_client, db_session, gadget):
    c = _customer(db_session, "Refund Sam", "555-0306", points=0, credit=0.0)
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"})
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    owner_client.post(f"/invoices/{inv.id}/refund", data={"refund_method": "Store Credit"})
    db_session.expire_all()
    cust = db_session.get(Customer, c.id)
    assert cust.store_credit == inv.total and cust.points == 0


def test_partial_return_takes_back_the_points_for_the_money_returned(owner_client, db_session, gadget):
    c = _customer(db_session, "Return Rick", "555-0307")
    _start_sale(owner_client, gadget, c, qty=2)
    owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"})
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    full_points = db_session.get(Customer, c.id).points
    assert full_points == int(inv.total * _ppd(db_session)) > 0
    line = inv.lines[0]
    data = {"method": "Cash", "reason": "", f"qty_{line.id}": "1", f"shelf_{line.id}": "on"}
    pv = owner_client.post(f"/invoices/{inv.id}/return/preview", data=data)
    data["nonce"] = re.search(r'name="nonce" value="([^"]+)"', pv.text).group(1)
    assert owner_client.post(f"/invoices/{inv.id}/return", data=data, follow_redirects=False).status_code == 303
    db_session.expire_all()
    ret = db_session.get(Invoice, inv.id).returns[0]
    assert db_session.get(Customer, c.id).points == max(0, full_points - int(ret.total * _ppd(db_session)))


# ── store credit used on a sale is a payment: tax is on the full price ─

def _province(db):
    db.expire_all()
    row = db.query(Setting).filter_by(key="province").first()
    return row.value if row and row.value else "ON"


def test_tax_is_charged_on_the_price_before_store_credit(owner_client, db_session, gadget):
    from app.tax import calc_canadian_tax
    c = _customer(db_session, "Credit Cara", "555-0310", credit=100.0)
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/redeem-credit", data={"amount": "20"})
    r = owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    full = calc_canadian_tax(50.0, _province(db_session))
    assert inv.subtotal == 50.0 and inv.store_credit_used == 20.0
    assert inv.tax_total == full["tax_total"]                      # tax on $50, not on $30
    assert inv.total == round(full["total"] - 20.0, 2)             # the customer pays price + tax - credit
    assert round(inv.subtotal - inv.discount + inv.tax_total, 2) == inv.total
    assert db_session.get(Customer, c.id).store_credit == 80.0
    assert inv.cash_amount == inv.total


def test_asking_for_more_credit_than_the_sale_needs_only_uses_what_is_needed(owner_client, db_session, gadget):
    c = _customer(db_session, "Credit Carl", "555-0311", credit=1000.0)
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/redeem-credit", data={"amount": "900"})
    r = owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    assert inv.total == 0 and 0 < inv.store_credit_used < 900
    assert db_session.get(Customer, c.id).store_credit == round(1000.0 - inv.store_credit_used, 2)   # the extra was NOT burned


def test_a_layaway_cannot_be_started_with_credit_or_points_applied(owner_client, db_session, gadget):
    c = _customer(db_session, "Credit Lay", "555-0312", points=500, credit=50.0)
    before = db_session.query(Layaway).count()
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/redeem-credit", data={"amount": "10"})
    r = owner_client.post("/pos/layaway/new", data={"deposit": "0", "due_date": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pos"
    owner_client.post("/pos/clear")
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/redeem-points", data={"points": "100"})
    r = owner_client.post("/pos/layaway/new", data={"deposit": "0", "due_date": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pos"
    owner_client.post("/pos/clear")
    db_session.expire_all()
    assert db_session.query(Layaway).count() == before
    assert (db_session.get(Customer, c.id).store_credit, db_session.get(Customer, c.id).points) == (50.0, 500)


def test_exchange_on_a_credit_sale_keeps_tax_on_the_price_before_credit(owner_client, db_session, gadget):
    from app.tax import calc_canadian_tax
    owner_client.post("/products/add", data={"name": "F-Gadget Pro", "sku": "FGAD-2", "category": "Accessories", "price": "80", "cost": "30", "stock": "10"})
    db_session.expire_all()
    pro = db_session.query(Product).filter_by(sku="FGAD-2").first()
    c = _customer(db_session, "Credit Xena", "555-0313", credit=100.0)
    _start_sale(owner_client, gadget, c)
    owner_client.post("/pos/redeem-credit", data={"amount": "20"})
    owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": "0"})
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(customer_id=c.id).one()
    line = inv.lines[0]
    r = owner_client.post(f"/invoices/{inv.id}/exchange-line/{line.id}", data={"new_product_id": pro.id}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    inv = db_session.get(Invoice, inv.id)
    full = calc_canadian_tax(80.0, _province(db_session))
    assert inv.tax_total == full["tax_total"]
    assert inv.total == round(full["total"] - 20.0, 2)
