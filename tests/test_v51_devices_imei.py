"""
v51 — the Brand -> Model lists on the repair and trade-in forms, and tidying / checking IMEI numbers.

The pickers themselves are JavaScript (checked in a real phone-sized browser); these tests cover everything the server
decides: what is in the lists, how staff-added models are merged, what each page sends, and what happens to an IMEI.
"""
import json
import re

import pytest

from app import devices as D
from app import imei as I
from app.models import AuditLog, Repair, Setting, TradeIn

GOOD_IMEI = "490154203237518"        # a well-known sample IMEI with a valid check digit


# ═══ IMEI helper ══════════════════════════════════════════════════════

def test_luhn_accepts_a_real_imei_and_rejects_a_typo():
    assert I.luhn_ok(GOOD_IMEI)
    assert not I.luhn_ok("490154203237519")
    assert not I.luhn_ok("49015420323751x") and not I.luhn_ok("")


@pytest.mark.parametrize("raw,expected", [
    ("49 015420 323751 8", GOOD_IMEI),
    ("49-015420-323751-8", GOOD_IMEI),
    ("490154/203237/518", GOOD_IMEI),
    ("  490154203237518\r\n", GOOD_IMEI),
    ("490154203237518", GOOD_IMEI),
    ("", ""),
    (None, ""),
])
def test_clean_collapses_a_number_with_separators(raw, expected):
    assert I.clean(raw) == expected


@pytest.mark.parametrize("serial", ["F2LXK3J9PQ", "R58N 123ABC", "SN-123-ABC", "a1b2"])
def test_clean_never_touches_serial_numbers_with_letters(serial):
    assert I.clean(serial) == " ".join(serial.split())


def test_clean_caps_the_length():
    assert len(I.clean("x" * 500)) == I.MAX_LEN
    assert I.clean("1" * 500) == "1" * I.MAX_LEN       # trimmed to the cap before it is judged


def test_warning_is_silent_for_good_imeis_serials_and_blanks():
    assert I.warning(GOOD_IMEI) == ""
    assert I.warning("F2LXK3J9PQ") == ""
    assert I.warning("") == "" and I.warning(None) == ""
    assert I.warning("123") == ""                      # too short to judge


def test_warning_speaks_up_about_a_wrong_check_digit_or_length():
    assert "check digit" in I.warning("490154203237519")
    assert "15 digits" in I.warning("49015420323751")        # 14
    assert "15 digits" in I.warning("4901542032375181")      # 16
    assert "not 15 long" in I.warning("123456789")           # all digits, 9 long


# ═══ the phone lists ══════════════════════════════════════════════════

def names(brand, cat=None):
    cat = cat or D.catalog()
    return [m for g in cat[brand] for m in g["models"]]


def test_brand_order_is_fixed_with_other_last():
    assert list(D.catalog()) == ["Apple", "Samsung", "Google", "Motorola", "Other"]
    assert [b["brand"] for b in D.payload()] == ["Apple", "Samsung", "Google", "Motorola", "Other"]
    assert D.catalog()["Other"] == []


def test_apple_list_has_the_models_sold_in_canada_newest_first():
    apple = names("Apple")
    for model in ("iPhone Duo", "iPhone 18 Pro Max", "iPhone 18 Pro", "iPhone 17 Pro Max", "iPhone 17e", "iPhone Air", "iPhone 16e",
                  "iPhone 15 Plus", "iPhone 14", "iPhone 13 mini", "iPhone 12 Pro Max", "iPhone SE (3rd gen)", "iPhone 11",
                  "iPhone XR", "iPhone 8 Plus", "iPhone 7"):
        assert model in apple, model
    assert apple.index("iPhone 18 Pro Max") < apple.index("iPhone 17 Pro Max") < apple.index("iPhone 15") < apple.index("iPhone 11")


def test_samsung_list_has_the_galaxy_models_sold_in_canada():
    sam = names("Samsung")
    for model in ("Galaxy S26 Ultra", "Galaxy S25 Edge", "Galaxy S24 FE", "Galaxy S23 Ultra", "Galaxy S22+", "Galaxy S21 FE",
                  "Galaxy Z Fold8 Ultra", "Galaxy Z Fold8", "Galaxy Z Flip8", "Galaxy Z Fold7", "Galaxy Z Flip7 FE", "Galaxy Z Flip6",
                  "Galaxy A56 5G", "Galaxy A36 5G", "Galaxy A16 5G", "Galaxy A15 5G", "Galaxy Note20 Ultra", "Galaxy XCover6 Pro"):
        assert model in sam, model


def test_google_and_motorola_come_from_the_existing_reference_list():
    assert any(m.startswith("Pixel") for m in names("Google"))
    assert len(names("Motorola")) >= 5


def test_every_model_is_unique_within_its_brand_and_has_a_clean_name():
    for brand in ("Apple", "Samsung", "Google", "Motorola"):
        lst = names(brand)
        assert len(lst) == len({m.lower() for m in lst}), f"duplicate model in {brand}"
        for m in lst:
            assert m == " ".join(m.split()) and 2 <= len(m) <= 60
    assert all(m.startswith(("iPhone", "iPad")) for m in names("Apple"))
    assert all(m.startswith("Galaxy") for m in names("Samsung"))


def test_every_group_has_a_title_and_models():
    for groups in D.catalog().values():
        for g in groups:
            assert g["group"].strip() and g["models"]


def test_staff_added_models_appear_under_added_by_you():
    cat = D.catalog({"Apple": ["iPhone 19 Pro"], "Samsung": ["Galaxy S27"]})
    assert cat["Apple"][0] == {"group": "Added by you", "models": ["iPhone 19 Pro"]}
    assert cat["Samsung"][0]["models"] == ["Galaxy S27"]
    assert "iPhone 19 Pro" in D.all_models(cat)


def test_a_staff_added_model_already_in_the_list_is_not_repeated():
    cat = D.catalog({"Apple": ["iPhone 17 Pro", "IPHONE 17 PRO", "iPhone 19 Pro"]})
    assert names("Apple", cat).count("iPhone 17 Pro") == 1
    assert [g["models"] for g in cat["Apple"] if g["group"] == "Added by you"] == [["iPhone 19 Pro"]]
    assert all(g["group"] != "Added by you" for g in D.catalog({"Apple": ["iPhone 17 Pro"]})["Apple"])


def test_a_brand_staff_added_is_listed_before_other():
    cat = D.catalog({"Nokia": ["Nokia G42"], "OnePlus": ["OnePlus 12"]})
    assert list(cat) == ["Apple", "Samsung", "Google", "Motorola", "Nokia", "OnePlus", "Other"]
    assert cat["Nokia"] == [{"group": "Added by you", "models": ["Nokia G42"]}]


def test_junk_in_the_staff_list_is_ignored():
    cat = D.catalog({"Apple": ["", "   ", None, 7], "": ["x"], "Other": ["y"], "Nokia": [], "Fairphone": "not a list", "X" * 3: ["  Spaced   Out  "]})
    assert "Other" in cat and cat["Other"] == []
    assert "Fairphone" not in cat and "Nokia" not in cat
    assert cat["XXX"][0]["models"] == ["Spaced Out"]
    assert D.catalog(None) == D.catalog({}) == D.catalog()


def test_custom_list_is_capped():
    many = [f"Model {i}" for i in range(D.MAX_CUSTOM + 50)]
    assert len(D.catalog({"Nokia": many})["Nokia"][0]["models"]) == D.MAX_CUSTOM


def test_payload_is_json_safe_and_keeps_its_order():
    data = json.loads(json.dumps(D.payload({"Nokia": ["N1"]})))
    assert [b["brand"] for b in data] == ["Apple", "Samsung", "Google", "Motorola", "Nokia", "Other"]
    assert set(data[0]) == {"brand", "groups"}


# ═══ the pages ════════════════════════════════════════════════════════

def catalog_from(html):
    m = re.search(r"data-catalog='([^']*)'", html)
    assert m, "the picker's list is missing from the page"
    import html as H
    return json.loads(H.unescape(m.group(1)))


@pytest.mark.parametrize("url,scripts", [("/repairs", ("devicepick.js", "scan.js")), ("/trade-ins", ("devicepick.js", "scan.js"))])
def test_repair_and_trade_in_pages_send_the_list_and_the_picker_script(owner_client, url, scripts):
    resp = owner_client.get(url)
    assert resp.status_code == 200
    data = catalog_from(resp.text)
    assert [b["brand"] for b in data][:2] == ["Apple", "Samsung"]
    assert any("iPhone 18 Pro" in g["models"] for g in data[0]["groups"])
    for script in scripts:
        assert script in resp.text
    assert 'name="device"' in resp.text and 'class="dp-text' in resp.text.replace("'", '"') or "dp-text" in resp.text


@pytest.mark.parametrize("client_fixture", ["cashier_client", "technician_client", "manager_client"])
def test_every_role_that_can_open_repairs_gets_the_list(request, client_fixture):
    resp = request.getfixturevalue(client_fixture).get("/repairs")
    assert resp.status_code == 200 and "iPhone 18 Pro" in resp.text


def test_models_staff_add_show_up_in_the_pickers(owner_client, db_session):
    row = db_session.get(Setting, "custom_phone_models")
    before = row.value if row else None
    try:
        db_session.merge(Setting(key="custom_phone_models", value=json.dumps({"Apple": ["iPhone 19 Test"], "Nokia": ["Nokia X Test"]})))
        db_session.commit()
        for url in ("/repairs", "/trade-ins"):
            data = catalog_from(owner_client.get(url).text)
            brands = [b["brand"] for b in data]
            assert "Nokia" in brands and brands[-1] == "Other"
            assert any(g["group"] == "Added by you" and g["models"] == ["iPhone 19 Test"] for g in data[0]["groups"])
    finally:
        db_session.merge(Setting(key="custom_phone_models", value=before if before is not None else "{}"))
        db_session.commit()


def test_the_device_text_box_is_what_gets_saved(owner_client, db_session):
    device = "iPhone 18 Pro Max, 512GB, Burgundy"
    resp = owner_client.post("/repairs/add", data={"phone": "4035550111", "name": "Picker Test", "device": device,
                                                    "issue": "Cracked screen", "warranty_days": "90"}, follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.query(Repair).filter(Repair.device == device).count() == 1


# ═══ IMEI on the forms ════════════════════════════════════════════════

def make_repair(client, db, device, imei, **extra):
    data = {"phone": "4035550122", "name": "Imei Test", "device": device, "imei": imei, "issue": "Battery", "warranty_days": "90"}
    data.update(extra)
    resp = client.post("/repairs/add", data=data, follow_redirects=True)
    db.expire_all()
    repair = db.query(Repair).filter(Repair.device == device).order_by(Repair.ticket_no.desc()).first()
    return resp, repair


def test_a_spaced_imei_is_saved_clean_with_no_warning(owner_client, db_session):
    resp, repair = make_repair(owner_client, db_session, "IMEI clean 1", "49 015420 323751 8")
    assert repair.imei == GOOD_IMEI
    assert "check digit" not in resp.text and "Take photos" in resp.text


def test_a_bad_check_digit_is_saved_but_flagged_in_amber(owner_client, db_session):
    resp, repair = make_repair(owner_client, db_session, "IMEI typo 1", "490154203237519")
    assert repair.imei == "490154203237519", "a warning must never block the ticket"
    assert "check digit is wrong" in resp.text


def test_a_serial_number_is_saved_exactly_and_never_warned_about(owner_client, db_session):
    resp, repair = make_repair(owner_client, db_session, "IMEI serial 1", "F2LXK3J9PQ")
    assert repair.imei == "F2LXK3J9PQ"
    assert "check digit" not in resp.text and "normally 15" not in resp.text


def test_a_ticket_without_an_imei_still_works(owner_client, db_session):
    _, repair = make_repair(owner_client, db_session, "IMEI none 1", "")
    assert repair is not None and (repair.imei or "") == ""


def test_changing_the_imei_on_a_ticket_cleans_flags_and_audits(owner_client, db_session):
    _, repair = make_repair(owner_client, db_session, "IMEI edit 1", "")
    resp = owner_client.post(f"/repairs/{repair.id}/imei", data={"imei": "35 123456 789012"}, follow_redirects=True)   # 14 digits
    db_session.expire_all()
    assert db_session.get(Repair, repair.id).imei == "35123456789012"
    assert "normally 15" in resp.text
    resp = owner_client.post(f"/repairs/{repair.id}/imei", data={"imei": "49 015420 323751 8"}, follow_redirects=True)
    db_session.expire_all()
    assert db_session.get(Repair, repair.id).imei == GOOD_IMEI and "IMEI / serial saved" in resp.text
    assert any(GOOD_IMEI in (a.detail or "") for a in db_session.query(AuditLog).filter(AuditLog.action == "REPAIR_EDIT").all())


def test_repair_page_has_a_scan_button_for_the_imei(owner_client, db_session):
    _, repair = make_repair(owner_client, db_session, "IMEI page 1", GOOD_IMEI)
    page = owner_client.get(f"/repairs/{repair.id}").text
    assert 'data-scan-fill="rd-imei"' in page and 'data-scan-submit="1"' in page
    assert 'data-scan-fill="rp-imei"' in owner_client.get("/repairs").text
    assert "*#06#" in owner_client.get("/repairs").text


def accept_trade_in(client, device, imei):
    return client.post("/trade-ins/add", data={"new_customer_name": "Trade Tester", "new_customer_phone": "4035550133", "device": device, "imei": imei,
                                                "condition": "Good", "offered_amount": "50", "payout_method": "store_credit"}, follow_redirects=True)


def test_trade_in_imei_is_cleaned_and_a_typo_is_flagged(owner_client, db_session):
    resp = accept_trade_in(owner_client, "TI imei clean", "49 015420 323751 8")
    db_session.expire_all()
    assert db_session.query(TradeIn).filter(TradeIn.device == "TI imei clean").one().imei == GOOD_IMEI
    assert "check digit" not in resp.text
    resp = accept_trade_in(owner_client, "TI imei typo", "490154203237519")
    db_session.expire_all()
    assert db_session.query(TradeIn).filter(TradeIn.device == "TI imei typo").one().imei == "490154203237519"
    assert "check digit is wrong" in resp.text


def test_trade_in_page_has_the_scan_button_and_picker(owner_client):
    page = owner_client.get("/trade-ins").text
    assert 'data-scan-fill="ti-imei"' in page and "devicepick.js" in page


def test_register_imei_box_cleans_and_has_a_scan_button(owner_client, db_session):
    from app.models import Product
    p = Product(sku="V51-IMEI-PH", name="V51 Phone", price=500.0, cost=300.0, stock=2, category="phones")
    db_session.add(p)
    db_session.commit()
    try:
        owner_client.post("/pos/clear")
        owner_client.post("/pos/scan", data={"sku": "V51-IMEI-PH"})
        owner_client.post("/pos/imei/0", data={"imei": "49 015420 323751 8"})
        cart = owner_client.get("/pos").text
        assert GOOD_IMEI in cart and 'data-scan-fill="pos-imei-0"' in cart and 'data-scan-submit="1"' in cart
    finally:
        owner_client.post("/pos/clear")
        db_session.delete(p)
        db_session.commit()
