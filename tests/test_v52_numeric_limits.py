"""v52.2 — typed amounts and quantities are checked on the server, not only in the browser.

Found by the whole-build number sweep: a hand-made request with "nan", "inf", a minus sign or a huge number was saved as a price,
a stock level, store credit, a deposit or a setting, and then broke the pages that list or total them (or the database, for a
number past the integer limit). A "Split" payment with only one side filled in was also saved as "Split" with no cash and no card,
so the money counted in neither column of Cash Up.
"""
import pytest

from app.models import Customer, Invoice, Layaway, LayawayPayment, Product, Repair, Setting, Supplier

BAD = ["nan", "inf", "-inf", "-5", "1e308", "99999999999"]


@pytest.fixture
def customer(db_session):
    c = Customer(name="Numeric Nell", phone="555-0199")
    db_session.add(c)
    db_session.commit()
    yield c
    db_session.rollback()
    for lay in db_session.query(Layaway).filter(Layaway.customer_id == c.id).all():
        db_session.query(LayawayPayment).filter(LayawayPayment.layaway_id == lay.id).delete()
        db_session.delete(lay)
    db_session.delete(c)
    db_session.commit()


def _product(db):
    return db.query(Product).filter(Product.stock > 5, Product.price > 0).first()


def _setting(db, key):
    db.expire_all()
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else None


# ── products ─────────────────────────────────────────────────────────

def test_product_add_refuses_nonsense_numbers(owner_client, db_session):
    before = db_session.query(Product).count()
    for bad in BAD:
        for field in ("price", "cost", "stock", "reorder_threshold", "reorder_qty"):
            data = {"name": "Numeric Widget", "sku": "NUMW-1", "category": "Accessories", "price": "9.99", "cost": "4", "stock": "3"}
            data[field] = bad
            r = owner_client.post("/products/add", data=data, follow_redirects=False)
            assert r.status_code in (303, 422), (field, bad, r.status_code)
    db_session.expire_all()
    assert db_session.query(Product).count() == before
    assert owner_client.get("/products").status_code == 200
    assert owner_client.get("/pos").status_code == 200


def test_product_edit_refuses_nonsense_numbers(owner_client, db_session):
    p = _product(db_session)
    price, cost, stock = p.price, p.cost, p.stock
    for bad in BAD:
        r = owner_client.post(f"/products/{p.id}/edit", data={"name": p.name, "sku": p.sku, "category": p.category or "", "price": bad,
                                                              "cost": str(cost), "stock": str(stock)}, follow_redirects=False)
        assert r.status_code in (303, 422)
        r = owner_client.post(f"/products/{p.id}/edit", data={"name": p.name, "sku": p.sku, "category": p.category or "", "price": str(price),
                                                              "cost": str(cost), "stock": bad}, follow_redirects=False)
        assert r.status_code in (303, 422)
    db_session.expire_all()
    p = db_session.get(Product, p.id)
    assert (p.price, p.cost, p.stock) == (price, cost, stock)
    assert owner_client.get("/products").status_code == 200


# ── the sale screen ──────────────────────────────────────────────────

def test_pos_numbers_cannot_break_the_sale_screen(owner_client, db_session):
    p = _product(db_session)
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    for bad in ("nan", "inf", "1e308", "99999999999"):
        owner_client.post("/pos/subtotal", data={"value": bad})
        owner_client.post("/pos/discount", data={"mode": "$", "value": bad})
        owner_client.post("/pos/add-custom", data={"product_id": p.id, "custom_price": bad})
        assert owner_client.get("/pos").status_code == 200, bad
    owner_client.post("/pos/qty/0", data={"qty": "99999999999"})
    assert owner_client.get("/pos").status_code == 200
    owner_client.post("/pos/clear")


def test_checkout_refuses_nonsense_amounts_received(owner_client, db_session):
    p = _product(db_session)
    before = db_session.query(Invoice).count()
    for tendered in ("-5", "nan", "inf", "99999999999"):
        owner_client.post("/pos/clear")
        owner_client.post(f"/pos/add/{p.id}")
        r = owner_client.post("/pos/checkout", data={"payment_method": "Cash", "tendered": tendered}, follow_redirects=False)
        assert r.status_code == 400, tendered
    owner_client.post("/pos/clear")
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before


def test_split_with_one_side_or_nothing_is_not_saved_as_an_empty_split():
    from app.money import plan_payment
    # nothing, or a minus: not a payment
    for cash, card in ((0, 0), (-10, 50), (50, -10)):
        assert plan_payment("Split", 100.0, 0, cash, card, True)["error"]
    # only the card side typed and it covers the sale: that is a Card sale
    p = plan_payment("Split", 100.0, 0, 0, 100.0, True)
    assert p["error"] is None and p["payment_method"] == "Card" and p["card_amount"] == 100.0 and p["cash_amount"] == 0
    # only the cash side typed and it covers the sale: that is a Cash sale, with change
    p = plan_payment("Split", 100.0, 0, 120.0, 0, True)
    assert p["error"] is None and p["payment_method"] == "Cash" and p["cash_amount"] == 100.0 and p["change_given"] == 20.0
    # short, or card more than the total with no cash to give change from
    assert plan_payment("Split", 100.0, 0, 0, 60.0, True)["error"]
    assert plan_payment("Split", 100.0, 0, 50.0, 0, True)["error"]
    assert plan_payment("Split", 100.0, 0, 0, 120.0, True)["error"]
    # a real split is unchanged
    p = plan_payment("Split", 100.0, 0, 60.0, 40.0, True)
    assert p["error"] is None and p["payment_method"] == "Split (Cash $60.00 + Card $40.00)"


def test_split_with_nothing_entered_is_refused_by_checkout(owner_client, db_session):
    p = _product(db_session)
    before = db_session.query(Invoice).count()
    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    r = owner_client.post("/pos/checkout", data={"payment_method": "Split", "cash_part": "0", "card_part": "0", "tendered": "0"}, follow_redirects=False)
    assert r.status_code == 400  # nothing entered is not a payment
    owner_client.post("/pos/clear")
    db_session.expire_all()
    assert db_session.query(Invoice).count() == before


# ── store credit, layaway ────────────────────────────────────────────

def test_store_credit_amount_must_be_a_real_positive_number(owner_client, db_session, customer):
    for bad in ("nan", "inf", "-inf", "-5", "0", "1e308", "99999999999"):
        owner_client.post(f"/customers/{customer.id}/credit", data={"amount": bad})
    db_session.expire_all()
    assert (db_session.get(Customer, customer.id).store_credit or 0) == 0
    assert owner_client.get(f"/customers/{customer.id}").status_code == 200
    owner_client.post(f"/customers/{customer.id}/credit", data={"amount": "12.50"})
    db_session.expire_all()
    assert db_session.get(Customer, customer.id).store_credit == 12.5


def test_layaway_deposit_and_payment_are_checked(owner_client, db_session, customer):
    p = _product(db_session)
    before = db_session.query(Layaway).count()
    for bad in ("nan", "inf", "-5", "1e308"):
        owner_client.post("/pos/clear")
        owner_client.post(f"/pos/add/{p.id}")
        owner_client.post("/pos/customer", data={"customer_id": customer.id})
        r = owner_client.post("/pos/layaway/new", data={"deposit": bad, "due_date": ""}, follow_redirects=False)
        assert r.status_code == 303 and "/layaway/" not in r.headers["location"].replace("/layaway/new", ""), bad
    db_session.expire_all()
    assert db_session.query(Layaway).count() == before

    owner_client.post("/pos/clear")
    owner_client.post(f"/pos/add/{p.id}")
    owner_client.post("/pos/customer", data={"customer_id": customer.id})
    r = owner_client.post("/pos/layaway/new", data={"deposit": "5", "due_date": ""}, follow_redirects=False)
    lay_id = r.headers["location"].rsplit("/", 1)[-1]
    for bad in ("nan", "inf", "-5", "0", "1e308"):
        owner_client.post(f"/layaway/{lay_id}/payment", data={"amount": bad, "method": "Cash"})
    db_session.expire_all()
    lay = db_session.get(Layaway, lay_id)
    assert lay.paid_total == 5 and lay.status == "active"
    assert owner_client.get(f"/layaway/{lay_id}").status_code == 200


# ── repairs, suppliers, settings ─────────────────────────────────────

def test_repair_estimate_and_warranty_are_checked(owner_client, db_session):
    before = db_session.query(Repair).count()
    base = {"phone": "555-0177", "name": "Numeric Nell", "device": "Pixel 8", "issue": "Screen", "warranty_days": "90"}
    for est in ("abc", "nan", "inf", "-5", "1e308"):
        owner_client.post("/repairs/add", data={**base, "estimated_cost": est})
    for days in ("-1", "99999", "99999999999"):
        owner_client.post("/repairs/add", data={**base, "estimated_cost": "50", "warranty_days": days})
    db_session.expire_all()
    assert db_session.query(Repair).count() == before
    owner_client.post("/repairs/add", data={**base, "estimated_cost": ""})
    owner_client.post("/repairs/add", data={**base, "estimated_cost": "$1,250.50"})
    db_session.expire_all()
    new = db_session.query(Repair).order_by(Repair.ticket_no.desc()).limit(2).all()
    assert db_session.query(Repair).count() == before + 2
    assert sorted(r.estimated_cost if r.estimated_cost is not None else -1 for r in new) == [-1, 1250.5]
    # the cost form on the ticket
    t = new[0]
    keep = (t.estimated_cost, t.final_cost)
    for bad in ("nan", "inf", "abc", "-5", "1e308"):
        owner_client.post(f"/repairs/{t.id}/cost", data={"estimated_cost": bad, "final_cost": ""})
        owner_client.post(f"/repairs/{t.id}/cost", data={"estimated_cost": "", "final_cost": bad})
    db_session.expire_all()
    t = db_session.get(Repair, t.id)
    assert (t.estimated_cost, t.final_cost) == keep
    for r in new:
        db_session.delete(db_session.get(Repair, r.id))
    db_session.commit()


def test_supplier_lead_time_is_checked(owner_client, db_session):
    before = db_session.query(Supplier).count()
    for bad in ("-1", "366", "99999999999"):
        owner_client.post("/suppliers/add", data={"name": "Numeric Supply", "lead_time_days": bad})
    db_session.expire_all()
    assert db_session.query(Supplier).count() == before
    owner_client.post("/suppliers/add", data={"name": "Numeric Supply", "lead_time_days": "7"})
    db_session.expire_all()
    s = db_session.query(Supplier).filter_by(name="Numeric Supply").first()
    assert s and s.lead_time_days == 7
    owner_client.post(f"/suppliers/{s.id}/edit", data={"name": "Numeric Supply", "lead_time_days": "9999"})
    db_session.expire_all()
    assert db_session.get(Supplier, s.id).lead_time_days == 7
    db_session.delete(db_session.get(Supplier, s.id))
    db_session.commit()


def test_settings_numbers_are_checked_and_zero_points_is_allowed(owner_client, db_session):
    db_session.expire_all()
    snapshot = {r.key: r.value for r in db_session.query(Setting).all()}      # the settings form rewrites every box, so put it all back
    original = {"shop_name": snapshot.get("shop_name") or "Test Shop", "province": snapshot.get("province") or "ON",
                "invoice_prefix": snapshot.get("invoice_prefix") or "INV", "points_per_dollar": "1", "points_redeem_rate": "100", "digest_hour": "21"}
    try:
        _settings_numbers_body(owner_client, db_session, original)
    finally:
        db_session.rollback()
        for row in db_session.query(Setting).all():
            if row.key not in snapshot:
                db_session.delete(row)
            elif row.value != snapshot[row.key]:
                row.value = snapshot[row.key]
        db_session.commit()


def _settings_numbers_body(owner_client, db_session, original):
    base = dict(original)
    owner_client.post("/settings", data=base)
    keep = [_setting(db_session, k) for k in ("points_per_dollar", "points_redeem_rate", "digest_hour")]
    for field, bads in (("points_per_dollar", BAD + ["101"]), ("points_redeem_rate", BAD + ["0", "0.5"]), ("digest_hour", ["-1", "24", "99", "abc"])):
        for bad in bads:
            owner_client.post("/settings", data={**base, field: bad, "shop_name": "Changed Name"})
            assert [_setting(db_session, k) for k in ("points_per_dollar", "points_redeem_rate", "digest_hour")] == keep, (field, bad)
            assert _setting(db_session, "shop_name") != "Changed Name", (field, bad)
    owner_client.post("/settings", data={**base, "points_per_dollar": "0"})
    assert float(_setting(db_session, "points_per_dollar")) == 0
    owner_client.post("/settings", data=base)
    assert owner_client.get("/settings").status_code == 200


def test_api_documentation_pages_are_switched_off(owner_client, anon_client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert owner_client.get(path, follow_redirects=False).status_code == 404, path
        r = anon_client.get(path, follow_redirects=False)
        assert r.status_code in (303, 404) and "openapi" not in r.text.lower(), path
