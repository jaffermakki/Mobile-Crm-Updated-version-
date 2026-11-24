"""v52 — the Scanner test page that the owner opens on a real phone (the camera itself can only be proved there)."""
import json
import os
import re
import shutil
import subprocess

import pytest

from app import barcodes as BC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "scannertest.js")
SCAN_JS = os.path.join(ROOT, "static", "scan.js")


def test_signed_out_visitors_are_sent_to_sign_in(anon_client):
    r = anon_client.get("/scanner-test", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("who", ["owner_client", "manager_client", "cashier_client", "technician_client"])
def test_every_role_can_open_it(request, who):
    r = request.getfixturevalue(who).get("/scanner-test")
    assert r.status_code == 200
    for want in ("Scanner test", "1. This phone and browser", "2. The camera", "3. Scan a barcode", "4. Send me the result",
                 'id="st-cam"', 'id="st-scan"', 'id="st-copy"', 'id="st-sample"', 'id="st-verdict"'):
        assert want in r.text, want


def test_it_loads_the_real_scanner_and_its_own_script_from_this_site(owner_client):
    html = owner_client.get("/scanner-test").text
    assert "/static/scan.js?v=54" in html and "/static/scannertest.js?v=54" in html
    assert not re.search(r"""src=["']https?://""", html)


def test_the_page_says_a_server_cannot_prove_the_camera_works(owner_client):
    assert "only a real phone" in owner_client.get("/scanner-test").text.lower()


def test_help_links_to_it_next_to_the_camera_section(owner_client):
    html = owner_client.get("/help").text
    assert 'href="/scanner-test"' in html
    assert html.index('id="scan"') < html.index('href="/scanner-test"')


def test_every_page_that_uses_the_scanner_loads_the_new_version():
    tpl = os.path.join(ROOT, "templates")
    for name in os.listdir(tpl):
        if name.endswith(".html"):
            text = open(os.path.join(tpl, name), encoding="utf-8").read()
            assert "scan.js?v=51" not in text, name


def test_the_scripts_are_served(owner_client):
    for path in ("/static/scannertest.js", "/static/scan.js", "/static/vendor/zxing-library-0.23.0.min.js"):
        r = owner_client.get(path)
        assert r.status_code == 200 and len(r.content) > 1000, path


def test_the_test_page_sends_nothing_anywhere_and_writes_no_html_from_data():
    src = open(JS, encoding="utf-8").read()
    for banned in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "innerHTML", "outerHTML", "document.write", "eval(", "localStorage"):
        assert banned not in src, banned
    urls = re.findall(r"https?://[^\s'\"]+", src)
    assert all(u.startswith("http://www.w3.org/2000/svg") for u in urls), urls


def test_the_real_scanner_reports_which_reader_it_uses():
    src = open(SCAN_JS, encoding="utf-8").read()
    assert "engine: function" in src and "BarcodeDetector" in src
    assert "ZXing-C++ (WebAssembly" in src and "ZXing JavaScript library (fallback)" in src      # v54.1: the three readers, by name


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_the_pages_barcode_maths_agrees_with_the_servers():
    """The page validates check digits and draws the sample barcode itself; it must agree with app/barcodes.py."""
    harness = r"""
    const fs = require('fs');
    global.window = {}; global.document = {readyState: 'x', addEventListener() {}, getElementById() { return null; }};
    global.navigator = {}; global.screen = {};
    let src = fs.readFileSync(process.argv[1], 'utf8');
    src = src.replace('if (document.readyState', 'global.__t = {eanCheckDigit, gtinOk, ean13Modules, SAMPLE: () => SAMPLE}; if (document.readyState');
    eval(src);
    const t = global.__t, codes = JSON.parse(process.argv[2]);
    console.log(JSON.stringify({sample: t.SAMPLE(), bits: t.ean13Modules(t.SAMPLE()), ok: codes.map(c => t.gtinOk(c))}));
    """
    codes = ["5901234123457", "4006381333931", "4006381333932", "036000291452", "036000291453", "96385074", "96385075", "12345", "abc", "0000000000000"]
    out = subprocess.run(["node", "-e", harness, JS, json.dumps(codes)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    for code, js_ok in zip(codes, data["ok"]):
        server = BC.check_digit_ok(code)
        assert js_ok == server, (code, js_ok, server)
    assert data["sample"] == "5901234123457" and BC.check_digit_ok(data["sample"]) is True
    bits = data["bits"]
    assert len(bits) == 95 and bits.startswith("101") and bits.endswith("101") and bits[45:50] == "01010"
    # every digit must come out as a 7-module pattern with the right ink/space shape (left side starts with 0, right with 1)
    left, right = bits[3:45], bits[50:92]
    assert all(left[i * 7] == "0" and left[i * 7 + 6] == "1" for i in range(6))
    assert all(right[i * 7] == "1" and right[i * 7 + 6] == "0" for i in range(6))


def test_stopping_the_camera_early_is_not_reported_as_a_failure():
    """Found in the real-browser pass: tapping Stop before the picture check ran made the page say 'The camera test did not
    pass' six seconds later and left 'Test the camera' disabled. Stopping must cancel the pending check, leave the verdict at
    'not finished' and re-enable the button. (The browser pass drives this for real; this pins the three pieces.)"""
    src = open(JS, encoding="utf-8").read()
    stop = src[src.index("function stopCam()"):src.index("function runCamera()")]
    assert "clearInterval(camTimer)" in stop, "stopping must cancel the pending picture check"
    assert "camPending" in stop and "cam = null" in stop, "an early stop must reset the camera step to 'not finished'"
    assert "$('st-cam').disabled = false" in stop, "the Test button must work again after Stop"
    run = src[src.index("function runCamera()"):src.index("/* ── 3: reading a real barcode")]
    assert run.count("camPending = false") >= 2, "both ways the picture check can end must clear the pending flag"
    assert "camTimer = timer" in run
