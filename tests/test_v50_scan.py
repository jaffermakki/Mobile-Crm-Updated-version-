"""
v50 — manufacturer barcodes on products, camera scanning (the server side of it) and stock-count mode.

Runs on SQLite and on PostgreSQL. The camera itself is exercised separately in a real browser with a fake camera
feed; these tests cover everything the server decides.
"""
import csv
import io
import re
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

from app import backup as B
from app import barcodes as BC
from app import stockcount as SC
from app.database import engine
from app.models import AuditLog, Invoice, Product, StockCount, StockCountLine

ROOT = Path(__file__).resolve().parent.parent

# real, valid check digits
UPC_A = "036000291452"       # UPC-A (12)
EAN_13 = "0036000291452"     # the same number as EAN-13 (leading 0)
GTIN_14 = "00036000291452"   # ... and as GTIN-14
EAN_B = "4006381333931"      # a different, valid EAN-13
EAN_C = "5012345678900"      # another valid EAN-13


# ═══ helpers ═════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean(db_session):
    yield
    db_session.rollback()
    db_session.query(StockCountLine).delete(synchronize_session=False)
    db_session.query(StockCount).delete(synchronize_session=False)
    db_session.query(Product).filter(Product.sku.like("V50-%")).delete(synchronize_session=False)
    db_session.commit()
    db_session.expire_all()


def mk(db, sku, name=None, barcode="", stock=10, cost=2.0, price=5.0, category="cases"):
    product = Product(sku=sku, name=name or f"V50 {sku}", barcode=barcode, stock=stock, cost=cost, price=price, category=category)
    db.add(product)
    db.commit()
    return product.id


def stock_of(db, product_id):
    db.expire_all()
    return db.get(Product, product_id).stock


def new_count(client, name="V50 count", scope=""):
    resp = client.post("/stock-count/start", data={"name": name, "scope": scope}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp.headers["location"].rsplit("/", 1)[-1]


def scan(client, count_id, code, qty=1):
    return client.post(f"/stock-count/{count_id}/scan", data={"code": code, "qty": str(qty)})


def line(client, count_id, product_id, mode="add", qty=1):
    return client.post(f"/stock-count/{count_id}/line", data={"product_id": product_id, "mode": mode, "qty": str(qty)})


def audit_actions(db):
    db.expire_all()
    return [a.action for a in db.query(AuditLog).order_by(AuditLog.ts).all()]


# ═══ the code helpers ═════════════════════════════════════════════════

def test_clean_strips_spaces_breaks_and_control_characters():
    assert BC.clean("  036000 291452\r\n") == UPC_A
    assert BC.clean("ab\x00c\x1f d") == "abcd"
    assert BC.clean(None) == ""
    assert len(BC.clean("1" * 500)) == BC.MAX_LEN


def test_upc_a_ean_13_and_gtin_14_are_the_same_number():
    assert set(BC.variants(UPC_A)) == {UPC_A, EAN_13, GTIN_14}
    assert set(BC.variants(EAN_13)) == {UPC_A, EAN_13, GTIN_14}
    assert set(BC.variants(GTIN_14)) == {UPC_A, EAN_13, GTIN_14}


def test_a_real_ean_13_only_gains_its_gtin_14_spelling():
    assert set(BC.variants(EAN_B)) == {EAN_B, "0" + EAN_B}


def test_short_codes_and_skus_are_matched_exactly():
    assert BC.variants("V50-ABC") == ["V50-ABC"]
    assert BC.variants("12345678") == ["12345678"]         # EAN-8 stays as it is
    assert BC.variants("") == []


def test_check_digit_catches_a_misread():
    assert BC.check_digit_ok(UPC_A) is True
    assert BC.check_digit_ok(EAN_B) is True
    assert BC.check_digit_ok("036000291453") is False
    assert BC.check_digit_ok("96385074") is True              # EAN-8
    assert BC.check_digit_ok("ABC-123") is None
    assert BC.check_digit_ok("12345") is None


def test_sku_beats_barcode_and_matching_is_case_insensitive(db_session):
    sku_id = mk(db_session, "V50-" + UPC_A, "V50 by sku")
    other = mk(db_session, "V50-OTHER", "V50 by barcode", barcode=UPC_A)
    products, how = BC.find_products(db_session, "v50-" + UPC_A)
    assert how == "sku" and [p.id for p in products] == [sku_id]
    products, how = BC.find_products(db_session, UPC_A)
    assert how == "barcode" and [p.id for p in products] == [other]


def test_barcode_found_in_any_spelling(db_session):
    pid = mk(db_session, "V50-A", barcode=UPC_A)
    for spelling in (UPC_A, EAN_13, GTIN_14, f" {EAN_13}\n"):
        products, how = BC.find_products(db_session, spelling)
        assert how == "barcode" and [p.id for p in products] == [pid], spelling
    assert BC.find_products(db_session, "000000000000") == ([], "")
    assert BC.find_products(db_session, "") == ([], "")


def test_conflict_looks_at_barcodes_and_skus_but_not_the_product_itself(db_session):
    a = mk(db_session, "V50-A", barcode=EAN_B)
    b = mk(db_session, "V50-B")
    assert BC.conflict(db_session, EAN_B, b).id == a
    assert BC.conflict(db_session, EAN_B, a) is None
    assert BC.conflict(db_session, "v50-a", b).id == a            # a SKU counts too
    assert BC.conflict(db_session, EAN_C, b) is None
    assert BC.conflict(db_session, "", b) is None
    mk(db_session, "V50-C", barcode=UPC_A)
    assert BC.conflict(db_session, EAN_13, b) is not None         # another spelling of a taken number


# ═══ the register ══════════════════════════════════════════════════════

def pos_scan(client, code, ajax=True):
    return client.post("/pos/scan", data={"sku": code}, headers={"X-Pos-Ajax": "1"} if ajax else {})


def test_register_finds_a_product_by_its_manufacturer_barcode(owner_client, db_session):
    pid = mk(db_session, "V50-CASE", "V50 Clear case", barcode=UPC_A, price=19.99)
    for spelling in (UPC_A, EAN_13):
        data = pos_scan(owner_client, spelling).json()
        assert data["found"] is True and data["how"] == "barcode" and data["product"]["pid"] == pid, spelling
        assert data["shared"] is False
    data = pos_scan(owner_client, "v50-case").json()
    assert data["found"] and data["how"] == "sku"


def test_register_unknown_code_comes_back_with_the_code_for_linking(owner_client):
    data = pos_scan(owner_client, " 7612345678900 ").json()
    assert data["found"] is False and data["code"] == "7612345678900"
    assert "No product found" in data["error"]


def test_register_without_javascript_still_adds_the_barcode_item_to_the_cart(owner_client, db_session):
    mk(db_session, "V50-CASE", "V50 Clear case", barcode=UPC_A, price=19.99)
    resp = pos_scan(owner_client, EAN_13, ajax=False)
    assert resp.status_code == 200 and "V50 Clear case" in resp.text


def test_register_scan_needs_a_signed_in_user(anon_client):
    resp = anon_client.post("/pos/scan", data={"sku": UPC_A}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/login")


def test_register_flags_a_code_two_products_share(owner_client, db_session):
    mk(db_session, "V50-ONE", "V50 One", barcode=EAN_B)
    # an older import may have left two products with one code; the register still answers, and says so
    db_session.add(Product(sku="V50-TWO", name="V50 Two", barcode=EAN_B, stock=1, price=1))
    db_session.commit()
    data = pos_scan(owner_client, EAN_B).json()
    assert data["found"] is True and data["shared"] is True


# ═══ barcodes on the product pages ══════════════════════════════════════

def add_product(client, sku, barcode="", **extra):
    data = {"sku": sku, "name": f"V50 {sku}", "category": "cases", "price": "9.5", "cost": "3", "stock": "4", "barcode": barcode}
    data.update(extra)
    return client.post("/products/add", data=data, follow_redirects=False)


def test_add_product_with_a_barcode(owner_client, db_session):
    assert add_product(owner_client, "V50-NEW", f" {EAN_13} ").status_code == 303
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == "V50-NEW").one().barcode == EAN_13


def test_add_product_without_a_barcode_still_works(owner_client, db_session):
    add_product(owner_client, "V50-PLAIN")
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == "V50-PLAIN").one().barcode == ""


def test_add_product_refuses_a_barcode_that_is_already_used(owner_client, db_session):
    mk(db_session, "V50-TAKEN", barcode=UPC_A)
    resp = add_product(owner_client, "V50-DUP", EAN_13)          # the same number, spelled as EAN-13
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == "V50-DUP").count() == 0
    page = owner_client.get("/products")
    assert "already on" in page.text and "V50 V50-TAKEN" in page.text


def test_add_product_refuses_a_barcode_equal_to_another_products_sku(owner_client, db_session):
    mk(db_session, "V50-SKU1")
    add_product(owner_client, "V50-DUP2", "v50-sku1")
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == "V50-DUP2").count() == 0


def test_cashier_cannot_add_products_or_link_barcodes(cashier_client, db_session):
    assert add_product(cashier_client, "V50-NOPE", UPC_A).status_code == 403
    pid = mk(db_session, "V50-X")
    resp = cashier_client.post(f"/products/{pid}/barcode", data={"barcode": UPC_A}, headers={"X-Scan-Ajax": "1"})
    assert resp.status_code == 403 and resp.json()["ok"] is False
    assert stock_of(db_session, pid) == 10
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == ""


def test_edit_page_shows_the_barcode_and_saving_changes_it(owner_client, db_session):
    pid = mk(db_session, "V50-EDIT", barcode=EAN_B)
    page = owner_client.get(f"/products/{pid}/edit")
    assert page.status_code == 200 and f'value="{EAN_B}"' in page.text and 'data-scan-fill="ep-barcode"' in page.text
    assert 'name="has_barcode"' in page.text
    form = {"name": "V50 Edited", "sku": "V50-EDIT", "category": "cases", "subcategory": "Generic", "variant_group": "",
            "price": "5", "cost": "2", "stock": "10", "reorder_threshold": "5", "reorder_qty": "10", "barcode": EAN_C, "has_barcode": "1"}
    assert owner_client.post(f"/products/{pid}/edit", data=form, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == EAN_C
    form["barcode"], form["has_barcode"] = "", "1"               # clearing it is allowed (the page sends the marker)
    owner_client.post(f"/products/{pid}/edit", data=form, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == ""


def test_an_old_form_without_the_barcode_field_leaves_it_alone(owner_client, db_session):
    pid = mk(db_session, "V50-OLDFORM", barcode=EAN_B)
    form = {"name": "V50 Renamed", "sku": "V50-OLDFORM", "category": "cases", "subcategory": "", "variant_group": "",
            "price": "5", "cost": "2", "stock": "10", "reorder_threshold": "5", "reorder_qty": "10"}
    owner_client.post(f"/products/{pid}/edit", data=form, follow_redirects=False)
    db_session.expire_all()
    p = db_session.get(Product, pid)
    assert p.name == "V50 Renamed" and p.barcode == EAN_B


def test_edit_refuses_a_barcode_used_elsewhere_and_says_why(owner_client, db_session):
    mk(db_session, "V50-HAS", "V50 Has it", barcode=EAN_B)
    pid = mk(db_session, "V50-WANTS")
    form = {"name": "V50 Wants", "sku": "V50-WANTS", "category": "cases", "subcategory": "", "variant_group": "",
            "price": "9", "cost": "2", "stock": "10", "reorder_threshold": "5", "reorder_qty": "10", "barcode": EAN_B}
    resp = owner_client.post(f"/products/{pid}/edit", data=form, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/products/{pid}/edit")
    page = owner_client.get(f"/products/{pid}/edit")
    assert "Not saved" in page.text and "V50 Has it" in page.text
    db_session.expire_all()
    p = db_session.get(Product, pid)
    assert p.barcode == "" and p.price == 5.0                    # nothing else was saved either
    assert "Not saved" not in owner_client.get(f"/products/{pid}/edit").text     # the message shows once


def test_product_search_finds_by_barcode_in_any_spelling(owner_client, db_session):
    mk(db_session, "V50-FIND", "V50 Findable", barcode=UPC_A)
    assert "V50 Findable" in owner_client.get("/products", params={"q": EAN_13}).text
    assert "V50 Findable" in owner_client.get("/products", params={"q": UPC_A}).text


def test_products_page_has_the_scan_controls(owner_client):
    page = owner_client.get("/products").text
    assert 'name="barcode"' in page and 'data-scan-fill="ap-barcode"' in page
    assert 'data-scan-fill="prod-q"' in page and "/static/scan.js" in page
    assert "Variant Group, Barcode" in page


def test_inventory_csv_has_a_barcode_column(owner_client, db_session):
    mk(db_session, "V50-CSV", barcode=EAN_B)
    rows = list(csv.reader(io.StringIO(owner_client.get("/export/csv/inventory").text)))
    header = rows[0]
    assert header[header.index("Stock") + 1] == "Barcode"
    mine = [r for r in rows if r and r[0] == "V50-CSV"]
    assert mine and mine[0][header.index("Barcode")] == EAN_B


def upload_csv(client, text):
    return client.post("/products/import", files={"file": ("p.csv", text.encode(), "text/csv")}, follow_redirects=False)


def test_csv_import_reads_barcodes(owner_client, db_session):
    upload_csv(owner_client, f"SKU,Name,Category,Brand,Price,Cost,Stock,Variant Group,Barcode\n"
                              f"V50-I1,V50 Imp 1,cases,,5,2,3,,{EAN_B}\nV50-I2,V50 Imp 2,cases,,5,2,3,,\n")
    db_session.expire_all()
    got = {p.sku: p.barcode for p in db_session.query(Product).filter(Product.sku.like("V50-I%"))}
    assert got == {"V50-I1": EAN_B, "V50-I2": ""}


def test_csv_import_without_the_column_still_works(owner_client, db_session):
    upload_csv(owner_client, "SKU,Name,Category,Brand,Price,Cost,Stock\nV50-I3,V50 Imp 3,cases,,5,2,3\n")
    db_session.expire_all()
    assert db_session.query(Product).filter(Product.sku == "V50-I3").one().barcode == ""


def test_csv_import_skips_used_duplicate_and_damaged_barcodes_but_adds_the_products(owner_client, db_session):
    mk(db_session, "V50-OWNER", barcode=UPC_A)
    upload_csv(owner_client,
               "SKU,Name,Category,Brand,Price,Cost,Stock,Variant Group,Barcode\n"
               f"V50-J1,V50 J1,cases,,5,2,3,,{EAN_13}\n"          # already used (as EAN-13 spelling)
               f"V50-J2,V50 J2,cases,,5,2,3,,{EAN_C}\n"           # fine
               f"V50-J3,V50 J3,cases,,5,2,3,,{EAN_C}\n"           # duplicate of the row above
               "V50-J4,V50 J4,cases,,5,2,3,,4.00638E+12\n"        # a spreadsheet ruined the number
               f"V50-J5,V50 J5,cases,,5,2,3,,'=\"{EAN_B}\"\n")    # Excel's text trick, tidied
    db_session.expire_all()
    got = {p.sku: p.barcode for p in db_session.query(Product).filter(Product.sku.like("V50-J%"))}
    assert got == {"V50-J1": "", "V50-J2": EAN_C, "V50-J3": "", "V50-J4": "", "V50-J5": EAN_B}
    assert "barcodes were left blank" in owner_client.get("/products").text


# ═══ linking a barcode from the scanner ═════════════════════════════════

def link(client, pid, code, ajax=True):
    return client.post(f"/products/{pid}/barcode", data={"barcode": code}, headers={"X-Scan-Ajax": "1"} if ajax else {},
                       follow_redirects=False)


def test_link_barcode_saves_audits_and_returns_the_product(owner_client, db_session):
    pid = mk(db_session, "V50-LINK")
    data = link(owner_client, pid, f" {EAN_B} ").json()
    assert data["ok"] is True and data["product"]["barcode"] == EAN_B and data["warning"] == ""
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == EAN_B
    assert "PRODUCT_EDIT" in audit_actions(db_session)


def test_manager_can_link_too(manager_client, db_session):
    pid = mk(db_session, "V50-LINKM")
    assert link(manager_client, pid, EAN_C).json()["ok"] is True


def test_link_barcode_conflicts_are_refused_with_a_409(owner_client, db_session):
    mk(db_session, "V50-HOLDER", "V50 Holder", barcode=UPC_A)
    pid = mk(db_session, "V50-LINK2")
    resp = link(owner_client, pid, EAN_13)
    assert resp.status_code == 409 and "V50 Holder" in resp.json()["error"]
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == ""


def test_relinking_the_same_product_is_fine(owner_client, db_session):
    pid = mk(db_session, "V50-LINK3", barcode=UPC_A)
    assert link(owner_client, pid, EAN_13).json()["ok"] is True       # same number, other spelling: no clash with itself


def test_link_barcode_warns_about_a_bad_check_digit_but_saves(owner_client, db_session):
    pid = mk(db_session, "V50-LINK4")
    data = link(owner_client, pid, "036000291453").json()
    assert data["ok"] is True and "check digit" in data["warning"]


def test_link_barcode_edge_cases(owner_client, db_session):
    pid = mk(db_session, "V50-LINK5")
    assert link(owner_client, pid, "   ").status_code == 400
    assert link(owner_client, "no-such-product", UPC_A).status_code == 404


def test_link_barcode_without_ajax_redirects_back_to_the_edit_page(owner_client, db_session):
    pid = mk(db_session, "V50-LINK6")
    resp = link(owner_client, pid, EAN_B, ajax=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/products/{pid}/edit")
    assert "Barcode saved" in owner_client.get(f"/products/{pid}/edit").text


def test_link_barcode_needs_sign_in(anon_client, db_session):
    pid = mk(db_session, "V50-LINK7")
    resp = anon_client.post(f"/products/{pid}/barcode", data={"barcode": UPC_A}, headers={"X-Scan-Ajax": "1"})
    assert resp.status_code == 401
    db_session.expire_all()
    assert db_session.get(Product, pid).barcode == ""


def test_product_search_json(owner_client, cashier_client, anon_client, db_session):
    mk(db_session, "V50-S1", "V50 Alpha case", barcode=UPC_A)
    mk(db_session, "V50-S2", "V50 Beta case")
    names = [p["name"] for p in owner_client.get("/products/search.json", params={"q": "alpha"}).json()["products"]]
    assert names == ["V50 Alpha case"]
    assert [p["sku"] for p in cashier_client.get("/products/search.json", params={"q": EAN_13}).json()["products"]] == ["V50-S1"]
    assert owner_client.get("/products/search.json", params={"q": "a"}).json()["products"] == []        # too short to search
    assert owner_client.get("/products/search.json", params={"q": "zzzzqqqq"}).json()["products"] == []
    assert anon_client.get("/products/search.json", params={"q": "alpha"}).status_code == 401


def test_register_page_has_the_camera_button(owner_client):
    page = owner_client.get("/pos").text
    assert 'id="pos-camera"' in page and "/static/scan.js" in page and 'data-can-link="1"' in page


def test_cashier_sees_the_register_scanner_without_link_rights(cashier_client):
    assert 'data-can-link="0"' in cashier_client.get("/pos").text


# ═══ stock count: the arithmetic ═══════════════════════════════════════

def test_a_count_starts_with_a_default_name_and_the_current_stock_is_remembered(db_session):
    pid = mk(db_session, "V50-C1", stock=8)
    count = SC.start(db_session, "Tester", "")
    assert count.name.startswith("Count ") and count.status == "open"
    product = db_session.get(Product, pid)
    ln = SC.add_to_line(db_session, count, product, 1, "Tester")
    assert (ln.counted, ln.stock_at_count) == (1, 8)
    db_session.commit()


def test_counting_adds_up_and_the_baseline_stays_put(db_session):
    pid = mk(db_session, "V50-C2", stock=8)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    for _ in range(3):
        SC.add_to_line(db_session, count, product, 1, "T")
    product.stock = 5                                       # a sale rings up mid-count
    ln = SC.add_to_line(db_session, count, product, 1, "T")
    assert ln.counted == 4 and ln.stock_at_count == 8       # the baseline is from the first scan
    db_session.commit()


def test_counts_never_go_below_zero_and_cannot_start_negative(db_session):
    pid = mk(db_session, "V50-C3", stock=2)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    with pytest.raises(SC.CountError):
        SC.add_to_line(db_session, count, product, -1, "T")
    SC.add_to_line(db_session, count, product, 1, "T")
    assert SC.add_to_line(db_session, count, product, -5, "T").counted == 0
    with pytest.raises(SC.CountError):
        SC.add_to_line(db_session, count, product, SC.MAX_QTY + 1, "T")
    db_session.commit()


def test_set_line_replaces_the_total_and_refreshes_the_baseline(db_session):
    pid = mk(db_session, "V50-C4", stock=10)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    SC.add_to_line(db_session, count, product, 1, "T")
    product.stock = 7
    ln = SC.set_line(db_session, count, product, 6, "T")
    assert (ln.counted, ln.stock_at_count) == (6, 7)
    with pytest.raises(SC.CountError):
        SC.set_line(db_session, count, product, -1, "T")
    assert SC.set_line(db_session, count, db_session.get(Product, mk(db_session, "V50-C4B", stock=3)), 9, "T").counted == 9
    db_session.commit()


def test_applying_sets_stock_to_what_was_counted(db_session):
    a, b, c = mk(db_session, "V50-A1", stock=10, cost=2), mk(db_session, "V50-A2", stock=4, cost=10), mk(db_session, "V50-A3", stock=6)
    count = SC.start(db_session, "T", "x")
    SC.set_line(db_session, count, db_session.get(Product, a), 7, "T")       # 3 missing
    SC.set_line(db_session, count, db_session.get(Product, b), 6, "T")       # 2 extra
    SC.set_line(db_session, count, db_session.get(Product, c), 6, "T")       # matches
    result = SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert result == {"items_changed": 2, "units_net": -1, "value_net": round(-3 * 2 + 2 * 10, 2)}
    assert [stock_of(db_session, i) for i in (a, b, c)] == [7, 6, 6]
    db_session.refresh(count)
    assert count.status == "applied" and count.closed_by == "Boss" and count.items_changed == 2 and count.units_net == -1


def test_a_sale_rung_up_during_the_count_is_not_undone(db_session):
    pid = mk(db_session, "V50-SALE", stock=10)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    SC.add_to_line(db_session, count, product, 1, "T")           # baseline 10
    SC.add_to_line(db_session, count, product, 9, "T")           # counted 10: matches the system
    product.stock = 8                                            # two sold while the shelf was still being counted
    db_session.commit()
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, pid) == 8                        # the sale stands; nothing is put back


def test_a_delivery_during_the_count_is_kept_and_a_real_shortfall_still_shows(db_session):
    pid = mk(db_session, "V50-DELIV", stock=10)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    SC.add_to_line(db_session, count, product, 8, "T")           # baseline 10, counted 8: 2 missing
    product.stock = 25                                           # 15 arrive before the count is approved
    db_session.commit()
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, pid) == 23                       # 25 - 2 missing


def test_apply_never_drives_stock_negative(db_session):
    pid = mk(db_session, "V50-NEG", stock=5)
    count = SC.start(db_session, "T", "x")
    product = db_session.get(Product, pid)
    SC.add_to_line(db_session, count, product, 0, "T")           # counted 0 (shelf empty), baseline 5
    product.stock = 2                                            # 3 sold meanwhile
    db_session.commit()
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, pid) == 0


def test_a_count_can_only_be_applied_once_and_cancelled_ones_cannot_be_applied(db_session):
    pid = mk(db_session, "V50-ONCE", stock=5)
    count = SC.start(db_session, "T", "x")
    SC.set_line(db_session, count, db_session.get(Product, pid), 3, "T")
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    with pytest.raises(SC.CountError):
        SC.apply_count(db_session, count, "Boss")
    with pytest.raises(SC.CountError):
        SC.add_to_line(db_session, count, db_session.get(Product, pid), 1, "T")
    other = SC.start(db_session, "T", "x")
    SC.cancel_count(db_session, other, "Boss")
    db_session.commit()
    with pytest.raises(SC.CountError):
        SC.apply_count(db_session, other, "Boss")
    assert stock_of(db_session, pid) == 3


def test_cancelling_changes_nothing(db_session):
    pid = mk(db_session, "V50-CAN", stock=9)
    count = SC.start(db_session, "T", "x")
    SC.set_line(db_session, count, db_session.get(Product, pid), 1, "T")
    SC.cancel_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, pid) == 9 and db_session.get(StockCount, count.id).status == "cancelled"


def test_uncounted_products_are_listed_and_can_be_zeroed_inside_the_scope(db_session):
    counted = mk(db_session, "V50-Z1", stock=5, category="v50-shelf")
    left_a = mk(db_session, "V50-Z2", stock=4, cost=3, category="v50-shelf")
    left_b = mk(db_session, "V50-Z3", stock=0, category="v50-shelf")
    elsewhere = mk(db_session, "V50-Z4", stock=7, category="v50-other")
    count = SC.start(db_session, "T", "zero", scope_category="v50-shelf")
    SC.set_line(db_session, count, db_session.get(Product, counted), 5, "T")
    rest, total = SC.uncounted(db_session, count)
    assert {p.sku for p in rest} == {"V50-Z2", "V50-Z3"} and total == 2
    result = SC.apply_count(db_session, count, "Boss", zero_uncounted=True)
    db_session.commit()
    assert result["items_changed"] >= 1
    assert stock_of(db_session, left_a) == 0 and stock_of(db_session, left_b) == 0
    assert stock_of(db_session, elsewhere) == 7 and stock_of(db_session, counted) == 5
    db_session.refresh(count)
    assert count.zero_filled is True


def test_without_the_zero_option_uncounted_products_are_left_alone(db_session):
    left = mk(db_session, "V50-LEFT", stock=4)
    count = SC.start(db_session, "T", "")
    SC.set_line(db_session, count, db_session.get(Product, mk(db_session, "V50-SEEN", stock=2)), 2, "T")
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, left) == 4


def test_a_product_deleted_during_the_count_is_skipped(db_session):
    pid = mk(db_session, "V50-GONE", stock=5)
    keep = mk(db_session, "V50-KEEP", stock=5)
    count = SC.start(db_session, "T", "x")
    SC.set_line(db_session, count, db_session.get(Product, pid), 1, "T")
    SC.set_line(db_session, count, db_session.get(Product, keep), 2, "T")
    db_session.delete(db_session.get(Product, pid))
    db_session.commit()
    rows = SC.review_rows(db_session, count)
    assert [r["deleted"] for r in rows if r["sku"] in ("V50-GONE",)] == [True]
    SC.apply_count(db_session, count, "Boss")
    db_session.commit()
    assert stock_of(db_session, keep) == 2


def test_review_rows_are_ordered_by_the_size_of_the_difference_and_summarised(db_session):
    a, b, c = mk(db_session, "V50-R1", stock=10, cost=1), mk(db_session, "V50-R2", stock=10, cost=1), mk(db_session, "V50-R3", stock=10, cost=1)
    count = SC.start(db_session, "T", "x")
    SC.set_line(db_session, count, db_session.get(Product, a), 9, "T")      # -1
    SC.set_line(db_session, count, db_session.get(Product, b), 15, "T")     # +5
    SC.set_line(db_session, count, db_session.get(Product, c), 10, "T")     # 0
    rows = SC.review_rows(db_session, count)
    assert [r["sku"] for r in rows] == ["V50-R2", "V50-R1", "V50-R3"]
    s = SC.summary(rows)
    assert s["lines"] == 3 and s["changed"] == 2 and s["units_net"] == 4 and s["missing_units"] == 1 and s["extra_units"] == 5
    db_session.commit()


def test_remove_line(db_session):
    pid = mk(db_session, "V50-RM", stock=1)
    count = SC.start(db_session, "T", "x")
    SC.add_to_line(db_session, count, db_session.get(Product, pid), 1, "T")
    assert SC.remove_line(db_session, count, pid) is True
    assert SC.remove_line(db_session, count, pid) is False
    db_session.commit()


def test_two_lines_for_one_product_in_one_count_are_impossible(db_session):
    from sqlalchemy.exc import IntegrityError
    pid = mk(db_session, "V50-UNI")
    count = SC.start(db_session, "T", "x")
    db_session.commit()
    db_session.add(StockCountLine(count_id=count.id, product_id=pid, counted=1))
    db_session.commit()
    db_session.add(StockCountLine(count_id=count.id, product_id=pid, counted=2))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ═══ stock count: through the web pages ════════════════════════════════

def test_start_a_count_and_open_it(owner_client, db_session):
    cid = new_count(owner_client, "V50 shelf one")
    page = owner_client.get(f"/stock-count/{cid}")
    assert page.status_code == 200 and "V50 shelf one" in page.text and 'id="sc-root"' in page.text
    assert "/static/stockcount.js" in page.text and "Review &amp; finish" in page.text
    listing = owner_client.get("/stock-count").text
    assert "V50 shelf one" in listing and "In progress" in listing
    assert "STOCK_COUNT" in audit_actions(db_session)


def test_everyone_signed_in_can_start_and_count(cashier_client, technician_client):
    for client in (cashier_client, technician_client):
        cid = new_count(client, "V50 staff count")
        assert client.get(f"/stock-count/{cid}").status_code == 200


def test_the_counting_screen_never_shows_the_system_quantity(owner_client, db_session):
    pid = mk(db_session, "V50-BLIND", "V50 Blind item", barcode=UPC_A, stock=7771)
    cid = new_count(owner_client)
    data = scan(owner_client, cid, UPC_A).json()
    assert data["ok"] is True and data["product"]["stock"] is None
    page = owner_client.get(f"/stock-count/{cid}").text
    assert "V50 Blind item" in page and "7771" not in page
    found = owner_client.get("/products/search.json", params={"q": "Blind item"}).json()
    assert found["ok"]                                            # (the search itself is the same as on the products page)
    assert line(owner_client, cid, pid).json()["product"]["stock"] is None


def test_scanning_counts_one_unit_each_time_and_finds_by_any_code(owner_client, db_session):
    pid = mk(db_session, "V50-SC1", "V50 Scan me", barcode=UPC_A)
    cid = new_count(owner_client)
    assert scan(owner_client, cid, UPC_A).json()["counted"] == 1
    assert scan(owner_client, cid, EAN_13).json()["counted"] == 2          # another spelling of the same number
    d = scan(owner_client, cid, "v50-sc1").json()
    assert d["counted"] == 3 and d["how"] == "sku" and "3 counted" in d["message"]
    db_session.expire_all()
    ln = db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).one()
    assert ln.product_id == pid and ln.counted == 3 and ln.stock_at_count == 10


def test_scan_with_a_quantity(owner_client, db_session):
    mk(db_session, "V50-SC2", barcode=EAN_B)
    cid = new_count(owner_client)
    assert scan(owner_client, cid, EAN_B, qty=12).json()["counted"] == 12


def test_unknown_code_is_reported_with_link_rights(owner_client, cashier_client):
    cid = new_count(owner_client)
    d = scan(owner_client, cid, "7612345678900").json()
    assert d["ok"] is False and d["found"] is False and d["code"] == "7612345678900" and d["canLink"] is True
    d = scan(cashier_client, cid, "7612345678900").json()
    assert d["found"] is False and d["canLink"] is False


def test_a_code_that_two_products_share_is_not_guessed(owner_client, db_session):
    mk(db_session, "V50-DUP-A", "V50 Dup A", barcode=EAN_B)
    db_session.add(Product(sku="V50-DUP-B", name="V50 Dup B", barcode=EAN_B, stock=1, price=1))
    db_session.commit()
    cid = new_count(owner_client)
    d = scan(owner_client, cid, EAN_B).json()
    assert d["ok"] is False and "share" in d["error"] and "V50 Dup A" in d["error"]
    db_session.expire_all()
    assert db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).count() == 0


def test_scanning_outside_the_count_category_is_allowed_but_noted(owner_client, db_session):
    mk(db_session, "V50-OUT", "V50 Screen thing", barcode=EAN_C, category="screen")
    cid = new_count(owner_client, scope="cases")
    d = scan(owner_client, cid, EAN_C).json()
    assert d["ok"] is True and d["outside"] is True and "outside" in d["message"]


def test_bad_codes_and_unknown_counts(owner_client):
    cid = new_count(owner_client)
    assert scan(owner_client, cid, "").status_code == 400
    assert scan(owner_client, cid, "12").status_code == 400
    r = scan(owner_client, "no-such-count", "7612345678900")
    assert r.status_code == 409 and r.json()["ok"] is False


def test_plus_minus_and_set_on_a_line(owner_client, db_session):
    pid = mk(db_session, "V50-LN1", stock=6)
    cid = new_count(owner_client)
    assert line(owner_client, cid, pid, "add", 1).json()["counted"] == 1
    assert line(owner_client, cid, pid, "add", 1).json()["counted"] == 2
    assert line(owner_client, cid, pid, "add", -1).json()["counted"] == 1
    assert line(owner_client, cid, pid, "set", 9).json()["counted"] == 9
    assert line(owner_client, cid, pid, "add", -50).json()["counted"] == 0
    assert line(owner_client, cid, pid, "set", -4).status_code == 400
    assert line(owner_client, cid, pid, "set", 100001).status_code == 400
    assert line(owner_client, cid, "no-such-product", "add", 1).status_code == 404
    other = mk(db_session, "V50-LN2")
    assert line(owner_client, cid, other, "add", -1).status_code == 400       # nothing counted yet, so nothing to take away


def test_remove_a_line_with_and_without_javascript(owner_client, db_session):
    a, b = mk(db_session, "V50-RM1"), mk(db_session, "V50-RM2")
    cid = new_count(owner_client)
    line(owner_client, cid, a)
    line(owner_client, cid, b)
    d = owner_client.post(f"/stock-count/{cid}/remove", data={"product_id": a}, headers={"X-Scan-Ajax": "1"}).json()
    assert d == {"ok": True, "product_id": a}
    resp = owner_client.post(f"/stock-count/{cid}/remove", data={"product_id": b}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/stock-count/{cid}")
    db_session.expire_all()
    assert db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).count() == 0


def test_counting_stops_once_a_count_is_finished(owner_client, db_session):
    pid = mk(db_session, "V50-FIN", barcode=EAN_B, stock=3)
    cid = new_count(owner_client)
    line(owner_client, cid, pid, "set", 3)
    assert owner_client.post(f"/stock-count/{cid}/apply", follow_redirects=False).status_code == 303
    r = scan(owner_client, cid, EAN_B)
    assert r.status_code == 409 and "already applied" in r.json()["error"]
    assert line(owner_client, cid, pid).status_code == 409
    d = owner_client.post(f"/stock-count/{cid}/remove", data={"product_id": pid}, headers={"X-Scan-Ajax": "1"})
    assert d.status_code == 409


def test_review_page_shows_differences_matches_and_uncounted(owner_client, db_session):
    short = mk(db_session, "V50-RV1", "V50 Short one", stock=10, cost=4, category="v50-shelf")
    ok = mk(db_session, "V50-RV2", "V50 Matching one", stock=5, category="v50-shelf")
    mk(db_session, "V50-RV3", "V50 Never scanned", stock=3, category="v50-shelf")
    cid = new_count(owner_client, scope="v50-shelf")
    line(owner_client, cid, short, "set", 7)
    line(owner_client, cid, ok, "set", 5)
    page = owner_client.get(f"/stock-count/{cid}/review").text
    assert "V50 Short one" in page and "-3" in page and "1 product matched exactly" in page
    assert "Not counted" in page and "V50 Never scanned" in page
    assert 'name="zero_uncounted"' in page and "Update the stock" in page
    assert stock_of(db_session, short) == 10                      # looking changes nothing


def test_review_shows_sales_since_counting(owner_client, db_session):
    pid = mk(db_session, "V50-RV4", "V50 Sold meanwhile", stock=10)
    cid = new_count(owner_client)
    line(owner_client, cid, pid, "set", 9)
    db_session.expire_all()
    db_session.get(Product, pid).stock = 6
    db_session.commit()
    page = owner_client.get(f"/stock-count/{cid}/review").text
    assert "sales or deliveries since counted (now 6)" in page


def test_staff_who_cannot_approve_see_a_waiting_message(cashier_client, owner_client, db_session):
    pid = mk(db_session, "V50-WAIT", stock=4)
    cid = new_count(cashier_client)
    line(cashier_client, cid, pid, "set", 2)
    page = cashier_client.get(f"/stock-count/{cid}/review").text
    assert "Waiting for approval" in page and "/apply" not in page and "net value at cost" not in page
    assert "net value at cost" in owner_client.get(f"/stock-count/{cid}/review").text


def test_only_the_owner_or_a_manager_can_apply_or_cancel(owner_client, manager_client, cashier_client, technician_client, db_session):
    pid = mk(db_session, "V50-PERM", stock=10)
    cid = new_count(cashier_client)
    line(cashier_client, cid, pid, "set", 4)
    for client in (cashier_client, technician_client):
        assert client.post(f"/stock-count/{cid}/apply", follow_redirects=False).status_code == 403
        assert client.post(f"/stock-count/{cid}/cancel", follow_redirects=False).status_code == 403
    assert stock_of(db_session, pid) == 10
    assert manager_client.post(f"/stock-count/{cid}/apply", follow_redirects=False).status_code == 303
    assert stock_of(db_session, pid) == 4
    cid2 = new_count(owner_client)
    assert manager_client.post(f"/stock-count/{cid2}/cancel", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(StockCount, cid2).status == "cancelled"


def test_applying_through_the_page_updates_stock_audits_and_shows_the_result(owner_client, db_session):
    a, b = mk(db_session, "V50-AP1", "V50 Apply 1", stock=10, cost=5), mk(db_session, "V50-AP2", "V50 Apply 2", stock=2, cost=1)
    cid = new_count(owner_client, "V50 apply me")
    line(owner_client, cid, a, "set", 8)
    line(owner_client, cid, b, "set", 5)
    resp = owner_client.post(f"/stock-count/{cid}/apply", follow_redirects=True)
    assert resp.status_code == 200 and "Stock updated: 2 product(s) changed, net +1 unit(s)." in resp.text
    assert "Applied" in resp.text and "V50 Apply 1" in resp.text
    assert [stock_of(db_session, a), stock_of(db_session, b)] == [8, 5]
    db_session.expire_all()
    count = db_session.get(StockCount, cid)
    assert (count.status, count.items_changed, count.units_net, count.value_net) == ("applied", 2, 1, -7.0)
    detail = [x.detail for x in db_session.query(AuditLog).filter(AuditLog.action == "STOCK_COUNT").all()]
    assert any("Applied count" in d and "V50 apply me" in d for d in detail)
    assert owner_client.get(f"/stock-count/{cid}/review", follow_redirects=False).status_code == 303   # review is for open counts
    assert "Applied" in owner_client.get("/stock-count").text


def test_zero_uncounted_through_the_page(owner_client, db_session):
    seen = mk(db_session, "V50-ZP1", stock=3, category="v50-shelf")
    gone = mk(db_session, "V50-ZP2", stock=9, category="v50-shelf")
    cid = new_count(owner_client, scope="v50-shelf")
    line(owner_client, cid, seen, "set", 3)
    owner_client.post(f"/stock-count/{cid}/apply", data={"zero_uncounted": "1"})
    assert stock_of(db_session, gone) == 0 and stock_of(db_session, seen) == 3
    assert "uncounted products set to zero" in owner_client.get(f"/stock-count/{cid}").text


def test_double_apply_from_a_second_tab_is_refused_without_changing_stock_again(owner_client, db_session):
    pid = mk(db_session, "V50-DBL", stock=10)
    cid = new_count(owner_client)
    line(owner_client, cid, pid, "set", 6)
    owner_client.post(f"/stock-count/{cid}/apply")
    db_session.expire_all()
    db_session.get(Product, pid).stock = 20                       # a delivery after the count
    db_session.commit()
    resp = owner_client.post(f"/stock-count/{cid}/apply", follow_redirects=True)
    assert "already applied" in resp.text
    assert stock_of(db_session, pid) == 20


def test_cancel_through_the_page(owner_client, db_session):
    pid = mk(db_session, "V50-CN", stock=4)
    cid = new_count(owner_client, "V50 cancel me")
    line(owner_client, cid, pid, "set", 1)
    resp = owner_client.post(f"/stock-count/{cid}/cancel", follow_redirects=True)
    assert "Count cancelled" in resp.text and "Cancelled" in resp.text
    assert stock_of(db_session, pid) == 4
    page = owner_client.get(f"/stock-count/{cid}").text
    assert "The stock was not changed" in page and 'id="sc-root"' not in page


def test_csv_export_is_owner_manager_only_and_guards_formulas(owner_client, manager_client, cashier_client, db_session):
    evil = mk(db_session, "V50-CSVX", name="=HYPERLINK(\"http://evil\",\"x\")", stock=3)
    plain = mk(db_session, "V50-CSVY", "V50 Plain", stock=5)
    cid = new_count(owner_client, "V50 export")
    line(owner_client, cid, evil, "set", 2)
    line(owner_client, cid, plain, "set", 5)
    assert cashier_client.get(f"/stock-count/{cid}/export.csv").status_code == 403
    resp = manager_client.get(f"/stock-count/{cid}/export.csv")
    assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"] and "stock_count_V50_export.csv" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][:3] == ["SKU", "Name", "Counted"] and rows[0][5] == "Would change by"
    body = {r[0]: r for r in rows[1:]}
    assert body["V50-CSVX"][1].startswith("'=") and body["V50-CSVX"][2] == "2" and body["V50-CSVX"][4] == "-1"
    assert body["V50-CSVY"][4] == "0"
    owner_client.post(f"/stock-count/{cid}/apply")
    assert list(csv.reader(io.StringIO(owner_client.get(f"/stock-count/{cid}/export.csv").text)))[0][5] == "Stock changed by"


def test_pages_that_need_a_login_redirect_and_json_ones_answer_401(anon_client, owner_client, db_session):
    cid = new_count(owner_client)
    for path in ("/stock-count", f"/stock-count/{cid}", f"/stock-count/{cid}/review", f"/stock-count/{cid}/export.csv"):
        r = anon_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/login"), path
    for path in ("/stock-count/start", f"/stock-count/{cid}/apply", f"/stock-count/{cid}/cancel"):
        r = anon_client.post(path, follow_redirects=False)
        assert r.status_code == 303, path
    assert anon_client.post(f"/stock-count/{cid}/scan", data={"code": UPC_A}).status_code == 401
    assert anon_client.post(f"/stock-count/{cid}/line", data={"product_id": "x"}).status_code == 401
    assert anon_client.post(f"/stock-count/{cid}/remove", data={"product_id": "x"}, headers={"X-Scan-Ajax": "1"}).status_code == 401


def test_unknown_count_pages_go_back_to_the_list(owner_client):
    for path in ("/stock-count/nope", "/stock-count/nope/review", "/stock-count/nope/export.csv"):
        r = owner_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/stock-count", path


def test_a_long_or_odd_count_name_is_tidied(owner_client, db_session):
    cid = new_count(owner_client, "  <b>Shelf</b>  " + "x" * 300)
    db_session.expire_all()
    name = db_session.get(StockCount, cid).name
    assert len(name) <= 80 and "  " not in name
    assert "<b>" not in owner_client.get(f"/stock-count/{cid}").text            # always escaped


def test_the_sidebar_links_to_stock_count(owner_client):
    assert 'href="/stock-count"' in owner_client.get("/products").text


# ═══ files the phone downloads ═════════════════════════════════════════

def test_scanner_files_are_served_from_this_site(owner_client):
    js = owner_client.get("/static/scan.js")
    assert js.status_code == 200 and "BarcodeDetector" in js.text and "/static/vendor/zxing-library-0.23.0.min.js" in js.text
    assert "https://" not in js.text and "http://" not in js.text                   # nothing is fetched from another site
    zx = owner_client.get("/static/vendor/zxing-library-0.23.0.min.js")
    assert zx.status_code == 200 and len(zx.content) > 100_000
    assert owner_client.get("/static/stockcount.js").status_code == 200
    assert (ROOT / "static/vendor/zxing-library-LICENSE.txt").exists()


def test_scanner_only_asks_for_barcode_formats_with_check_digits():
    js = (ROOT / "static/scan.js").read_text()
    for wanted in ("ean_13", "ean_8", "upc_a", "upc_e", "code_128"):
        assert wanted in js
    for refused in ("code_39", "itf", "qr_code", "CODE_39", "ITF", "QR_CODE"):
        assert refused not in js


def test_the_security_headers_allow_the_camera_to_this_site_only(owner_client):
    resp = owner_client.get("/stock-count")
    assert "camera=(self)" in resp.headers["permissions-policy"]
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-eval'" not in csp               # v54.1: only 'wasm-unsafe-eval' (the reader's WebAssembly), never 'unsafe-eval'
    assert "'wasm-unsafe-eval'" in csp


# ═══ the database ═══════════════════════════════════════════════════════

def test_schema_has_the_new_column_index_and_tables():
    insp = sa_inspect(engine)
    assert "barcode" in {c["name"] for c in insp.get_columns("products")}
    assert any(ix["column_names"] == ["barcode"] for ix in insp.get_indexes("products"))
    assert {"stock_counts", "stock_count_lines"} <= set(insp.get_table_names())
    assert any(set(u["column_names"]) == {"count_id", "product_id"} for u in insp.get_unique_constraints("stock_count_lines"))


def test_migrating_twice_is_harmless():
    from app.seed import migrate_schema
    migrate_schema()
    migrate_schema()
    assert "barcode" in {c["name"] for c in sa_inspect(engine).get_columns("products")}


# ═══ backup and restore ═════════════════════════════════════════════════

def test_counts_survive_a_backup_and_restore(owner_client, db_session):
    from app.models import Staff
    pid = mk(db_session, "V50-BK", "V50 Backup me", barcode=EAN_B, stock=5)
    cid = new_count(owner_client, "V50 backup count")
    line(owner_client, cid, pid, "set", 3)
    done = new_count(owner_client, "V50 finished count")
    line(owner_client, done, pid, "set", 5)
    owner_client.post(f"/stock-count/{done}/apply")
    doc = B.build_doc(db_session, include_secrets=False)
    assert len(doc["tables"]["stock_counts"]) == 2 and len(doc["tables"]["stock_count_lines"]) == 2
    assert doc["app"].startswith("TechPro+ CRM v")
    # wipe the count data, then restore
    db_session.query(StockCountLine).delete()
    db_session.query(StockCount).delete()
    db_session.commit()
    owner = db_session.query(Staff).filter(Staff.role == "owner").first()
    B.restore_full(db_session, B.read_backup(B.doc_bytes(doc)), owner)
    db_session.expire_all()
    assert {c.name: c.status for c in db_session.query(StockCount).all()} == {"V50 backup count": "open", "V50 finished count": "applied"}
    restored = db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).one()
    assert restored.counted == 3 and restored.product_id == pid
    assert db_session.get(Product, pid).barcode == EAN_B
    labels = {r["label"] for r in B.summarize(db_session, doc)}
    assert "Stock counts" in labels and "Stock count lines" in labels


def test_a_backup_made_before_barcodes_existed_still_restores(owner_client, db_session):
    from app.models import Staff
    pid = mk(db_session, "V50-OLD", "V50 Old backup item", stock=2)
    doc = B.build_doc(db_session, include_secrets=False)
    for row in doc["tables"]["products"]:
        row.pop("barcode", None)                                  # a v49 file has no such column
    doc["tables"].pop("stock_counts", None)
    doc["tables"].pop("stock_count_lines", None)
    owner = db_session.query(Staff).filter(Staff.role == "owner").first()
    B.restore_full(db_session, B.read_backup(B.doc_bytes(doc)), owner)
    db_session.expire_all()
    product = db_session.get(Product, pid)
    assert product is not None and (product.barcode or "") == ""
    assert BC.find_products(db_session, "V50-OLD")[1] == "sku"


# ═══ two phones counting at once ═════════════════════════════════════════

def test_two_phones_counting_the_same_product_never_lose_a_scan(db_session):
    from app.database import SessionLocal
    pid = mk(db_session, "V50-TWO", stock=10)
    count = SC.start(db_session, "A", "two phones")
    db_session.commit()
    cid = count.id
    other = SessionLocal()
    try:
        SC.add_to_line(db_session, count, db_session.get(Product, pid), 1, "A")        # phone A: first scan (not committed yet)
        db_session.commit()
        stale = db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).one()
        assert stale.counted == 1
        SC.add_to_line(other, other.get(StockCount, cid), other.get(Product, pid), 1, "B")   # phone B counts the same product
        other.commit()
        SC.add_to_line(db_session, count, db_session.get(Product, pid), 1, "A")        # phone A, still holding the old row, scans again
        db_session.commit()
    finally:
        other.close()
    db_session.expire_all()
    assert db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).one().counted == 3


def test_a_simultaneous_first_scan_of_a_product_is_counted_once_each(db_session, monkeypatch):
    from app.database import SessionLocal
    pid = mk(db_session, "V50-FIRST", stock=4)
    count = SC.start(db_session, "A", "race")
    db_session.commit()
    cid = count.id
    other = SessionLocal()
    try:
        SC.add_to_line(other, other.get(StockCount, cid), other.get(Product, pid), 1, "B")     # phone B got there a moment earlier
        other.commit()
    finally:
        other.close()
    real = SC._line
    calls = {"n": 0}

    def blind_first_time(db, count_id, product_id):         # phone A's own look-up happened before B's insert became visible
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, count_id, product_id)

    monkeypatch.setattr(SC, "_line", blind_first_time)
    SC.add_to_line(db_session, db_session.get(StockCount, cid), db_session.get(Product, pid), 1, "A")
    db_session.commit()
    monkeypatch.setattr(SC, "_line", real)
    db_session.expire_all()
    lines = db_session.query(StockCountLine).filter(StockCountLine.count_id == cid).all()
    assert len(lines) == 1 and lines[0].counted == 2


def test_applying_while_a_sale_is_ringing_up_keeps_the_sale(owner_client, db_session):
    """A real sale through the register while a count is open, then the count is applied."""
    pid = mk(db_session, "V50-LIVE", "V50 Live item", barcode=EAN_B, stock=10, price=10)
    cid = new_count(owner_client)
    for _ in range(10):
        scan(owner_client, cid, EAN_B)                        # the shelf holds 10
    owner_client.post(f"/pos/add/{pid}")                      # somebody buys one at the till
    owner_client.post(f"/pos/add/{pid}")
    sale = owner_client.post("/pos/checkout", data={"payment_method": "Cash", "cash_tendered": "100"}, follow_redirects=False)
    assert sale.status_code in (200, 303)
    after_sale = stock_of(db_session, pid)
    assert after_sale == 8
    owner_client.post(f"/stock-count/{cid}/apply")
    assert stock_of(db_session, pid) == 8                     # counted 10 before the sale; the sale is not undone


def test_a_big_catalogue_review_lists_the_first_200_but_zeroing_covers_every_uncounted_product(owner_client, db_session):
    db_session.add_all([Product(sku=f"V50-BIG-{i:03d}", name=f"V50 Big {i:03d}", category="v50-big", stock=2, cost=1, price=3) for i in range(205)])
    db_session.commit()
    seen = db_session.query(Product).filter(Product.sku == "V50-BIG-000").one().id
    cid = new_count(owner_client, scope="v50-big")
    line(owner_client, cid, seen, "set", 2)
    page = owner_client.get(f"/stock-count/{cid}/review").text
    assert "204 products in" in page and "(first 200)" in page and "V50 Big 001" in page and "V50 Big 204" not in page
    assert "Also set the 204 uncounted products to" in page and "units removed" not in page      # total units unknown past 200
    owner_client.post(f"/stock-count/{cid}/apply", data={"zero_uncounted": "1"})
    db_session.expire_all()
    rows = db_session.query(Product).filter(Product.category == "v50-big").all()
    assert len(rows) == 205 and sum(p.stock for p in rows) == 2             # only the one counted product still has stock
    count = db_session.get(StockCount, cid)
    assert count.items_changed == 204 and count.units_net == -408
