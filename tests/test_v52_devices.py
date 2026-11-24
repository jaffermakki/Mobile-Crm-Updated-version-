"""v52 — newer phones, tablets and the Galaxy M series in the Brand -> Model pickers."""
import html as H
import json
import re

import pytest

from app import devices as D
from app.models import Repair, TradeIn
from app.phone_const import PHONE_MODELS


def names(brand, cat=None):
    cat = cat or D.catalog()
    return [m for g in cat[brand] for m in g["models"]]


def group(brand, title_part):
    return next(g for g in D.catalog()[brand] if title_part in g["group"])


def catalog_from(html):
    m = re.search(r"data-catalog='([^']*)'", html)
    assert m, "the picker's list is missing from the page"
    return json.loads(H.unescape(m.group(1)))


# ── Pixel ──────────────────────────────────────────────────────────────

def test_pixel_10_and_11_families_are_listed_with_the_folds_and_the_a_model():
    g = names("Google")
    for model in ("Pixel 11", "Pixel 11 Pro", "Pixel 11 Pro XL", "Pixel 11 Pro Fold",
                  "Pixel 10", "Pixel 10 Pro", "Pixel 10 Pro XL", "Pixel 10 Pro Fold", "Pixel 10a",
                  "Pixel 9 Pro Fold", "Pixel 9a", "Pixel Fold", "Pixel Tablet"):
        assert model in g, model


def test_pixels_are_newest_first_and_grouped_by_family():
    titles = [g["group"] for g in D.catalog()["Google"]]
    assert titles[0].startswith("Pixel 11") and titles[1].startswith("Pixel 10") and titles[2].startswith("Pixel 9")
    g = names("Google")
    assert g.index("Pixel 11 Pro Fold") < g.index("Pixel 10") < g.index("Pixel 9 Pro") < g.index("Pixel 7a") < g.index("Pixel 6")
    assert g.index("Pixel 6") < g.index("Pixel 4a")                  # older Pixels come last
    assert group("Google", "Pixel 11")["models"] == ["Pixel 11 Pro Fold", "Pixel 11 Pro XL", "Pixel 11 Pro", "Pixel 11"]


def test_the_case_generator_list_knows_the_new_pixels_too():
    for m in ("Pixel 10", "Pixel 10 Pro XL", "Pixel 10a", "Pixel 11", "Pixel 11 Pro Fold"):
        assert m in PHONE_MODELS["Google"], m


def test_everything_in_the_older_reference_list_is_still_pickable():
    for brand in ("Google", "Motorola"):
        missing = [m for m in PHONE_MODELS[brand] if m not in names(brand)]
        assert not missing, (brand, missing)


# ── tablets ────────────────────────────────────────────────────────────

def test_ipads_are_listed_under_apple_after_the_iphones():
    apple = names("Apple")
    for model in ("iPad Pro 13-inch (M5)", "iPad Pro 11-inch (M5)", "iPad Pro 11-inch (M4)", "iPad Air 13-inch (M4)", "iPad Air 11-inch (M3)",
                  "iPad (A16)", "iPad (10th gen)", "iPad (9th gen)", "iPad mini (A20 Pro)", "iPad mini (A17 Pro)", "iPad mini (6th gen)"):
        assert model in apple, model
    assert apple.index("iPhone 4") < apple.index("iPad Pro 13-inch (M5)")        # phones first: that is most of the work
    titles = [g["group"] for g in D.catalog()["Apple"]]
    assert titles[-4:] == ["iPad Pro", "iPad Air", "iPad", "iPad mini"]


def test_galaxy_tabs_are_listed_under_samsung():
    sam = names("Samsung")
    for model in ("Galaxy Tab S12 Ultra", "Galaxy Tab S12+", "Galaxy Tab S11 Ultra", "Galaxy Tab S11", "Galaxy Tab S10 FE", "Galaxy Tab S10 Lite",
                  "Galaxy Tab S9 FE", "Galaxy Tab S8 Ultra", "Galaxy Tab S7", "Galaxy Tab A11+", "Galaxy Tab A9", "Galaxy Tab A8",
                  "Galaxy Tab Active5 Pro", "Galaxy Tab Active3"):
        assert model in sam, model
    assert sam.index("Galaxy Tab S12 Ultra") < sam.index("Galaxy Tab S11") < sam.index("Galaxy Tab S9")


def test_the_pixel_tablet_is_there_for_google():
    assert "Pixel Tablet" in names("Google")


# ── Galaxy M ───────────────────────────────────────────────────────────

def test_galaxy_m_series_is_listed_newest_first_and_labelled_as_imports():
    g = next(g for g in D.catalog()["Samsung"] if g["group"].startswith("Galaxy M"))
    assert "import" in g["group"].lower()
    for model in ("Galaxy M17 5G", "Galaxy M07", "Galaxy M56 5G", "Galaxy M36 5G", "Galaxy M35 5G", "Galaxy M15 5G", "Galaxy M34 5G",
                  "Galaxy M31", "Galaxy M21", "Galaxy M10"):
        assert model in g["models"], model
    m = g["models"]
    assert m.index("Galaxy M56 5G") < m.index("Galaxy M35 5G") < m.index("Galaxy M21") < m.index("Galaxy M10")


# ── the whole catalogue stays tidy ─────────────────────────────────────

def test_every_name_starts_with_its_makers_word_and_is_clean():
    prefixes = {"Apple": ("iPhone", "iPad"), "Samsung": ("Galaxy",), "Google": ("Pixel",)}
    for brand, pre in prefixes.items():
        for m in names(brand):
            assert m.startswith(pre), (brand, m)
            assert m == " ".join(m.split()) and 2 <= len(m) <= 60, m


def test_no_model_appears_twice_even_across_brands():
    seen = {}
    for brand in ("Apple", "Samsung", "Google", "Motorola"):
        for m in names(brand):
            assert m.lower() not in seen, f"{m!r} is in both {seen.get(m.lower())} and {brand}"
            seen[m.lower()] = brand


def test_every_group_still_has_a_title_and_models_and_the_brand_order_is_unchanged():
    for groups in D.catalog().values():
        for g in groups:
            assert g["group"].strip() and g["models"]
    assert list(D.catalog()) == ["Apple", "Samsung", "Google", "Motorola", "Other"]


def test_a_model_staff_added_that_is_now_built_in_is_not_repeated():
    cat = D.catalog({"Google": ["Pixel 11 Pro", "Pixel 12"], "Samsung": ["Galaxy Tab S12 Ultra"]})
    assert names("Google", cat).count("Pixel 11 Pro") == 1
    assert [g["models"] for g in cat["Google"] if g["group"] == "Added by you"] == [["Pixel 12"]]
    assert all(g["group"] != "Added by you" for g in D.catalog({"Samsung": ["Galaxy Tab S12 Ultra"]})["Samsung"])


# ── the pages ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["/repairs", "/trade-ins"])
def test_the_repair_and_trade_in_pages_send_the_new_models(owner_client, url):
    data = catalog_from(owner_client.get(url).text)
    flat = {b["brand"]: [m for g in b["groups"] for m in g["models"]] for b in data}
    assert "Pixel 11 Pro Fold" in flat["Google"] and "Pixel 10a" in flat["Google"]
    assert "iPad Pro 13-inch (M5)" in flat["Apple"] and "iPad mini (A20 Pro)" in flat["Apple"]
    assert "Galaxy Tab S12 Ultra" in flat["Samsung"] and "Galaxy M56 5G" in flat["Samsung"]


def test_a_picked_tablet_is_saved_exactly_as_shown_with_storage_added(owner_client, db_session):
    device = "iPad Air 11-inch (M3), 256GB, Wi-Fi + Cellular"
    r = owner_client.post("/repairs/add", data={"phone": "4035550166", "name": "Tablet Test", "device": device,
                                                "issue": "Cracked screen", "warranty_days": "90"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(Repair).filter(Repair.device == device).count() == 1


def test_a_galaxy_m_phone_can_be_booked_in(owner_client, db_session):
    device = "Galaxy M56 5G, 8GB, Black"
    r = owner_client.post("/repairs/add", data={"phone": "4035550177", "name": "Import Test", "device": device,
                                                "issue": "Battery", "warranty_days": "90"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.query(Repair).filter(Repair.device == device).count() == 1


def test_the_bulk_variants_page_offers_the_new_pixels(owner_client):
    html = owner_client.get("/products/bulk-variants").text
    assert "Pixel 11 Pro XL" in html and "Pixel 10a" in html
