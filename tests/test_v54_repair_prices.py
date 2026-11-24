"""v54 — the repair price menu (item 1).

The owner keeps "this phone + this repair = this price, with this part". These tests pin: who can change it, exactly which
phone a line matches, that the menu only ever FILLS IN boxes (no ticket, stock or money rule changes), the part suggestion on
the ticket, and the CSV import/export (including bad rows and formula injection).
"""
import io
import re

import pytest

from app import repair_prices as RPR
from app.models import AuditLog, Customer, Product, Repair, RepairPart, RepairPrice

TAG = "V54RP"


def _db():
    from app.database import SessionLocal
    return SessionLocal()


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        db_session.query(RepairPrice).delete(synchronize_session=False)
        cids = [c.id for c in db_session.query(Customer).filter(Customer.name.like(f"{TAG}%")).all()]
        rids = [r.id for r in db_session.query(Repair).filter(Repair.device.like(f"%{TAG}%")).all()]
        if cids:
            rids += [r.id for r in db_session.query(Repair).filter(Repair.customer_id.in_(cids)).all()]
        if rids:
            db_session.query(RepairPart).filter(RepairPart.repair_id.in_(rids)).delete(synchronize_session=False)
            db_session.query(Repair).filter(Repair.id.in_(rids)).delete(synchronize_session=False)
        if cids:
            db_session.query(Customer).filter(Customer.id.in_(cids)).delete(synchronize_session=False)
        db_session.query(Product).filter(Product.sku.like(f"{TAG}-%")).delete(synchronize_session=False)
        db_session.query(AuditLog).filter(AuditLog.detail.like("%V54RP%")).delete(synchronize_session=False)
        db_session.commit()
    wipe()
    yield
    wipe()


def _add(c, device="", repair="Screen Replacement", price="100", sku="", minutes="", any_phone=False):
    data = {"repair": repair, "price": price, "sku": sku, "minutes": minutes}
    if any_phone:
        data["model_any"] = "1"
    else:
        data["device"] = device
    return c.post("/repair-prices/add", data=data, follow_redirects=False)


def _menu(c, device):
    r = c.get("/repair-menu", params={"device": device})
    assert r.status_code == 200, r.text
    return {i["repair"]: i for i in r.json()["items"]}


def _product(db, sku, name, stock=5, category="SCREEN"):
    p = Product(sku=f"{TAG}-{sku}", name=f"{TAG} {name}", category=category, price=50, cost=20, stock=stock)
    db.add(p)
    db.commit()
    return p


def _ticket(c, device, issue="Screen Replacement", name=f"{TAG} Quinn"):
    r = c.post("/repairs/add", data={"phone": "250-555-0154", "name": name, "device": device, "issue": issue,
                                     "description": "x", "estimated_cost": "10"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return re.split(r"[?#]", r.headers["location"])[0]


# ── who may do what ─────────────────────────────────────────────────────────

def test_only_owner_can_change_prices(owner_client, manager_client, cashier_client, technician_client, anon_client, db_session):
    assert _add(owner_client, f"{TAG} Phone", price="99").status_code == 303
    row = db_session.query(RepairPrice).one()
    for c in (manager_client, cashier_client, technician_client):
        assert _add(c, f"{TAG} Other").status_code == 403
        assert c.post(f"/repair-prices/{row.id}/save", data={"repair": "Screen Replacement", "price": "1"}).status_code == 403
        assert c.post(f"/repair-prices/{row.id}/delete").status_code == 403
        assert c.post("/repair-prices/import", files={"file": ("a.csv", b"Repair,Price\nX,1\n", "text/csv")}).status_code == 403
    db_session.expire_all()
    assert db_session.query(RepairPrice).count() == 1 and db_session.query(RepairPrice).one().price == 99
    a = anon_client.post("/repair-prices/add", data={"device": "x", "repair": "y", "price": "1"}, follow_redirects=False)
    assert a.status_code in (303, 401, 403) and db_session.query(RepairPrice).count() == 1


def test_who_can_look_at_the_list(owner_client, manager_client, cashier_client, technician_client, anon_client):
    _add(owner_client, f"{TAG} Phone", price="99")
    assert owner_client.get("/repair-prices").status_code == 200
    m = manager_client.get("/repair-prices")
    assert m.status_code == 200 and "Only the owner can change it" in m.text and 'action="/repair-prices/add"' not in m.text
    assert 'action="/repair-prices/' not in m.text.replace('action="/repair-prices"', "")      # no edit forms for a manager
    for c in (cashier_client, technician_client):
        assert c.get("/repair-prices").status_code == 403
        assert c.get("/repair-prices/export.csv").status_code == 403
    assert anon_client.get("/repair-prices", follow_redirects=False).status_code == 303
    assert manager_client.get("/repair-prices/export.csv").status_code == 200


def test_menu_lookup_needs_login_and_works_for_every_role(owner_client, manager_client, cashier_client, technician_client, anon_client):
    _add(owner_client, f"{TAG} Pixel", price="75")
    for c in (owner_client, manager_client, cashier_client, technician_client):
        assert "Screen Replacement" in _menu(c, f"{TAG} Pixel 128GB")
    r = anon_client.get("/repair-menu", params={"device": f"{TAG} Pixel"})
    assert r.status_code == 401 and r.json()["ok"] is False
    assert owner_client.get("/repair-menu", params={"device": f"{TAG} Pixel"}).headers["cache-control"] == "no-store"


# ── matching the phone ──────────────────────────────────────────────────────

def test_exact_phone_beats_any_phone_and_other_models_never_match(owner_client):
    _add(owner_client, any_phone=True, repair="Screen Replacement", price="80")
    _add(owner_client, "iPhone 15", price="150")
    _add(owner_client, "iPhone 15 Pro", price="220")
    _add(owner_client, "Galaxy S24", price="140")
    _add(owner_client, "Galaxy S24 Ultra", price="260")
    assert _menu(owner_client, "iPhone 15, 128GB, Blue")["Screen Replacement"]["price"] == 150
    assert _menu(owner_client, "Apple iPhone 15 Pro Max")["Screen Replacement"]["price"] == 80     # Pro Max has no line: any-phone price
    assert _menu(owner_client, "iPhone 15 Pro 256GB")["Screen Replacement"]["price"] == 220
    assert _menu(owner_client, "Samsung Galaxy S24+")["Screen Replacement"]["price"] == 80        # S24+ is not S24
    assert _menu(owner_client, "Galaxy S24")["Screen Replacement"]["price"] == 140
    assert _menu(owner_client, "Galaxy S24 Ultra")["Screen Replacement"]["price"] == 260
    assert _menu(owner_client, "Nokia 3310")["Screen Replacement"]["price"] == 80


def test_no_line_means_empty_menu_not_an_error(owner_client):
    _add(owner_client, "iPhone 15", price="150")
    assert _menu(owner_client, "Pixel 8") == {}
    assert _menu(owner_client, "") == {}
    r = owner_client.get("/repair-menu")
    assert r.status_code == 200 and r.json()["items"] == []


def test_menu_lists_one_line_per_repair_in_usual_order(owner_client):
    _add(owner_client, "iPhone 15", repair="Charging Port", price="70")
    _add(owner_client, "iPhone 15", repair="Screen Replacement", price="150")
    _add(owner_client, "iPhone 15", repair="Zebra Fix", price="5")
    _add(owner_client, "iPhone 15", repair="Battery Replacement", price="80")
    items = owner_client.get("/repair-menu", params={"device": "iPhone 15"}).json()["items"]
    assert [i["repair"] for i in items] == ["Screen Replacement", "Battery Replacement", "Charging Port", "Zebra Fix"]


def test_hostile_text_in_names_is_escaped_everywhere(owner_client):
    evil = '<script>alert(1)</script>'
    _add(owner_client, f"{TAG} {evil}", repair=f"Fix {evil}", price="9")
    page = owner_client.get("/repair-prices").text
    assert evil not in page and "&lt;script&gt;" in page
    issue_list = owner_client.get("/repairs").text
    assert evil not in issue_list


# ── adding, editing, removing ───────────────────────────────────────────────

def test_add_edit_delete_round_trip_with_audit(owner_client, db_session):
    p = _product(db_session, "SCR1", "Screen Assembly")
    assert _add(owner_client, f"{TAG} Pixel 8", price="129.5", sku=p.sku.lower(), minutes="45").status_code == 303
    row = db_session.query(RepairPrice).one()
    assert row.price == 129.5 and row.minutes == 45 and row.product_id == p.id and row.model == f"{TAG} Pixel 8"
    r = owner_client.post(f"/repair-prices/{row.id}/save", data={"repair": "Screen Replacement", "price": "139", "sku": p.sku, "minutes": "40"},
                          follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    row = db_session.get(RepairPrice, row.id)
    assert row.price == 139 and row.minutes == 40 and row.updated_by
    details = [a.detail for a in db_session.query(AuditLog).filter(AuditLog.action.in_(["PRICE_ADD", "PRICE_CHANGE", "PRICE_DELETE"])).all()]
    assert any("PRICE" not in d and "$129.50" in d for d in details) and any("$129.50 → $139.00" in d for d in details)
    assert owner_client.post(f"/repair-prices/{row.id}/delete", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.query(RepairPrice).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.action == "PRICE_DELETE").count() >= 1


def test_clearing_the_part_and_minutes_on_a_line(owner_client, db_session):
    p = _product(db_session, "SCR2", "Screen")
    _add(owner_client, f"{TAG} Pixel", price="100", sku=p.sku, minutes="30")
    row = db_session.query(RepairPrice).one()
    owner_client.post(f"/repair-prices/{row.id}/save", data={"repair": "Screen Replacement", "price": "100", "sku": "", "minutes": ""})
    db_session.expire_all()
    row = db_session.get(RepairPrice, row.id)
    assert row.product_id == "" and row.minutes is None


@pytest.mark.parametrize("price", ["", "abc", "-5", "99999999", "NaN", "inf", "-inf", "1e308", "$"])
def test_bad_prices_are_refused(owner_client, db_session, price):
    r = _add(owner_client, f"{TAG} Phone", price=price)
    assert r.status_code == 303
    assert db_session.query(RepairPrice).count() == 0


@pytest.mark.parametrize("minutes", ["0", "-3", "abc", "1441", "4.5", "99999"])
def test_bad_minutes_are_refused(owner_client, db_session, minutes):
    _add(owner_client, f"{TAG} Phone", price="10", minutes=minutes)
    assert db_session.query(RepairPrice).count() == 0


def test_zero_price_is_allowed_for_free_checks(owner_client, db_session):
    _add(owner_client, f"{TAG} Phone", repair="Diagnostic", price="0")
    assert db_session.query(RepairPrice).one().price == 0


def test_unknown_sku_and_blank_names_and_missing_phone_are_refused(owner_client, db_session):
    assert _add(owner_client, f"{TAG} Phone", price="10", sku="NOPE-NOT-A-SKU").status_code == 303
    assert _add(owner_client, f"{TAG} Phone", repair="", price="10").status_code == 303
    assert _add(owner_client, "", price="10").status_code == 303
    assert _add(owner_client, f"{TAG} Phone", repair="!!!", price="10").status_code == 303
    assert _add(owner_client, "x" * 200, price="10").status_code == 303
    assert _add(owner_client, f"{TAG} Phone", repair="y" * 200, price="10").status_code == 303
    assert db_session.query(RepairPrice).count() == 0


def test_sku_with_wildcards_does_not_match_by_accident(owner_client, db_session):
    _product(db_session, "ABC", "Screen")
    _add(owner_client, f"{TAG} Phone", price="10", sku="%")
    _add(owner_client, f"{TAG} Phone", price="10", sku="V54RP-%")
    assert db_session.query(RepairPrice).count() == 0


def test_duplicate_phone_and_repair_is_refused_even_with_different_spelling(owner_client, db_session):
    _add(owner_client, "iPhone 15", repair="Screen Replacement", price="100")
    _add(owner_client, "Apple iPhone 15", repair="screen   replacement", price="120")
    _add(owner_client, "IPHONE 15", repair="SCREEN REPLACEMENT", price="130")
    assert db_session.query(RepairPrice).count() == 1 and db_session.query(RepairPrice).one().price == 100


def test_editing_cannot_rename_into_an_existing_line(owner_client, db_session):
    _add(owner_client, "iPhone 15", repair="Screen Replacement", price="100")
    _add(owner_client, "iPhone 15", repair="Battery Replacement", price="80")
    bat = db_session.query(RepairPrice).filter(RepairPrice.repair == "Battery Replacement").one()
    owner_client.post(f"/repair-prices/{bat.id}/save", data={"repair": "screen replacement", "price": "1"})
    db_session.expire_all()
    assert db_session.get(RepairPrice, bat.id).repair == "Battery Replacement" and db_session.get(RepairPrice, bat.id).price == 80


def test_save_or_delete_of_a_missing_line_is_a_friendly_message(owner_client):
    r = owner_client.post("/repair-prices/nope/save", data={"repair": "x", "price": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "no longer exists" in r.text
    r = owner_client.post("/repair-prices/nope/delete", follow_redirects=True)
    assert r.status_code == 200 and "no longer exists" in r.text


def test_deleting_a_part_product_leaves_the_line_working(owner_client, db_session):
    p = _product(db_session, "GONE", "Screen")
    _add(owner_client, f"{TAG} Pixel", price="100", sku=p.sku)
    db_session.delete(p)
    db_session.commit()
    items = _menu(owner_client, f"{TAG} Pixel")
    assert items["Screen Replacement"]["part"] is None
    assert owner_client.get("/repair-prices").status_code == 200


def test_list_page_search_and_empty_states(owner_client):
    page = owner_client.get("/repair-prices").text
    assert "No prices yet" in page
    _add(owner_client, "iPhone 15", price="150")
    _add(owner_client, "Pixel 8", repair="Battery Replacement", price="80")
    assert "iPhone 15" in owner_client.get("/repair-prices", params={"q": "iphone"}).text
    r = owner_client.get("/repair-prices", params={"q": "battery"}).text
    assert "Pixel 8" in r and "iPhone 15" not in r.split('class="card rp-group"', 1)[-1]
    assert "Nothing matches" in owner_client.get("/repair-prices", params={"q": "zzzz"}).text
    assert owner_client.get("/repair-prices", params={"q": "%"}).status_code == 200


# ── the new-ticket form and the ticket page ─────────────────────────────────

def test_new_ticket_form_has_the_chip_area_and_repair_names_join_the_issue_list(owner_client, technician_client):
    _add(owner_client, "iPhone 15", repair="Back Glass Repair", price="120")
    for c in (owner_client, technician_client):
        html = c.get("/repairs").text
        assert 'id="repair-menu"' in html and "/static/repairmenu.js" in html
        assert "Back Glass Repair" in html                       # a custom repair is selectable as the Issue
    assert "Repair prices" in owner_client.get("/repairs").text
    assert "/repair-prices" not in technician_client.get("/repairs").text


def test_owner_sees_a_hint_until_the_list_has_lines(owner_client, manager_client):
    assert "set up your repair prices" in owner_client.get("/repairs").text
    assert "set up your repair prices" not in manager_client.get("/repairs").text
    _add(owner_client, "iPhone 15", price="150")
    assert "set up your repair prices" not in owner_client.get("/repairs").text


def test_ticket_page_suggests_the_usual_part_and_adds_it_once(owner_client, technician_client, db_session):
    p = _product(db_session, "SCR3", "iPhone 15 Screen", stock=3)
    _add(owner_client, f"{TAG} iPhone 15", price="159.99", sku=p.sku, minutes="50")
    url = _ticket(technician_client, f"{TAG} iPhone 15 128GB")
    html = technician_client.get(url).text
    assert 'id="menu-suggest"' in html and "159.99" in html and "about 50 min" in html and p.sku in html
    assert "+ Add this part" in html
    rid = url.rsplit("/", 1)[1]
    r = technician_client.post(f"/repairs/{rid}/parts/add", data={"product_id": p.id, "qty": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Product, p.id).stock == 2
    html = technician_client.get(url).text
    block = re.sub(r"<svg.*?</svg>", "", html.split('id="menu-suggest"', 1)[1][:3000], flags=re.S)
    assert "+ Add this part" not in html and "Added" in block[:700]


def test_ticket_page_without_a_matching_line_has_no_suggestion(owner_client, db_session):
    _add(owner_client, f"{TAG} iPhone 15", price="159.99")
    url = _ticket(owner_client, f"{TAG} iPhone 15 Pro")
    assert 'id="menu-suggest"' not in owner_client.get(url).text
    url2 = _ticket(owner_client, f"{TAG} iPhone 15", issue="Water Damage")
    assert 'id="menu-suggest"' not in owner_client.get(url2).text


def test_menu_never_changes_stock_or_the_ticket_by_itself(owner_client, db_session):
    p = _product(db_session, "SCR4", "Screen", stock=4)
    _add(owner_client, f"{TAG} Pixel", price="99", sku=p.sku)
    url = _ticket(owner_client, f"{TAG} Pixel")
    db_session.expire_all()
    assert db_session.get(Product, p.id).stock == 4
    rep = db_session.query(Repair).filter(Repair.device == f"{TAG} Pixel").one()
    assert rep.estimated_cost == 10                      # exactly what the staff member typed, not the menu price
    assert db_session.query(RepairPart).filter(RepairPart.repair_id == rep.id).count() == 0


def test_out_of_stock_part_is_flagged_and_adding_it_is_still_refused(owner_client, db_session):
    p = _product(db_session, "SCR5", "Screen", stock=0)
    _add(owner_client, f"{TAG} Pixel", price="99", sku=p.sku)
    url = _ticket(owner_client, f"{TAG} Pixel")
    html = owner_client.get(url).text
    assert "0 in stock" in html and "None in stock" in html
    rid = url.rsplit("/", 1)[1]
    owner_client.post(f"/repairs/{rid}/parts/add", data={"product_id": p.id, "qty": "1"})
    db_session.expire_all()
    assert db_session.query(RepairPart).filter(RepairPart.repair_id == rid).count() == 0


# ── CSV ──────────────────────────────────────────────────────────────────────

def _import(c, body, name="p.csv"):
    raw = body if isinstance(body, bytes) else body.encode()
    return c.post("/repair-prices/import", files={"file": (name, raw, "text/csv")})


def test_csv_round_trip_keeps_every_line(owner_client, db_session):
    p = _product(db_session, "SCR6", "Screen")
    _add(owner_client, "iPhone 15", price="150.5", sku=p.sku, minutes="45")
    _add(owner_client, any_phone=True, repair="Battery Replacement", price="79")
    exported = owner_client.get("/repair-prices/export.csv")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    db_session.query(RepairPrice).delete()
    db_session.commit()
    r = _import(owner_client, exported.content)
    assert r.status_code == 200 and "2 added" in r.text and "0 skipped" in r.text
    got = {(x.model, x.repair): (x.price, x.minutes, x.product_id) for x in db_session.query(RepairPrice).all()}
    assert got == {("iPhone 15", "Screen Replacement"): (150.5, 45, p.id), ("", "Battery Replacement"): (79, None, "")}


def test_import_skips_bad_rows_lists_them_and_keeps_the_good_ones(owner_client, db_session):
    body = ("Phone model,Repair,Price (before tax),Part SKU,Minutes\n"
            "Pixel 8,Screen Replacement,150,,30\n"
            "Pixel 8,Battery Replacement,abc,,\n"
            "Pixel 8,,50,,\n"
            "Pixel 8,Charging Port,60,NO-SUCH-SKU,\n"
            "Pixel 8,Screen Replacement,155,,\n"
            ",Camera Repair,90,,15\n")
    r = _import(owner_client, body)
    assert r.status_code == 200 and "2 added" in r.text and "4 skipped" in r.text
    assert "Row 3" in r.text and "Row 4" in r.text and "Row 5" in r.text and "Row 6" in r.text
    assert db_session.query(RepairPrice).count() == 2
    assert db_session.query(RepairPrice).filter(RepairPrice.repair == "Screen Replacement").one().price == 150     # first duplicate wins


def test_import_updates_existing_lines(owner_client, db_session):
    _add(owner_client, "Pixel 8", price="100")
    r = _import(owner_client, "Phone model,Repair,Price\nPixel 8,Screen Replacement,120\n")
    assert "0 added" in r.text and "1 updated" in r.text
    assert db_session.query(RepairPrice).one().price == 120


@pytest.mark.parametrize("body,needle", [
    (b"", "empty"), (b"\n\n", "empty"), (b"just,some,words\n1,2,3\n", "first row must name the columns"),
    (b"\xff\xfe\x00\x01\x02garbage", None), (b"x" * 500_000, "too big")])
def test_import_rejects_unusable_files_without_changing_anything(owner_client, db_session, body, needle):
    _add(owner_client, "Pixel 8", price="100")
    r = _import(owner_client, body)
    assert r.status_code in (200, 400)
    if needle:
        assert needle in r.text
    db_session.expire_all()
    assert db_session.query(RepairPrice).count() == 1 and db_session.query(RepairPrice).one().price == 100


def test_import_understands_excel_style_files(owner_client, db_session):
    body = "﻿Model;Service;Price;Mins\r\nPixel 8;Screen Replacement;1,50;5\r\n".encode("utf-8")        # BOM, semicolons
    r = _import(owner_client, body)
    assert "skipped" in r.text
    tab = "Phone\tRepair\tPrice\niPhone 14\tCharging Port\t69.00\n".encode("cp1252")
    r = _import(owner_client, tab)
    assert "1 added" in r.text
    assert db_session.query(RepairPrice).filter(RepairPrice.model == "iPhone 14").one().price == 69


def test_export_neutralises_spreadsheet_formulas(owner_client):
    _add(owner_client, "iPhone 15", repair="=HYPERLINK(\"http://evil\")", price="1")
    _add(owner_client, "@SUM(A1)", repair="+cmd|calc", price="2")
    text = owner_client.get("/repair-prices/export.csv").text
    for line in text.splitlines()[1:]:
        for cell in line.split(","):
            assert not cell.strip('"').startswith(("=", "+", "@", "-")), line


def test_import_cap_on_total_lines_and_audit(owner_client, db_session, monkeypatch):
    monkeypatch.setattr(RPR, "MAX_LINES", 2)
    r = _import(owner_client, "Phone model,Repair,Price\nA 1,Screen Replacement,1\nA 2,Screen Replacement,1\nA 3,Screen Replacement,1\n")
    assert r.status_code == 400 and db_session.query(RepairPrice).count() == 0
    monkeypatch.setattr(RPR, "MAX_LINES", 5000)
    _import(owner_client, "Phone model,Repair,Price\nA 1,Screen Replacement,1\n")
    assert db_session.query(AuditLog).filter(AuditLog.action == "PRICE_IMPORT").count() >= 1


def test_prices_survive_a_backup_round_trip(owner_client, db_session):
    from app import backup as B
    _add(owner_client, "iPhone 15", price="150")
    doc = B.build_doc(db_session, include_secrets=False)
    assert "repair_prices" in doc["tables"] and doc["tables"]["repair_prices"][0]["price"] == 150


def test_the_price_list_page_loads_the_phone_picker_script_and_the_parts_table_is_phone_friendly(owner_client):
    """Found in the browser run: the Brand/Model picker on /repair-prices stayed empty because devicepick.js was not loaded."""
    page = owner_client.get("/repair-prices").text
    assert "/static/devicepick.js" in page and "/static/repairprices.js" in page
    assert page.index("devicepick.js") < page.index("repairprices.js")
    css = owner_client.get("/static/style.css").text
    assert "grid-template-columns:minmax(0,1fr)" in css and ".parts-table" in css
