"""v53.1: which products the repair ticket's "Add Part" picker offers.

The picker used to list the whole catalogue, so a technician looking for a screen scrolled past cases, chargers and
screen protectors. It now shows only repair parts (categories Screen, Battery and Part), with the ones that fit the phone
on the ticket first. "Show all products" brings the rest back for the odd job that needs one.

This only decides what the dropdown LISTS. The route that adds a part is unchanged and still accepts any product, so
nothing already saved or scripted is affected.
"""
import re

from .product_const import CATEGORY_LABELS

# What counts as a repair part, in the order the groups are shown.
REPAIR_CATEGORIES = ("SCREEN", "BATTERY", "PART")

_BRANDS = {"apple", "samsung", "google", "motorola", "oneplus", "huawei", "xiaomi", "nokia", "lg", "sony", "oppo", "tcl"}
# Words that turn one model into a different one ("iPhone 15" vs "iPhone 15 Pro"): a part for the shorter name must not
# be offered as a fit for the longer one.
_MODEL_SUFFIX = {"pro", "max", "plus", "ultra", "mini", "fe", "lite", "se", "edge", "fold", "flip", "air", "xl"}


def _toks(text) -> list:
    # A trailing "+" stays on the word: "S24+" is a different phone from "S24".
    return re.findall(r"[a-z0-9]+\+?", (text or "").lower())


def _drop_brand(toks: list, brand_toks: list) -> list:
    if toks and (toks[0] in _BRANDS or (brand_toks and toks[0] == brand_toks[0])):
        return toks[1:]
    return toks


def fits_device(product, device: str) -> bool:
    """True when `product` is plainly meant for the phone named on the ticket. Used only to float matches to the top."""
    dev = _toks(device)
    if not dev:
        return False
    brand = _toks(getattr(product, "phone_brand", ""))
    dev_nb = _drop_brand(dev, brand)
    model = _drop_brand(_toks(getattr(product, "phone_model", "")), brand)
    if model:
        return bool(dev_nb) and dev_nb == model
    # No model saved on the product: look for the device's words in its name, as long as the next word does not turn it
    # into a different model.
    name = _toks(getattr(product, "name", ""))
    n = len(dev_nb)
    if not n:
        return False
    for i in range(0, len(name) - n + 1):
        if name[i:i + n] == dev_nb:
            nxt = name[i + n] if i + n < len(name) else ""
            if nxt not in _MODEL_SUFFIX:
                return True
    return False


def _label(cat: str) -> str:
    raw = CATEGORY_LABELS.get(cat or "", "")
    # "🖥️ Screen" -> "Screen" (the emoji is noise inside a dropdown group heading)
    words = raw.split(" ", 1)
    plural = {"SCREEN": "Screens", "BATTERY": "Batteries", "PART": "Parts"}
    if cat in plural:
        return plural[cat]
    return (words[1] if len(words) == 2 else raw) or "Other"


def build_picker(products, device: str, show_all: bool = False) -> dict:
    """Split `products` for the dropdown.

    Returns {"fits": [...], "groups": [(label, [...]), ...], "hidden": n, "has_repair_parts": bool}
    `hidden` is how many non-repair products are being left out (0 once show_all is on)."""
    products = list(products)
    repair = [p for p in products if (p.category or "") in REPAIR_CATEGORIES]
    others = [p for p in products if (p.category or "") not in REPAIR_CATEGORIES]
    fits = [p for p in repair if fits_device(p, device)]
    fit_ids = {p.id for p in fits}
    groups = []
    for cat in REPAIR_CATEGORIES:
        items = [p for p in repair if (p.category or "") == cat and p.id not in fit_ids]
        if items:
            groups.append((_label(cat), items))
    if show_all and others:
        by_cat = {}
        for p in others:
            by_cat.setdefault(p.category or "", []).append(p)
        for cat in sorted(by_cat, key=lambda c: (c == "", _label(c))):
            groups.append((_label(cat) if cat else "Other / uncategorised", by_cat[cat]))
    return {"fits": fits, "groups": groups, "hidden": 0 if show_all else len(others), "has_repair_parts": bool(repair)}
