"""v52.1 — a purchase order made by mistake can be cancelled, and a cancelled/received one can't add stock
(found by the whole-build end-to-end run: there was no way to get rid of a wrong order)."""
import pytest

from app.models import Product, PurchaseOrder, PurchaseOrderLine

TAG = "V52PO"


@pytest.fixture
def product(db_session):
    p = Product(sku=f"{TAG}-1", name=f"{TAG} part", category="PART", price=20.0, cost=8.0, stock=4)
    db_session.add(p)
    db_session.commit()
    yield p
    db_session.rollback()
    for po in db_session.query(PurchaseOrder).filter(PurchaseOrder.notes == TAG).all():
        db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).delete()
        db_session.delete(po)
    db_session.delete(db_session.get(Product, p.id))
    db_session.commit()


def _make(c, db, product, send=False):
    r = c.post("/purchase-orders/add", data={"supplier_id": "", "notes": TAG, "product_id": [product.id], "qty": ["6"], "unit_cost": ["7.50"]},
               follow_redirects=False)
    po_id = r.headers["location"].rsplit("/", 1)[-1]
    if send:
        c.post(f"/purchase-orders/{po_id}/send")
    db.expire_all()
    return po_id


def test_a_draft_order_can_be_cancelled(owner_client, db_session, product):
    po = _make(owner_client, db_session, product)
    page = owner_client.get(f"/purchase-orders/{po}").text
    assert f"/purchase-orders/{po}/cancel" in page
    owner_client.post(f"/purchase-orders/{po}/cancel")
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po).status == "cancelled"
    assert f"/purchase-orders/{po}/cancel" not in owner_client.get(f"/purchase-orders/{po}").text
    assert db_session.get(Product, product.id).stock == 4


def test_a_sent_order_can_be_cancelled_and_then_cannot_be_received(owner_client, db_session, product):
    po = _make(owner_client, db_session, product, send=True)
    owner_client.post(f"/purchase-orders/{po}/cancel")
    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po).first()
    owner_client.post(f"/purchase-orders/{po}/receive", data={f"received_{line.id}": "6"})
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po).status == "cancelled"
    assert db_session.get(Product, product.id).stock == 4


def test_a_received_order_cannot_be_cancelled_or_received_twice(owner_client, db_session, product):
    po = _make(owner_client, db_session, product, send=True)
    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po).first()
    owner_client.post(f"/purchase-orders/{po}/receive", data={f"received_{line.id}": "6"})
    owner_client.post(f"/purchase-orders/{po}/receive", data={f"received_{line.id}": "6"})
    owner_client.post(f"/purchase-orders/{po}/cancel")
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po).status == "received"
    assert db_session.get(Product, product.id).stock == 10


def test_a_cashier_cannot_cancel(owner_client, cashier_client, db_session, product):
    po = _make(owner_client, db_session, product)
    assert cashier_client.post(f"/purchase-orders/{po}/cancel").status_code == 403
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po).status == "draft"
