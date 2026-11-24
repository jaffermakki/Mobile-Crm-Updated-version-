import re
"""
v51 — receiving a purchase order by scanning the delivery (the server side: which line a scanned code belongs to,
who may ask, and what the page offers). The counting itself is JavaScript, checked in a real browser.
"""
import pytest

from app.models import Product, PurchaseOrder, PurchaseOrderLine, Supplier

UPC_A = "036000291452"
EAN_13 = "0036000291452"          # the same number as EAN-13
EAN_B = "4006381333931"

_made = {"po": [], "products": [], "suppliers": []}


@pytest.fixture(autouse=True)
def _clean(db_session):
    yield
    db_session.rollback()
    for po_id in _made["po"]:
        po = db_session.get(PurchaseOrder, po_id)
        if po:
            db_session.delete(po)
    db_session.flush()
    for pid in _made["products"]:
        p = db_session.get(Product, pid)
        if p:
            db_session.delete(p)
    for sid in _made["suppliers"]:
        s = db_session.get(Supplier, sid)
        if s:
            db_session.delete(s)
    db_session.commit()
    for key in _made:
        _made[key].clear()
    db_session.expire_all()


def product(db, sku, name=None, barcode="", stock=5):
    p = Product(sku=sku, name=name or f"V51 {sku}", barcode=barcode, stock=stock, cost=2.0, price=6.0, category="v51-po")
    db.add(p)
    db.commit()
    _made["products"].append(p.id)
    return p.id


def make_po(client, db, items, send=True):
    """items: [(product_id, qty)] -> purchase order id"""
    supplier = Supplier(name="V51 Supplier")
    db.add(supplier)
    db.commit()
    _made["suppliers"].append(supplier.id)
    resp = client.post("/purchase-orders/add", data={"supplier_id": supplier.id, "notes": "V51 test",
                                                      "product_id": [p for p, _ in items], "qty": [str(q) for _, q in items],
                                                      "unit_cost": ["2.00"] * len(items)}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    po_id = resp.headers["location"].rsplit("/", 1)[-1]
    _made["po"].append(po_id)
    if send:
        assert client.post(f"/purchase-orders/{po_id}/send", follow_redirects=False).status_code == 303
    return po_id


def lookup(client, po_id, code):
    return client.get(f"/purchase-orders/{po_id}/scan.json", params={"code": code})


def line_id(db, po_id, pid):
    db.expire_all()
    return next(l.id for l in db.get(PurchaseOrder, po_id).lines if l.product_id == pid)


# ═══ finding the line ════════════════════════════════════════════════

def test_a_sku_finds_its_line(owner_client, db_session):
    a = product(db_session, "V51-PO-A", "V51 Screen")
    b = product(db_session, "V51-PO-B")
    po = make_po(owner_client, db_session, [(a, 6), (b, 2)])
    body = lookup(owner_client, po, "V51-PO-A").json()
    assert body["ok"] is True and body["how"] == "sku"
    assert (body["line_id"], body["name"], body["sku"], body["ordered"]) == (line_id(db_session, po, a), "V51 Screen", "V51-PO-A", 6)


def test_a_manufacturer_barcode_finds_its_line_in_any_spelling(owner_client, db_session):
    a = product(db_session, "V51-PO-C", barcode=UPC_A)
    po = make_po(owner_client, db_session, [(a, 3)])
    for spelling in (UPC_A, EAN_13, "  " + UPC_A + "\r\n"):
        body = lookup(owner_client, po, spelling).json()
        assert body["ok"] is True and body["how"] == "barcode" and body["line_id"] == line_id(db_session, po, a), spelling


def test_the_sku_match_is_not_case_sensitive(owner_client, db_session):
    a = product(db_session, "V51-PO-D")
    po = make_po(owner_client, db_session, [(a, 1)])
    assert lookup(owner_client, po, "v51-po-d").json()["ok"] is True


def test_an_item_that_is_not_on_this_order_is_set_aside(owner_client, db_session):
    on, off = product(db_session, "V51-PO-E"), product(db_session, "V51-PO-F", "V51 Wrong item")
    po = make_po(owner_client, db_session, [(on, 1)])
    body = lookup(owner_client, po, "V51-PO-F").json()
    assert body["ok"] is False and body["found"] is True and "not on" in body["error"] and "V51 Wrong item" in body["error"]
    assert "line_id" not in body and "canLink" not in body


def test_an_unknown_code_can_be_linked(owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-G"), 1)])
    body = lookup(owner_client, po, EAN_B).json()
    assert (body["ok"], body["found"], body["canLink"], body["code"]) == (False, False, True, EAN_B)


def test_when_two_products_share_a_code_the_one_on_the_order_wins(owner_client, db_session):
    other = product(db_session, "V51-PO-H1", barcode=EAN_B)
    mine = product(db_session, "V51-PO-H2", barcode=EAN_B)
    po = make_po(owner_client, db_session, [(mine, 4)])
    body = lookup(owner_client, po, EAN_B).json()
    assert body["ok"] is True and body["line_id"] == line_id(db_session, po, mine)
    assert other != mine


def test_a_code_that_is_too_short_is_refused(owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-I"), 1)])
    for code in ("", "  ", "ab"):
        resp = lookup(owner_client, po, code)
        assert resp.status_code == 400 and resp.json()["ok"] is False


def test_looking_up_changes_nothing(owner_client, db_session):
    a = product(db_session, "V51-PO-J", stock=5)
    po = make_po(owner_client, db_session, [(a, 4)])
    for _ in range(3):
        lookup(owner_client, po, "V51-PO-J")
    db_session.expire_all()
    assert db_session.get(Product, a).stock == 5
    assert db_session.get(PurchaseOrder, po).status == "sent"
    assert all((l.received_qty or 0) == 0 for l in db_session.get(PurchaseOrder, po).lines)


def test_responses_are_never_cached(owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-K"), 1)])
    assert lookup(owner_client, po, "V51-PO-K").headers["cache-control"] == "no-store"


# ═══ which orders can be received ════════════════════════════════════

def test_only_a_sent_order_can_be_received(owner_client, db_session):
    a = product(db_session, "V51-PO-L")
    draft = make_po(owner_client, db_session, [(a, 2)], send=False)
    resp = lookup(owner_client, draft, "V51-PO-L")
    assert resp.status_code == 409 and "draft" in resp.json()["error"]
    sent = make_po(owner_client, db_session, [(a, 2)])
    assert lookup(owner_client, sent, "V51-PO-L").status_code == 200
    line = db_session.get(PurchaseOrder, sent).lines[0]
    owner_client.post(f"/purchase-orders/{sent}/receive", data={f"received_{line.id}": "2"})
    resp = lookup(owner_client, sent, "V51-PO-L")
    assert resp.status_code == 409 and "received" in resp.json()["error"]


def test_a_missing_order_is_a_404(owner_client):
    resp = lookup(owner_client, "no-such-order", "V51-PO-X")
    assert resp.status_code == 404 and resp.json()["ok"] is False


# ═══ who may ask ═════════════════════════════════════════════════════

def test_signed_out_is_a_401(anon_client, owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-M"), 1)])
    resp = lookup(anon_client, po, "V51-PO-M")
    assert resp.status_code == 401 and resp.json()["ok"] is False


def test_cashier_and_technician_are_refused_and_a_manager_is_allowed(cashier_client, technician_client, manager_client, owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-N"), 1)])
    for client in (cashier_client, technician_client):
        resp = lookup(client, po, "V51-PO-N")
        assert resp.status_code == 403 and "name" not in resp.json()
    assert lookup(manager_client, po, "V51-PO-N").json()["ok"] is True


# ═══ the page ════════════════════════════════════════════════════════

def test_a_sent_order_page_has_the_scan_controls_and_defaults_to_the_full_quantity(owner_client, db_session):
    a, b = product(db_session, "V51-PO-P"), product(db_session, "V51-PO-Q")
    po = make_po(owner_client, db_session, [(a, 6), (b, 2)])
    page = owner_client.get(f"/purchase-orders/{po}").text
    assert 'id="po-recv"' in page and f'data-po="{po}"' in page
    assert "Scan items as they arrive" in page and 'id="po-scan-start"' in page and 'id="po-scan-reset"' in page
    assert "porecv.js" in page and re.search(r"scan\.js\?v=\d+", page)
    la, lb = line_id(db_session, po, a), line_id(db_session, po, b)
    assert f'name="received_{la}" data-line="{la}" data-ordered="6"' in page and f'name="received_{lb}" data-line="{lb}" data-ordered="2"' in page
    assert 'value="6"' in page and 'value="2"' in page and "Confirm Receipt" in page


def test_a_draft_and_a_received_order_have_no_scan_controls(owner_client, db_session):
    a = product(db_session, "V51-PO-R")
    draft = make_po(owner_client, db_session, [(a, 1)], send=False)
    assert "po-scan-start" not in owner_client.get(f"/purchase-orders/{draft}").text
    sent = make_po(owner_client, db_session, [(a, 1)])
    line = db_session.get(PurchaseOrder, sent).lines[0]
    owner_client.post(f"/purchase-orders/{sent}/receive", data={f"received_{line.id}": "1"})
    assert "po-scan-start" not in owner_client.get(f"/purchase-orders/{sent}").text


def test_receiving_what_was_scanned_adds_exactly_that_to_stock(owner_client, db_session):
    """The page posts the counted numbers in the same form as before, so a short delivery adds only what arrived."""
    a, b = product(db_session, "V51-PO-S", stock=5), product(db_session, "V51-PO-T", stock=1)
    po = make_po(owner_client, db_session, [(a, 6), (b, 2)])
    la, lb = line_id(db_session, po, a), line_id(db_session, po, b)
    resp = owner_client.post(f"/purchase-orders/{po}/receive", data={f"received_{la}": "4", f"received_{lb}": "0"}, follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert (db_session.get(Product, a).stock, db_session.get(Product, b).stock) == (9, 1)
    assert db_session.get(PurchaseOrder, po).status == "received"


def test_the_purchasing_pages_still_need_manager_rights(cashier_client, owner_client, db_session):
    po = make_po(owner_client, db_session, [(product(db_session, "V51-PO-U"), 1)])
    assert cashier_client.get(f"/purchase-orders/{po}").status_code in (302, 303, 403)
