"""v54.1 — the stronger barcode reader (ZXing-C++ in WebAssembly, served from this site) and the IMEI scan options.

A server cannot prove a phone's camera reads a label; these tests lock the parts a server *can* check: the files are
there and served correctly, the security policy allows WebAssembly (and still not eval), the scan buttons ask for IMEI
mode, the page scripts are the new version, and the script keeps its safety rules."""
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WASM_DIR = ROOT / "static/vendor/zxing-wasm"
WASM_JS = "/static/vendor/zxing-wasm/zxing-wasm-reader-3.1.7.iife.js"
WASM_BIN = "/static/vendor/zxing-wasm/zxing_reader-3.1.7.wasm"
IMEI_BUTTONS = {"templates/repairs.html": "rp-imei", "templates/repair_detail.html": "rd-imei",
                "templates/trade_ins.html": "ti-imei", "templates/partials/pos_cart.html": "pos-imei-"}


def test_the_reader_files_are_in_the_build_and_are_the_ones_we_checked():
    assert (WASM_DIR / "zxing-wasm-LICENSE.txt").exists()
    assert hashlib.sha256((WASM_DIR / "zxing-wasm-reader-3.1.7.iife.js").read_bytes()).hexdigest() == "1c61f43ac68da5c242f58c0d05763c295cd8a605d46d14bc31821eee7160e778"
    assert hashlib.sha256((WASM_DIR / "zxing_reader-3.1.7.wasm").read_bytes()).hexdigest() == "33c8c50be17d0bb05454b4fd3ffb5b5ab9a8e8d1c0ebeb21a1adf4a89c1a8b59"
    assert "zxing-wasm" in (ROOT / "static/vendor/README.txt").read_text()


def test_the_reader_files_are_served_from_this_site(owner_client):
    js = owner_client.get(WASM_JS)
    assert js.status_code == 200 and len(js.content) > 20_000
    wasm = owner_client.get(WASM_BIN)
    assert wasm.status_code == 200 and wasm.content[:4] == b"\x00asm" and len(wasm.content) > 500_000
    assert "wasm" in wasm.headers["content-type"], "browsers refuse to stream-compile a .wasm that is not application/wasm"


def test_the_policy_allows_webassembly_but_still_never_eval(owner_client):
    csp = owner_client.get("/stock-count").headers["content-security-policy"]
    script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    assert "'wasm-unsafe-eval'" in script_src and "'unsafe-eval'" not in script_src
    assert "connect-src 'self'" in csp and "default-src 'self'" in csp                   # nothing opened up to other sites
    assert "https:" not in script_src and "*" not in script_src


def test_the_policy_is_the_same_on_every_scanning_page(owner_client):
    for path in ("/repairs", "/pos", "/trade-ins", "/scanner-test", "/stock-count", "/products"):
        r = owner_client.get(path)
        assert r.status_code == 200, path
        assert "'wasm-unsafe-eval'" in r.headers["content-security-policy"], path


def test_the_scanner_script_uses_the_wasm_reader_and_keeps_the_js_one_as_a_fallback():
    js = (ROOT / "static/scan.js").read_text()
    assert WASM_JS in js and WASM_BIN in js and "/static/vendor/zxing-library-0.23.0.min.js" in js
    assert "readBarcodes" in js and "prepareZXingModule" in js
    assert "https://" not in js and "http://" not in js
    for refused in ("code_39", "Code39", "CODE_39", "ITF", "QR_CODE", "qr_code"):      # still only formats that carry a check digit
        assert refused not in js
    assert "eval(" not in js and "new Function" not in js


def test_the_scanner_has_the_photo_and_zoom_helpers():
    js = (ROOT / "static/scan.js").read_text()
    assert "Take a photo instead" in js and "capture" in js and "image/*" in js
    assert "Zoom 2" in js and "focusMode" in js and "1920" in js
    assert "'imei'" in js and "luhnOk" in js


def test_a_phone_that_claims_a_native_reader_but_cannot_use_it_does_not_block_the_others():
    js = (ROOT / "static/scan.js").read_text()
    assert "fails" in js and ">= 8" in js
    assert re.search(r"Promise\.all\(\[nativeP, wasmStarted\]\)", js), "the readers must start together, not one after the other"


@pytest.mark.parametrize("path,ident", list(IMEI_BUTTONS.items()))
def test_every_imei_scan_button_asks_for_imei_mode(path, ident):
    text = (ROOT / path).read_text()
    m = re.search(r'<button[^>]*data-scan-fill="%s[^"]*"[^>]*>' % re.escape(ident), text)
    assert m, path
    assert 'data-scan-prefer="imei"' in m.group(0), path


def test_product_barcode_buttons_do_not_ask_for_imei_mode():
    for path in ("templates/products.html", "templates/product_edit.html"):
        assert "data-scan-prefer" not in (ROOT / path).read_text(), path


def test_the_click_handler_passes_the_preference_through():
    js = (ROOT / "static/scan.js").read_text()
    assert "data-scan-prefer" in js and "prefer:" in js


def test_every_page_loads_the_new_script_version():
    for f in (ROOT / "templates").rglob("*.html"):
        text = f.read_text(encoding="utf-8")
        assert "scan.js?v=52" not in text and "scan.js?v=51" not in text, f.name
        for m in re.finditer(r"/static/scan\.js\?v=(\d+)", text):
            assert int(m.group(1)) >= 54, f.name


def test_the_test_page_checks_the_new_reader(owner_client):
    js = owner_client.get("/static/scannertest.js").text
    assert WASM_JS in js and WASM_BIN in js and "WebAssembly" in js
    assert "iOS 16.4" in js                                                              # tells the owner why an older iPhone falls back


def test_the_scanner_test_page_loads_the_scripts(owner_client):
    html = owner_client.get("/scanner-test").text
    assert "/static/scan.js?v=54" in html and "/static/scannertest.js?v=54" in html


def test_the_about_page_shows_the_new_version(owner_client):
    from app import version as V
    assert V.VERSION == "54.1"
    assert V.VERSION in owner_client.get("/about").text


def test_help_tells_how_to_scan_a_small_imei_label(owner_client):
    text = owner_client.get("/help").text
    assert "Take a photo instead" in text and "IMEI" in text
