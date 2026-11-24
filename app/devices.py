"""v51/v52: the device list behind the Brand -> Model pickers on the repair ticket and trade-in forms.

Apple, Samsung and Google are written out in full (phones sold in Canada, newest first, grouped by family so a long
list stays easy to scroll). v52 added the Pixel 10 and 11 families, tablets (iPad, Galaxy Tab, Pixel Tablet) and the
Galaxy M phones that customers bring in from abroad. Motorola comes from phone_const.PHONE_MODELS. Anything else is
typed in by hand, and any model staff added on the Bulk-Create Variants page (Setting "custom_phone_models") is shown
too, under "Added by you". The text box next to the pickers is what is saved, so a model that is not listed here can
always be typed, and storage or colour can be added to a picked one ("iPhone 15 Pro, 256GB, Blue").

Names follow the makers' own spelling, so reports can tell the brand from the first word ("iPhone ...", "Galaxy ...").

Lists last checked November 2026. Samsung's older Galaxy A models are the ones sold through Canadian carriers and
retail; if one is missing, add it once on the Bulk-Create Variants page (or just type it) and it appears here too.
"""
from __future__ import annotations

from .phone_const import PHONE_MODELS

OTHER_BRAND = "Other"
MAX_CUSTOM = 200

# brand -> [(group title, [models, newest first])]
APPLE = [
    ("iPhone 18 family (2026)", ["iPhone Duo", "iPhone 18 Pro Max", "iPhone 18 Pro"]),
    ("iPhone 17 family (2025–26)", ["iPhone 17e", "iPhone Air", "iPhone 17 Pro Max", "iPhone 17 Pro", "iPhone 17"]),
    ("iPhone 16 family (2024–25)", ["iPhone 16e", "iPhone 16 Pro Max", "iPhone 16 Pro", "iPhone 16 Plus", "iPhone 16"]),
    ("iPhone 15 family (2023)", ["iPhone 15 Pro Max", "iPhone 15 Pro", "iPhone 15 Plus", "iPhone 15"]),
    ("iPhone 14 family (2022)", ["iPhone 14 Pro Max", "iPhone 14 Pro", "iPhone 14 Plus", "iPhone 14"]),
    ("iPhone 13 family (2021)", ["iPhone 13 Pro Max", "iPhone 13 Pro", "iPhone 13", "iPhone 13 mini"]),
    ("iPhone 12 family (2020)", ["iPhone 12 Pro Max", "iPhone 12 Pro", "iPhone 12", "iPhone 12 mini"]),
    ("iPhone SE", ["iPhone SE (3rd gen)", "iPhone SE (2nd gen)", "iPhone SE (1st gen)"]),
    ("iPhone 11 family (2019)", ["iPhone 11 Pro Max", "iPhone 11 Pro", "iPhone 11"]),
    ("iPhone X family (2017–18)", ["iPhone XS Max", "iPhone XS", "iPhone XR", "iPhone X"]),
    ("iPhone 8 and 7", ["iPhone 8 Plus", "iPhone 8", "iPhone 7 Plus", "iPhone 7"]),
    ("Older iPhones", ["iPhone 6s Plus", "iPhone 6s", "iPhone 6 Plus", "iPhone 6", "iPhone 5s", "iPhone 5c", "iPhone 5", "iPhone 4s", "iPhone 4"]),
    # v52: tablets, named the way Apple names them on the back of the box and in Settings > General > About
    ("iPad Pro", ["iPad Pro 13-inch (M5)", "iPad Pro 11-inch (M5)", "iPad Pro 13-inch (M4)", "iPad Pro 11-inch (M4)",
                  "iPad Pro 12.9-inch (6th gen)", "iPad Pro 11-inch (4th gen)", "iPad Pro 12.9-inch (5th gen)", "iPad Pro 11-inch (3rd gen)",
                  "iPad Pro 12.9-inch (4th gen)", "iPad Pro 11-inch (2nd gen)", "iPad Pro 12.9-inch (3rd gen)", "iPad Pro 11-inch (1st gen)",
                  "iPad Pro 12.9-inch (2nd gen)", "iPad Pro 10.5-inch", "iPad Pro 12.9-inch (1st gen)", "iPad Pro 9.7-inch"]),
    ("iPad Air", ["iPad Air 13-inch (M4)", "iPad Air 11-inch (M4)", "iPad Air 13-inch (M3)", "iPad Air 11-inch (M3)",
                  "iPad Air 13-inch (M2)", "iPad Air 11-inch (M2)", "iPad Air (5th gen)", "iPad Air (4th gen)", "iPad Air (3rd gen)",
                  "iPad Air 2", "iPad Air"]),
    ("iPad", ["iPad (A16)", "iPad (10th gen)", "iPad (9th gen)", "iPad (8th gen)", "iPad (7th gen)", "iPad (6th gen)", "iPad (5th gen)"]),
    ("iPad mini", ["iPad mini (A20 Pro)", "iPad mini (A17 Pro)", "iPad mini (6th gen)", "iPad mini (5th gen)", "iPad mini 4"]),
]

SAMSUNG = [
    ("Galaxy S26 family (2026)", ["Galaxy S26 Ultra", "Galaxy S26+", "Galaxy S26", "Galaxy S26 FE"]),
    ("Galaxy S25 family (2025)", ["Galaxy S25 Ultra", "Galaxy S25+", "Galaxy S25 Edge", "Galaxy S25", "Galaxy S25 FE"]),
    ("Galaxy S24 family (2024)", ["Galaxy S24 Ultra", "Galaxy S24+", "Galaxy S24", "Galaxy S24 FE"]),
    ("Galaxy S23 family (2023)", ["Galaxy S23 Ultra", "Galaxy S23+", "Galaxy S23", "Galaxy S23 FE"]),
    ("Galaxy S22 family (2022)", ["Galaxy S22 Ultra", "Galaxy S22+", "Galaxy S22"]),
    ("Galaxy S21 family (2021)", ["Galaxy S21 Ultra", "Galaxy S21+", "Galaxy S21", "Galaxy S21 FE"]),
    ("Galaxy S20 family (2020)", ["Galaxy S20 Ultra", "Galaxy S20+", "Galaxy S20", "Galaxy S20 FE"]),
    ("Older Galaxy S", ["Galaxy S10 5G", "Galaxy S10+", "Galaxy S10", "Galaxy S10e", "Galaxy S10 Lite",
                        "Galaxy S9+", "Galaxy S9", "Galaxy S8+", "Galaxy S8", "Galaxy S7 edge", "Galaxy S7"]),
    ("Galaxy Z Fold", ["Galaxy Z Fold8 Ultra", "Galaxy Z Fold8", "Galaxy Z Fold7", "Galaxy Z Fold6", "Galaxy Z Fold5",
                       "Galaxy Z Fold4", "Galaxy Z Fold3", "Galaxy Z Fold2", "Galaxy Fold"]),
    ("Galaxy Z Flip", ["Galaxy Z Flip8", "Galaxy Z Flip7 FE", "Galaxy Z Flip7", "Galaxy Z Flip6", "Galaxy Z Flip5",
                       "Galaxy Z Flip4", "Galaxy Z Flip3", "Galaxy Z Flip 5G", "Galaxy Z Flip"]),
    ("Galaxy Note", ["Galaxy Note20 Ultra", "Galaxy Note20", "Galaxy Note10+", "Galaxy Note10", "Galaxy Note9", "Galaxy Note8"]),
    ("Galaxy A (current)", ["Galaxy A57 5G", "Galaxy A37 5G", "Galaxy A17 5G", "Galaxy A56 5G", "Galaxy A36 5G", "Galaxy A26 5G",
                            "Galaxy A16 5G", "Galaxy A06", "Galaxy A55 5G", "Galaxy A35 5G", "Galaxy A25 5G", "Galaxy A15 5G"]),
    ("Galaxy A (older)", ["Galaxy A54 5G", "Galaxy A14 5G", "Galaxy A53 5G", "Galaxy A23 5G", "Galaxy A13", "Galaxy A52s 5G",
                          "Galaxy A52 5G", "Galaxy A32 5G", "Galaxy A42 5G", "Galaxy A12", "Galaxy A11", "Galaxy A71 5G",
                          "Galaxy A51 5G", "Galaxy A51", "Galaxy A20", "Galaxy A10e", "Galaxy A03s", "Galaxy A02s", "Galaxy A01"]),
    ("Galaxy XCover (rugged)", ["Galaxy XCover6 Pro", "Galaxy XCover Pro"]),
    # v52: the Galaxy M phones are sold mainly in India and Asia, so these arrive in Canada as imports
    ("Galaxy M (imported, not sold by Canadian carriers)", [
        "Galaxy M17e", "Galaxy M17 5G", "Galaxy M07", "Galaxy M56 5G", "Galaxy M36 5G", "Galaxy M16 5G", "Galaxy M06 5G",
        "Galaxy M55s 5G", "Galaxy M55 5G", "Galaxy M35 5G", "Galaxy M15 5G", "Galaxy M05", "Galaxy M54 5G", "Galaxy M34 5G",
        "Galaxy M14 5G", "Galaxy M44 5G", "Galaxy M53 5G", "Galaxy M33 5G", "Galaxy M23 5G", "Galaxy M13", "Galaxy M04",
        "Galaxy M52 5G", "Galaxy M42 5G", "Galaxy M32 5G", "Galaxy M32", "Galaxy M62", "Galaxy M51", "Galaxy M31s", "Galaxy M31",
        "Galaxy M21", "Galaxy M11", "Galaxy M01", "Galaxy M40", "Galaxy M30s", "Galaxy M30", "Galaxy M20", "Galaxy M10"]),
    # v52: tablets
    ("Galaxy Tab S (current)", ["Galaxy Tab S12 Ultra", "Galaxy Tab S12+", "Galaxy Tab S11 Ultra", "Galaxy Tab S11",
                                "Galaxy Tab S10 Ultra", "Galaxy Tab S10+", "Galaxy Tab S10 Lite", "Galaxy Tab S10 FE+", "Galaxy Tab S10 FE"]),
    ("Galaxy Tab S (older)", ["Galaxy Tab S9 Ultra", "Galaxy Tab S9+", "Galaxy Tab S9", "Galaxy Tab S9 FE+", "Galaxy Tab S9 FE",
                              "Galaxy Tab S8 Ultra", "Galaxy Tab S8+", "Galaxy Tab S8", "Galaxy Tab S7 FE", "Galaxy Tab S7+", "Galaxy Tab S7",
                              "Galaxy Tab S6 Lite", "Galaxy Tab S6", "Galaxy Tab S5e", "Galaxy Tab S4"]),
    ("Galaxy Tab A", ["Galaxy Tab A11+", "Galaxy Tab A11", "Galaxy Tab A9+", "Galaxy Tab A9", "Galaxy Tab A8", "Galaxy Tab A7 Lite",
                      "Galaxy Tab A7", "Galaxy Tab A 10.1 (2019)", "Galaxy Tab A 8.0 (2019)"]),
    ("Galaxy Tab Active (rugged)", ["Galaxy Tab Active5 Pro", "Galaxy Tab Active5", "Galaxy Tab Active4 Pro", "Galaxy Tab Active3"]),
]

GOOGLE = [
    ("Pixel 11 family (2026)", ["Pixel 11 Pro Fold", "Pixel 11 Pro XL", "Pixel 11 Pro", "Pixel 11"]),
    ("Pixel 10 family (2025–26)", ["Pixel 10 Pro Fold", "Pixel 10 Pro XL", "Pixel 10 Pro", "Pixel 10", "Pixel 10a"]),
    ("Pixel 9 family (2024–25)", ["Pixel 9 Pro Fold", "Pixel 9 Pro XL", "Pixel 9 Pro", "Pixel 9", "Pixel 9a"]),
    ("Pixel 8 family (2023–24)", ["Pixel 8 Pro", "Pixel 8", "Pixel 8a"]),
    ("Pixel 7 family (2022–23)", ["Pixel 7 Pro", "Pixel 7", "Pixel 7a"]),
    ("Pixel 6 family (2021–22)", ["Pixel 6 Pro", "Pixel 6", "Pixel 6a"]),
    ("Pixel Fold and Tablet", ["Pixel Tablet", "Pixel Fold"]),
    ("Older Pixels", ["Pixel 5a (5G)", "Pixel 5", "Pixel 4a (5G)", "Pixel 4a", "Pixel 4 XL", "Pixel 4",
                      "Pixel 3a XL", "Pixel 3a", "Pixel 3 XL", "Pixel 3"]),
]

# Brands shown in the picker, in order. "Other" has no list: the text box is used.
BRANDS = ["Apple", "Samsung", "Google", "Motorola", OTHER_BRAND]


def _clean_models(raw) -> list:
    out, seen = [], set()
    for m in raw if isinstance(raw, list) else []:
        text = " ".join(str(m or "").split())[:60]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
        if len(out) >= MAX_CUSTOM:
            break
    return out


def catalog(custom: dict | None = None) -> dict:
    """{brand: [{"group": title, "models": [...]}]} for the pickers. `custom` is get_custom_phone_models(db)."""
    cat = {
        "Apple": [{"group": g, "models": list(m)} for g, m in APPLE],
        "Samsung": [{"group": g, "models": list(m)} for g, m in SAMSUNG],
        "Google": [{"group": g, "models": list(m)} for g, m in GOOGLE],
        "Motorola": [{"group": "Motorola", "models": list(PHONE_MODELS.get("Motorola", []))}],
        OTHER_BRAND: [],
    }
    known = {b: {m.lower() for g in groups for m in g["models"]} for b, groups in cat.items()}
    extra_brands = []
    for brand, models in (custom or {}).items():
        brand = " ".join(str(brand or "").split())[:40]
        models = _clean_models(models)
        if not brand or brand == OTHER_BRAND or not models:
            continue
        have = known.setdefault(brand, set())
        fresh = [m for m in models if m.lower() not in have]
        if not fresh:
            continue
        if brand not in cat:
            cat[brand] = []
            extra_brands.append(brand)
        cat[brand].insert(0, {"group": "Added by you", "models": fresh})
    # brand order: the built-in ones, then any staff-added brand, "Other" last
    order = [b for b in BRANDS if b in cat and b != OTHER_BRAND] + extra_brands + [OTHER_BRAND]
    return {b: cat[b] for b in order}


def all_models(cat: dict | None = None) -> list:
    cat = cat or catalog()
    return [m for groups in cat.values() for g in groups for m in g["models"]]


def payload(custom: dict | None = None) -> list:
    """The same catalogue as an ordered list [{"brand": ..., "groups": [...]}, ...]. JSON objects lose their order on
    the way to the page (Jinja sorts keys), a list does not."""
    return [{"brand": b, "groups": g} for b, g in catalog(custom).items()]
