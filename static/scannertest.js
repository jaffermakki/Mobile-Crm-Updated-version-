/* v52: the Scanner test page. Checks, on the phone itself, everything the barcode scanner depends on, then runs the
   real scanner (static/scan.js) so a person can confirm a barcode is actually read. Nothing is sent anywhere: all of
   the checks run in this browser, and "Copy results" puts a text summary on the clipboard to paste into a message.

   The camera and the reading of a barcode can only be proved on a real device; this page is how the owner does that. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var checks = [], cam = null, reads = [], camStream = null, camDone = false, camTimer = null, camPending = false;
  var ZX_URL = '/static/vendor/zxing-library-0.23.0.min.js';
  var WASM_JS = '/static/vendor/zxing-wasm/zxing-wasm-reader-3.1.7.iife.js';
  var WASM_BIN = '/static/vendor/zxing-wasm/zxing_reader-3.1.7.wasm';
  var WANT = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128'];

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

  /* ── a list of ticked / crossed / info lines ── */
  function line(list, kind, title, detail) {
    var li = el('li', 'st-' + kind), mark = el('span', 'st-mark', kind === 'ok' ? '✓' : kind === 'bad' ? '✗' : kind === 'warn' ? '!' : 'i');
    mark.setAttribute('aria-hidden', 'true');
    var body = el('span', 'st-body'); body.appendChild(el('strong', '', title));
    if (detail) { body.appendChild(document.createElement('br')); body.appendChild(el('span', 'st-detail', detail)); }
    li.appendChild(mark); li.appendChild(body);
    var sr = el('span', 'st-sr', kind === 'ok' ? ' (passed)' : kind === 'bad' ? ' (problem)' : kind === 'warn' ? ' (check this)' : ''); body.appendChild(sr);
    list.appendChild(li);
    return li;
  }
  function record(kind, title, detail) { checks.push({ kind: kind, title: title, detail: detail || '' }); line($('st-checks'), kind, title, detail); }

  /* ── EAN-13 maths, drawing ── */
  function eanCheckDigit(d12) {
    var sum = 0, i;
    for (i = 0; i < 12; i++) sum += (+d12.charAt(i)) * (i % 2 === 0 ? 1 : 3);
    return String((10 - (sum % 10)) % 10);
  }
  function gtinOk(code) {
    if (!/^\d+$/.test(code) || [8, 12, 13, 14].indexOf(code.length) < 0) return null;       // not a retail number: say nothing
    var body = code.slice(0, -1), sum = 0, i, w;
    for (i = 0; i < body.length; i++) {
      w = ((body.length - i) % 2 === 1) ? 3 : 1;
      sum += (+body.charAt(i)) * w;
    }
    return String((10 - (sum % 10)) % 10) === code.charAt(code.length - 1);
  }
  var L = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011'];
  var G = ['0100111', '0110011', '0011011', '0100001', '0011101', '0111001', '0000101', '0010001', '0001001', '0010111'];
  var R = ['1110010', '1100110', '1101100', '1000010', '1011100', '1001110', '1010000', '1000100', '1001000', '1110100'];
  var PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG', 'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL'];
  function ean13Modules(code) {
    var first = +code.charAt(0), par = PARITY[first], bits = '101', i;
    for (i = 1; i <= 6; i++) bits += (par.charAt(i - 1) === 'L' ? L : G)[+code.charAt(i)];
    bits += '01010';
    for (i = 7; i <= 12; i++) bits += R[+code.charAt(i)];
    return bits + '101';
  }
  function drawEan13(host, code) {
    var NS = 'http://www.w3.org/2000/svg', mod = 3, quiet = 9, bits = ean13Modules(code), h = 110, w = (bits.length + quiet * 2) * mod;
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + (h + 26)); svg.setAttribute('width', w); svg.setAttribute('height', h + 26);
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Sample barcode ' + code);
    var bg = document.createElementNS(NS, 'rect'); bg.setAttribute('width', w); bg.setAttribute('height', h + 26); bg.setAttribute('fill', '#fff'); svg.appendChild(bg);
    var i, run = 0;
    for (i = 0; i <= bits.length; i++) {
      if (i < bits.length && bits.charAt(i) === '1') { run++; continue; }
      if (run) {
        var r = document.createElementNS(NS, 'rect');
        r.setAttribute('x', (quiet + i - run) * mod); r.setAttribute('y', 6); r.setAttribute('width', run * mod); r.setAttribute('height', h - 6); r.setAttribute('fill', '#000');
        svg.appendChild(r); run = 0;
      }
    }
    var t = document.createElementNS(NS, 'text');
    t.setAttribute('x', w / 2); t.setAttribute('y', h + 18); t.setAttribute('text-anchor', 'middle'); t.setAttribute('font-size', '15'); t.setAttribute('font-family', 'monospace');
    t.setAttribute('fill', '#000'); t.textContent = code.charAt(0) + ' ' + code.slice(1, 7) + ' ' + code.slice(7);
    svg.appendChild(t);
    host.textContent = ''; host.appendChild(svg);
  }

  /* ── 1: the phone and browser ── */
  function phoneInfo() {
    var ua = navigator.userAgent || '', os = /iPhone|iPad|iPod/.test(ua) ? 'iPhone/iPad' : /Android/.test(ua) ? 'Android' : /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : 'Unknown system';
    var br = /EdgA?\//.test(ua) ? 'Edge' : /SamsungBrowser/.test(ua) ? 'Samsung Internet' : /FxiOS|Firefox\//.test(ua) ? 'Firefox' : /CriOS|Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Unknown browser';
    return { os: os, browser: br, ua: ua };
  }

  function loadZX() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script'); s.src = ZX_URL;
      s.onload = function () { resolve(window.ZXing); }; s.onerror = function () { reject(new Error('could not load')); };
      document.head.appendChild(s);
    });
  }

  function loadWasmReader() {                                                          // starts the fast reader and makes it read a tiny blank picture
    return new Promise(function (resolve, reject) {
      if (typeof WebAssembly !== 'object') { reject(new Error('no WebAssembly')); return; }
      function go() {
        var Z = window.ZXingWASM;
        if (!Z || !Z.readBarcodes) { reject(new Error('incomplete')); return; }
        try {
          Z.prepareZXingModule({ overrides: { locateFile: function (path, prefix) { return /\.wasm$/.test(path) ? WASM_BIN : prefix + path; } }, fireImmediately: true });
          Z.readBarcodes(new ImageData(8, 8), { formats: ['EAN13'] }).then(function () { resolve(); }, reject);
        } catch (e) { reject(e); }
      }
      if (window.ZXingWASM) { go(); return; }
      var sc = document.createElement('script'); sc.src = WASM_JS; sc.onload = go; sc.onerror = function () { reject(new Error('load')); };
      document.head.appendChild(sc);
    });
  }

  function runEnvironment() {
    var info = phoneInfo(), standalone = false;
    try { standalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true; } catch (e) { /* ignore */ }
    record('info', 'This device', info.os + ' · ' + info.browser + ' · screen ' + screen.width + '×' + screen.height + ' @' + (window.devicePixelRatio || 1) + 'x'
      + (standalone ? ' · opened from the home-screen icon' : ' · opened in the browser'));

    if (window.isSecureContext) record('ok', 'Secure connection', location.protocol + '//' + location.host);
    else record('bad', 'Not a secure connection', 'The camera is blocked on plain http. Open the shop\'s https address (the live site) on the phone and run this test again.');

    var hasCam = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    if (hasCam) record('ok', 'This browser can use a camera');
    else record('bad', 'This browser cannot open the camera', window.isSecureContext ? 'Try Safari on iPhone or Chrome on Android.' : 'This is most likely because the connection is not secure (see above).');

    if (hasCam && navigator.permissions && navigator.permissions.query) {
      navigator.permissions.query({ name: 'camera' }).then(function (p) {
        if (p.state === 'granted') record('ok', 'Camera permission is already allowed for this site');
        else if (p.state === 'denied') record('bad', 'The camera is blocked for this site', 'Open the browser\'s site settings, set Camera to Allow for this site, then run the test again.');
        else record('info', 'The browser will ask for camera permission', 'When it asks, tap Allow.');
        finish();
      }).catch(function () { record('info', 'Camera permission cannot be checked in this browser', 'The camera test below will ask if needed.'); finish(); });
    }

    if (window.BarcodeDetector && window.BarcodeDetector.getSupportedFormats) {
      window.BarcodeDetector.getSupportedFormats().then(function (list) {
        var usable = WANT.filter(function (f) { return list.indexOf(f) >= 0; });
        if (usable.length) record('ok', 'The phone has its own barcode reader', 'Reads: ' + usable.join(', ').replace(/_/g, '-'));
        else record('info', 'The phone\'s own barcode reader does not read shop barcodes', 'The scanner library is used instead.');
        finish();
      }).catch(function () { record('info', 'The phone\'s own barcode reader could not be checked', 'The scanner library is used instead.'); finish(); });
    } else {
      record('info', 'No built-in barcode reader in this browser', 'This is normal on iPhone. The scanner library that comes with this site is used instead.');
    }

    loadWasmReader().then(function () {
      record('ok', 'The barcode reader started', 'The fast reader (WebAssembly) is served from this site; nothing comes from another website.');
      finish();
    }).catch(function () {
      loadZX().then(function (ZX) {
        if (ZX && ZX.BrowserMultiFormatReader && ZX.DecodeHintType && ZX.BarcodeFormat) record('warn', 'Only the older barcode reader can run on this phone', 'The fast reader needs a newer browser (iPhone: iOS 16.4 or later). The older reader still works for large, well-lit barcodes. Use "Take a photo instead" on the scan screen for small labels like an IMEI.');
        else record('bad', 'The scanner library loaded but looks incomplete', 'Reload the page. If it keeps happening, the file on the server is damaged.');
        finish();
      }).catch(function () { record('bad', 'The scanner library could not be loaded', 'Check the phone is online and reload the page.'); finish(); });
    });

    if (navigator.onLine === false) record('warn', 'The phone says it is offline', 'Connect to Wi-Fi or mobile data. Scanning still reads codes, but looking them up needs the connection.');

    $('st-sample-code').textContent = SAMPLE;
    drawEan13($('st-sample'), SAMPLE);
  }

  var SAMPLE = '590123412345' + eanCheckDigit('590123412345');

  /* ── 2: the camera ── */
  function stopCam() {
    if (camTimer) { clearInterval(camTimer); camTimer = null; }
    if (camPending) {     /* stopped before the first picture was checked: that is not a failure, the test just is not finished */
      camPending = false; cam = null;
      line($('st-cam-out'), 'info', 'Camera stopped before the picture check finished', 'Run the camera test again and wait for the picture size to appear.');
      finish();
    }
    if (camStream) { camStream.getTracks().forEach(function (t) { t.stop(); }); camStream = null; }
    var v = $('st-video'); try { v.srcObject = null; } catch (e) { /* ignore */ }
    v.hidden = true; $('st-cam-stop').hidden = true; $('st-cam').hidden = false; $('st-cam').disabled = false;
  }

  function runCamera() {
    var out = $('st-cam-out'); out.textContent = ''; cam = { ok: false, notes: [] };
    $('st-cam').disabled = true;
    var t0 = Date.now(), v = $('st-video');
    camPending = true;
    var bad = function (title, detail) { line(out, 'bad', title, detail); cam.notes.push('✗ ' + title + (detail ? ' — ' + detail : '')); finish(); $('st-cam').disabled = false; };
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) { bad('No camera access in this browser', window.isSecureContext ? '' : 'The connection is not secure.'); return; }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false }).then(function (stream) {
      camStream = stream; v.srcObject = stream; v.hidden = false; $('st-cam').hidden = true; $('st-cam-stop').hidden = false;
      var track = stream.getVideoTracks()[0], st = track.getSettings ? track.getSettings() : {}, caps = {};
      try { caps = track.getCapabilities ? track.getCapabilities() : {}; } catch (e) { caps = {}; }
      var say = function (kind, title, detail) { line(out, kind, title, detail); cam.notes.push((kind === 'ok' ? '✓ ' : kind === 'bad' ? '✗ ' : kind === 'warn' ? '! ' : '· ') + title + (detail ? ' — ' + detail : '')); };
      say('ok', 'The camera opened', (track.label || 'camera') + ' · took ' + (Date.now() - t0) + ' ms');
      var facing = st.facingMode || '', label = (track.label || '').toLowerCase();
      if (facing === 'user' || /front|user|facetime/.test(label)) say('warn', 'The front (selfie) camera opened', 'Barcodes are much easier to read with the back camera. If this phone has one, check the browser is allowed to use it.');
      else say('ok', 'The back camera is in use');
      var play = v.play ? v.play() : null;
      Promise.resolve(play).catch(function () { /* autoplay may need a tap; the frame check below says so */ });
      var waited = 0, timer = setInterval(function () {
        waited += 200;
        if (v.videoWidth > 0 && v.readyState >= 2) {
          clearInterval(timer); camTimer = null; camPending = false;
          var w = v.videoWidth, hgt = v.videoHeight;
          if (Math.max(w, hgt) >= 1000) say('ok', 'Picture size ' + w + '×' + hgt, st.frameRate ? Math.round(st.frameRate) + ' frames a second' : '');
          else say('warn', 'Picture is small: ' + w + '×' + hgt, 'Small pictures make thin barcodes harder to read. Hold the phone a bit closer.');
          cam.ok = true; cam.size = w + '×' + hgt; finish();
          $('st-cam').disabled = false;
        } else if (waited >= 6000) {
          clearInterval(timer); camTimer = null; camPending = false;
          say('bad', 'The camera opened but no picture arrived', 'Close other apps that may be using the camera and try again.');
          $('st-cam').disabled = false; finish();
        }
      }, 200);
      camTimer = timer;
      say('info', caps.torch ? 'The camera light (torch) can be switched on from the scanner' : 'No light control from the browser on this camera', caps.torch ? '' : 'Use the room light, or the phone\'s own flashlight, in dim places.');
      if (caps.focusMode && caps.focusMode.indexOf && caps.focusMode.indexOf('continuous') >= 0) say('ok', 'Continuous focus is available');
      if (caps.zoom && caps.zoom.max) say('info', 'Zoom is available', 'Up to ' + Math.round(caps.zoom.max * 10) / 10 + '×');
      if (navigator.mediaDevices.enumerateDevices) {
        navigator.mediaDevices.enumerateDevices().then(function (devs) {
          var n = devs.filter(function (d) { return d.kind === 'videoinput'; }).length;
          say('info', n + ' camera' + (n === 1 ? '' : 's') + ' found on this device');
        }).catch(function () { /* ignore */ });
      }
    }).catch(function (err) {
      var n = err && err.name, d = '';
      if (n === 'NotAllowedError' || n === 'SecurityError') d = 'The camera is blocked. Allow the camera for this site in the browser settings, then try again.';
      else if (n === 'NotFoundError' || n === 'OverconstrainedError') d = 'No camera was found on this device.';
      else if (n === 'NotReadableError' || n === 'AbortError') d = 'Another app is using the camera. Close it and try again.';
      else d = 'The camera could not be started' + (n ? ' (' + n + ')' : '') + '.';
      bad('The camera did not open', d);
    });
  }

  /* ── 3: reading a real barcode with the real scanner ── */
  function logRead(code, ms, engine) {
    var ok = gtinOk(code);
    reads.push({ code: code, ok: ok, ms: ms, engine: engine, at: new Date() });
    var ul = $('st-reads'); ul.textContent = '';
    reads.slice(-8).reverse().forEach(function (r) {
      var kind = r.ok === false ? 'warn' : 'ok';
      line(ul, kind, r.code, (r.ok === null ? 'not a standard shop barcode length' : r.ok ? 'check digit is correct' : 'check digit is WRONG — a misread, or a damaged label') + ' · ' + r.ms + ' ms after the scanner opened');
    });
    finish();
  }

  function openScanner() {
    if (!window.TPScan) { alert('The scanner is still loading. Try again in a moment.'); return; }
    var t0 = Date.now();
    window.TPScan.open({ title: 'Scanner test — point at any barcode', continuous: true, onCode: function (code) {
      var ok = gtinOk(code);
      logRead(code, Date.now() - t0, window.TPScan.engine ? window.TPScan.engine() : '');
      return { ok: ok !== false, message: 'Read ' + code + (ok === false ? ' — but the check digit is wrong' : ok ? ' ✓' : '') };
    } });
  }

  /* ── 4: the verdict and the text summary ── */
  function verdict() {
    var bads = checks.filter(function (c) { return c.kind === 'bad'; });
    var campass = cam && cam.ok, good = reads.filter(function (r) { return r.ok !== false; });
    if (bads.length) return { kind: 'bad', text: 'Not ready: ' + bads.map(function (b) { return b.title; }).join('; ') + '.' };
    if (cam && !cam.ok) return { kind: 'bad', text: 'The camera test did not pass. See step 2.' };
    if (campass && good.length) return { kind: 'ok', text: 'Camera scanning works on this phone. ' + good.length + ' barcode' + (good.length === 1 ? '' : 's') + ' read.' };
    if (campass) return { kind: 'warn', text: 'The camera works. Now scan a real barcode in step 3 to finish the test.' };
    return { kind: 'info', text: 'Not finished. Run step 2 (camera), then step 3 (scan a barcode).' };
  }

  function report() {
    var info = phoneInfo(), v = verdict(), lines = ['TechPro+ scanner test — ' + new Date().toLocaleString(), 'Result: ' + v.text, '',
      'Device: ' + info.os + ' / ' + info.browser, 'Screen: ' + screen.width + 'x' + screen.height + ' @' + (window.devicePixelRatio || 1) + 'x', 'Browser: ' + info.ua, '', 'Checks:'];
    checks.forEach(function (c) { lines.push('  ' + (c.kind === 'ok' ? '[ok]  ' : c.kind === 'bad' ? '[FAIL]' : c.kind === 'warn' ? '[warn]' : '[info]') + ' ' + c.title + (c.detail ? ' — ' + c.detail : '')); });
    lines.push('', 'Camera test:');
    if (cam) cam.notes.forEach(function (n) { lines.push('  ' + n); }); else lines.push('  (not run)');
    lines.push('', 'Barcodes read: ' + reads.length + (window.TPScan && window.TPScan.engine && window.TPScan.engine() ? ' using the ' + window.TPScan.engine() : ''));
    reads.forEach(function (r) { lines.push('  ' + r.code + ' · ' + (r.ok === null ? 'non-retail length' : r.ok ? 'check digit ok' : 'CHECK DIGIT WRONG') + ' · ' + r.ms + ' ms'); });
    return lines.join('\n');
  }

  function finish() {
    var v = verdict(), box = $('st-verdict');
    box.className = 'st-verdict st-v-' + v.kind; box.textContent = v.text;
    $('st-report').textContent = report();
  }

  function copy() {
    var text = report(), btn = $('st-copy'), done = function (msg) { btn.textContent = msg; setTimeout(function () { btn.textContent = 'Copy results'; }, 2000); };
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(function () { done('Copied'); }, function () { fallback(); }); }
    else fallback();
    function fallback() {
      var ta = $('st-report'), range = document.createRange();
      range.selectNodeContents(ta); var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      try { document.execCommand('copy'); done('Copied'); } catch (e) { done('Select the text and copy it'); }
    }
  }

  function boot() {
    if (!$('st-checks')) return;
    runEnvironment();
    $('st-cam').addEventListener('click', runCamera);
    $('st-cam-stop').addEventListener('click', stopCam);
    $('st-scan').addEventListener('click', function () { stopCam(); openScanner(); });
    $('st-copy').addEventListener('click', copy);
    $('st-print').addEventListener('click', function () { var d = $('st-sample').closest ? $('st-sample').closest('details') : null; if (d) d.open = true; window.print(); });
    window.addEventListener('pagehide', stopCam);
    finish();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
