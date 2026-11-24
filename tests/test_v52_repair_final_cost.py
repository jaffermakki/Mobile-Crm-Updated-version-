"""v52.1 — a collected repair's price is stored before tax (found by the whole-build end-to-end run).

It used to be filled from the invoice TOTAL (tax included), so a $179.00 quote that was charged exactly as quoted showed
a "final price" of $200.48: the ticket's margin was overstated by the tax and the Repairs report said the price "came in
higher than the quote"."""
from app.models import Invoice, Repair

TAG = "V52FC"


def _ticket(c, db, phone, est):
    c.post("/repairs/add", data={"phone": phone, "name": f"{TAG} Cust", "device": f"{TAG} {phone}", "issue": "Screen Replacement",
                                  "description": "x", "estimated_cost": est, "warranty_days": "90"}, follow_redirects=False)
    db.expire_all()
    return db.query(Repair).filter(Repair.device == f"{TAG} {phone}").first()


def _collect(c, rid, **pay):
    c.post(f"/repairs/{rid}/charge", follow_redirects=False)
    return c.post("/pos/checkout", data=pay or {"payment_method": "Cash", "tendered": "500"}, follow_redirects=False)


def test_final_cost_is_the_price_before_tax(owner_client, db_session):
    r = _ticket(owner_client, db_session, "555-8101", "179.00")
    _collect(owner_client, r.id)
    db_session.expire_all()
    r = db_session.get(Repair, r.id)
    inv = db_session.query(Invoice).filter(Invoice.repair_id == r.id).first()
    assert r.status == "COLLECTED"
    assert inv.total > 179.0                      # tax was added on the invoice ...
    assert round(r.final_cost, 2) == 179.00       # ... but the ticket's price is the repair price itself


def test_a_typed_final_cost_is_kept_and_charged(owner_client, db_session):
    r = _ticket(owner_client, db_session, "555-8102", "89.00")
    owner_client.post(f"/repairs/{r.id}/cost", data={"estimated_cost": "89.00", "final_cost": "99.00"})
    _collect(owner_client, r.id)
    db_session.expire_all()
    assert round(db_session.get(Repair, r.id).final_cost, 2) == 99.00


def test_ticket_margin_and_quote_accuracy_are_not_inflated_by_tax(owner_client, db_session):
    r = _ticket(owner_client, db_session, "555-8103", "100.00")
    _collect(owner_client, r.id)
    page = owner_client.get(f"/repairs/{r.id}").text
    assert "112.00" not in page and "$100.00" in page    # 100 + 12% BC tax would show 112.00
    # an exactly-as-quoted ticket must not be counted as "came in higher": estimate == final
    db_session.expire_all()
    r = db_session.get(Repair, r.id)
    assert r.estimated_cost == r.final_cost
