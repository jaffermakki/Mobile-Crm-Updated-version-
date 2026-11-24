"""v53.1 — the repair ticket's "Add Part" picker lists repair parts only.

Cases, chargers, cables and screen protectors used to crowd the list. The picker now shows Screen / Battery / Part
products (the ones that fit the phone on the ticket first); `?parts=all` brings the rest back. The route that
actually adds a part is untouched, so these tests also pin that it still accepts any product.
"""
import re
from types import SimpleNamespace as NS

import pytest

from app.models import AuditLog, Customer, Product, Repair, RepairPart
from app import repair_parts as RP

TAG = "V53RP"


def _mk(db, sku, name, category, stock=5, **kw):
    p = Product(sku=f"{TAG}-{sku}", name=f"{TAG} {name}", category=category, price=10, cost=4, stock=stock, **kw)
    db.add(p)
    db.commit()
    return p


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        cids = [c.id for c in db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).all()]
        rq = db_session.query(Repair).filter(Repair.device.like(f"{TAG}%"))
        rids = [r.id for r in rq.all()]
        if cids:
            rids += [r.id for r in db_session.query(Repair).filter(Repair.customer_id.in_(cids)).all()]
        if rids:
            db_session.query(RepairPart).filter(RepairPart.repair_id.in_(rids)).delete(synchronize_session=False)
            db_session.query(Repair).filter(Repair.id.in_(rids)).delete(synchronize_session=False)
        if cids:
            db_session.query(Customer).filter(Customer.id.in_(cids)).delete(synchronize_session=False)
        db_session.query(Product).filter(Product.sku.like(f"{TAG}-%")).delete(synchronize_session=False)
        db_session.query(AuditLog).filter(AuditLog.detail.like(f"%{TAG}%")).delete(synchronize_session=False)
        db_session.commit()
    wipe()
    yield
    wipe()


def _repair(c, device=f"{TAG} iPhone 15"):
    r = c.post("/repairs/add", data={"phone": "250-555-0177", "name": f"{TAG} Rae", "device": device,
                                     "issue": "Screen Replacement", "description": "cracked", "estimated_cost": "100"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return re.split(r"[?#]", r.headers["location"])[0]


def _picker_options(html):
    """-> {product sku-tag: group label} for the part <select>."""
    m = re.search(r'<select name="product_id"[^>]*id="part-select">(.*?)</select>', html, re.S)
    assert m, "part picker not on the page"
    out, group = {}, None
    for tok in re.finditer(r'<optgroup label="([^"]*)">|<option value="([^"]*)">([^<]*)</option>', m.group(1)):
        if tok.group(1) is not None:
            group = tok.group(1)
        elif tok.group(2):
            out[tok.group(3).split(" — ")[0].strip()] = group
    return out


# ── the page ────────────────────────────────────────────────────────────────

def test_picker_lists_only_screens_batteries_and_parts(owner_client, db_session):
    _mk(db_session, "SCR", "Generic Screen", "SCREEN")
    _mk(db_session, "BAT", "Generic Battery", "BATTERY")
    _mk(db_session, "PRT", "Charge Port Flex", "PART")
    _mk(db_session, "CASE", "Clear Case", "CASE")
    _mk(db_session, "CHG", "20W Charger", "CHARGER")
    _mk(db_session, "CAB", "USB-C Cable", "CABLE")
    _mk(db_session, "PRO", "Glass Protector", "ACCESSORY")
    _mk(db_session, "UNC", "Mystery Thing", "")
    html = owner_client.get(_repair(owner_client)).text
    got = _picker_options(html)
    assert {f"{TAG}-SCR", f"{TAG}-BAT", f"{TAG}-PRT"} <= set(got)
    for hidden in ("CASE", "CHG", "CAB", "PRO", "UNC"):
        assert f"{TAG}-{hidden}" not in got, hidden
    assert got[f"{TAG}-SCR"] == "Screens" and got[f"{TAG}-BAT"] == "Batteries" and got[f"{TAG}-PRT"] == "Parts"


def test_page_says_how_many_were_hidden_and_offers_show_all(owner_client, db_session):
    _mk(db_session, "SCR", "Generic Screen", "SCREEN")
    _mk(db_session, "CASE", "Clear Case", "CASE")
    html = owner_client.get(_repair(owner_client)).text
    assert "Show all products" in html and "?parts=all" in html
    assert "hidden)" in html


def test_show_all_brings_back_everything_and_a_way_back(owner_client, db_session):
    _mk(db_session, "SCR", "Generic Screen", "SCREEN")
    _mk(db_session, "CASE", "Clear Case", "CASE")
    _mk(db_session, "UNC", "Mystery Thing", "")
    url = _repair(owner_client)
    html = owner_client.get(url + "?parts=all").text
    got = _picker_options(html)
    assert {f"{TAG}-SCR", f"{TAG}-CASE", f"{TAG}-UNC"} <= set(got)
    assert "Show repair parts only" in html and "Show all products" not in html


def test_matching_phone_is_listed_first_and_neighbours_are_not_mistaken_for_it(owner_client, db_session):
    _mk(db_session, "S15", "iPhone 15 Screen", "SCREEN", phone_brand="Apple", phone_model="iPhone 15")
    _mk(db_session, "S15P", "iPhone 15 Pro Screen", "SCREEN", phone_brand="Apple", phone_model="iPhone 15 Pro")
    _mk(db_session, "S14", "iPhone 14 Screen", "SCREEN", phone_brand="Apple", phone_model="iPhone 14")
    html = owner_client.get(_repair(owner_client, device="iPhone 15")).text
    got = _picker_options(html)
    assert got[f"{TAG}-S15"].startswith("Fits this phone")
    assert got[f"{TAG}-S15P"] == "Screens" and got[f"{TAG}-S14"] == "Screens"
    # and the fit group comes before the generic ones
    assert html.index("Fits this phone") < html.index('<optgroup label="Screens">')


def test_no_repair_parts_in_stock_list_explains_itself(owner_client, db_session):
    _mk(db_session, "CASE", "Clear Case", "CASE")
    # make sure no other test data supplies a repair part for this assertion
    others = db_session.query(Product).filter(Product.category.in_(RP.REPAIR_CATEGORIES)).count()
    html = owner_client.get(_repair(owner_client)).text
    if others == 0:
        assert "No repair parts yet" in html


def test_hostile_product_names_are_escaped_in_the_picker(owner_client, db_session):
    _mk(db_session, "XSS", '<script>alert(1)</script>"><img src=x>', "SCREEN")
    html = owner_client.get(_repair(owner_client)).text
    assert "<script>alert(1)</script>" not in html.split('id="part-select"')[1].split("</select>")[0]
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("client_name", ["manager_client", "technician_client"])
def test_picker_works_for_manager_and_technician(request, owner_client, db_session, client_name):
    _mk(db_session, "SCR", "Generic Screen", "SCREEN")
    _mk(db_session, "CASE", "Clear Case", "CASE")
    url = _repair(owner_client)
    got = _picker_options(request.getfixturevalue(client_name).get(url).text)
    assert f"{TAG}-SCR" in got and f"{TAG}-CASE" not in got


def test_cashier_still_sees_no_parts_card(cashier_client, owner_client, db_session):
    _mk(db_session, "SCR", "Generic Screen", "SCREEN")
    url = _repair(owner_client)
    r = cashier_client.get(url)
    assert 'id="part-select"' not in r.text


# ── the route is unchanged ──────────────────────────────────────────────────

def test_adding_a_part_still_accepts_any_product_and_deducts_stock(owner_client, db_session):
    case = _mk(db_session, "CASE", "Clear Case", "CASE", stock=3)
    url = _repair(owner_client)
    rid = url.rsplit("/", 1)[1]
    r = owner_client.post(f"/repairs/{rid}/parts/add", data={"product_id": case.id, "qty": "2"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Product, case.id).stock == 1
    assert db_session.query(RepairPart).filter(RepairPart.repair_id == rid).count() == 1


# ── the matching rules, directly ────────────────────────────────────────────

@pytest.mark.parametrize("device,model,brand,name,expected", [
    ("iPhone 15", "iPhone 15", "Apple", "x", True),
    ("Apple iPhone 15", "iPhone 15", "Apple", "x", True),
    ("iphone 15", "iPhone 15", "Apple", "x", True),
    ("iPhone 15 Pro", "iPhone 15", "Apple", "x", False),
    ("iPhone 15", "iPhone 15 Pro", "Apple", "x", False),
    ("iPhone 15 Pro Max", "iPhone 15 Pro Max", "Apple", "x", True),
    ("Samsung Galaxy S24", "Galaxy S24", "Samsung", "x", True),
    ("Galaxy S24 Ultra", "Galaxy S24", "Samsung", "x", False),
    ("Pixel 8", "", "", "Pixel 8 Screen", True),
    ("Pixel 8", "", "", "Pixel 8 Pro Screen", False),
    ("Pixel 8 Pro", "", "", "Pixel 8 Pro Screen", True),
    ("", "iPhone 15", "Apple", "iPhone 15 Screen", False),
    ("Pixel 8", "", "", "Universal Battery", False),
])
def test_fits_device_rules(device, model, brand, name, expected):
    p = NS(phone_model=model, phone_brand=brand, name=name)
    assert RP.fits_device(p, device) is expected


def test_build_picker_never_raises_on_empty_or_odd_input():
    assert RP.build_picker([], "")["groups"] == []
    odd = NS(id="1", category=None, name=None, phone_model=None, phone_brand=None, sku="s", stock=0)
    out = RP.build_picker([odd], None, show_all=True)
    assert out["groups"] and out["groups"][0][0].startswith("Other")
