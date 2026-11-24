/* v50: camera barcode scanner, shared by the register, the product pages and stock-count mode.
   v54.1: a stronger reader. The JavaScript ZXing used before could not read a phone's IMEI label when it sat on a dark
   background (a phone box, a hand, a table) and was weak on small, slightly blurry codes. The camera feed is now read by
   ZXing-C++ compiled to WebAssembly (static/vendor/zxing-wasm/, about 4 ms a frame), together with the phone's own reader
   where it has one (Chrome on Android). If a browser cannot run WebAssembly, the old JavaScript ZXing is used instead.
   There is also "Take a photo instead": the phone's own camera app takes a full-resolution picture, which is the most
   reliable way to read a tiny IMEI label. Nothing is loaded from another website and no picture leaves the phone.

   Pages load it with:  <script src="/static/scan.js" data-csrf="…" data-can-link="1" defer></script>
   and use:             TPScan.open({ title, continuous, prefer, onCode(code) -> {ok, message, unknownCode} | undefined, onUnknown(code) })
                        TPScan.linkDialog({ code, onLinked(product) })
   prefer: 'imei' favours a valid 15-digit IMEI when several barcodes are in view.   */
(function () {
  'use strict';
  var me = document.currentScript;
  var CSRF = me ? (me.getAttribute('data-csrf') || '') : '';
  var CAN_LINK = me ? me.getAttribute('data-can-link') === '1' : false;
  var ZX_URL = '/static/vendor/zxing-library-0.23.0.min.js';                         // the JavaScript fallback
  var WASM_JS = '/static/vendor/zxing-wasm/zxing-wasm-reader-3.1.7.iife.js';
  var WASM_BIN = '/static/vendor/zxing-wasm/zxing_reader-3.1.7.wasm';
  var WANT = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128'];                        // BarcodeDetector names: formats that carry a check digit
  var WASM_WANT = ['EAN13', 'EAN8', 'UPCA', 'UPCE', 'Code128'];                        // the same, by ZXing-C++ names
  var COOLDOWN_MS = 1800;                                                             // the same label still in view does not count twice
  var FULL_FRAME_AFTER_MS = 2500;                                                     // then every 4th look uses the whole picture, not just the middle band
  var ui = null, run = null, audio = null, wasmP = null, lastEngineName = '';

  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function build() {
    if (ui) return ui;
    var root = h('div', 'tpscan'); root.hidden = true;
    root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true'); root.setAttribute('aria-label', 'Barcode scanner');
    var video = h('video'); video.setAttribute('playsinline', ''); video.muted = true; video.setAttribute('autoplay', '');
    var top = h('div', 'tpscan-top'), title = h('div', 'tpscan-title'), x = h('button', 'tpscan-x', 'Close'); x.type = 'button';
    top.appendChild(title); top.appendChild(x);
    var frame = h('div', 'tpscan-frame');
    var bottom = h('div', 'tpscan-bottom'), msg = h('div', 'tpscan-msg'); msg.setAttribute('aria-live', 'polite');
    var actions = h('div', 'tpscan-actions');
    var torch = h('button', 'tpscan-btn', 'Light'); torch.type = 'button'; torch.hidden = true;
    var zoom = h('button', 'tpscan-btn', 'Zoom 2×'); zoom.type = 'button'; zoom.hidden = true;
    var photo = h('button', 'tpscan-btn', 'Take a photo instead'); photo.type = 'button';
    var link = h('button', 'tpscan-btn', 'Link this barcode…'); link.type = 'button'; link.hidden = true;
    var file = h('input'); file.type = 'file'; file.accept = 'image/*'; file.setAttribute('capture', 'environment'); file.hidden = true;
    file.setAttribute('aria-label', 'Photo of the barcode');
    actions.appendChild(torch); actions.appendChild(zoom); actions.appendChild(photo); actions.appendChild(link);
    var form = h('form', 'tpscan-manual'), input = h('input'), go = h('button', '', 'Use');
    input.type = 'text'; input.placeholder = 'Or type the number'; input.setAttribute('inputmode', 'numeric'); input.autocomplete = 'off';
    go.type = 'submit'; form.appendChild(input); form.appendChild(go);
    bottom.appendChild(msg); bottom.appendChild(actions); bottom.appendChild(form);
    [video, top, frame, bottom, file].forEach(function (n) { root.appendChild(n); });
    document.body.appendChild(root);
    ui = { root: root, video: video, frame: frame, title: title, msg: msg, torch: torch, zoom: zoom, photo: photo, file: file, link: link, form: form, input: input };
    x.addEventListener('click', function () { close(); });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var code = input.value.replace(/\s+/g, '');
      if (!code || !run) return;
      input.value = '';
      deliver(run, code);
    });
    link.addEventListener('click', function () {
      if (run && run.unknown && run.opts.onUnknown) run.opts.onUnknown(run.unknown);
    });
    photo.addEventListener('click', function () {
      if (!run) return;
      if (run.photoMode) { run.photoMode = false; ui.photo.textContent = 'Take a photo instead'; startLive(run); return; }
      run.photoMode = true; ui.photo.textContent = 'Use the live camera';
      stopLive(run);                                               // free the camera first: the phone's camera app needs it
      say('Take a clear, close photo of the barcode.');
      try { ui.file.value = ''; } catch (e) { /* ignore */ }
      ui.file.click();
    });
    file.addEventListener('change', function () {
      var s = run, f = file.files && file.files[0];
      if (!s || !s.photoMode || !f) return;
      say('Reading the photo…');
      readPhoto(f, s).then(function (code) {
        if (!run || run !== s) return;
        if (code) { onRead(s, code, true); return; }
        say('No barcode found in that photo. Get closer, use good light and keep it sharp. Or type the number below.', 'bad');
      }).catch(function () { if (run === s) say('The photo could not be read. You can still type the number below.', 'bad'); });
    });
    file.addEventListener('cancel', function () {
      if (run && run.photoMode) { run.photoMode = false; ui.photo.textContent = 'Take a photo instead'; startLive(run); }
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && run) close(); });
    document.addEventListener('visibilitychange', function () { if (document.hidden && run && !run.photoMode) close(); });
    window.addEventListener('pagehide', function () { if (run && !run.photoMode) close(); });
    return ui;
  }

  function say(text, kind) { ui.msg.textContent = text || ''; ui.msg.className = 'tpscan-msg' + (kind ? ' ' + kind : ''); }

  function beep() {
    try {
      var C = window.AudioContext || window.webkitAudioContext;
      if (C) {
        audio = audio || new C();
        var o = audio.createOscillator(), g = audio.createGain();
        o.type = 'sine'; o.frequency.value = 1000; g.gain.value = 0.08;
        o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + 0.09);
      }
    } catch (e) { /* no sound is fine */ }
    try { if (navigator.vibrate) navigator.vibrate(40); } catch (e) { /* ignore */ }
  }

  function problem() {
    if (!window.isSecureContext) return 'The camera only works on a secure (https) connection.';
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return 'This browser cannot open the camera.';
    return '';
  }

  function cameraMessage(err) {
    var n = err && err.name;
    if (n === 'NotAllowedError' || n === 'SecurityError') return 'The camera is blocked. Allow the camera for this site in your browser settings, then try again.';
    if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No camera was found on this device.';
    if (n === 'NotReadableError' || n === 'AbortError') return 'The camera is being used by another app. Close it and try again.';
    return 'The camera could not be started. You can still type the number below.';
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = url; s.onload = function () { resolve(); }; s.onerror = function () { reject(new Error('script')); };
      document.head.appendChild(s);
    });
  }

  function loadZX() {                                                                  // the JavaScript fallback reader
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return loadScript(ZX_URL).then(function () { return window.ZXing; });
  }

  function loadWasm() {                                                                // the WebAssembly reader, once per page
    if (wasmP) return wasmP;
    wasmP = (window.ZXingWASM ? Promise.resolve() : loadScript(WASM_JS)).then(function () {
      var Z = window.ZXingWASM;
      if (!Z || !Z.readBarcodes || typeof WebAssembly !== 'object') throw new Error('no wasm');
      Z.prepareZXingModule({ overrides: { locateFile: function (path, prefix) { return /\.wasm$/.test(path) ? WASM_BIN : prefix + path; } }, fireImmediately: true });
      return Z.readBarcodes(new ImageData(8, 8), { formats: ['EAN13'] }).then(function () { return Z; });   // forces the .wasm to load and compile now
    }).catch(function (e) { wasmP = null; throw e; });
    return wasmP;
  }

  function wasmOptions(session, still) {
    return { formats: WASM_WANT, tryHarder: true, tryRotate: true, tryInvert: false, tryDownscale: true, maxNumberOfSymbols: still ? 6 : 4 };
  }

  function luhnOk(d) {
    var sum = 0, i, n;
    for (i = 0; i < d.length; i++) {
      n = +d.charAt(d.length - 1 - i);
      if (i % 2 === 1) { n *= 2; if (n > 9) n -= 9; }
      sum += n;
    }
    return sum % 10 === 0;
  }

  /* Several barcodes in view (a phone box shows IMEI, serial, model...): for an IMEI scan prefer a valid 15-digit IMEI,
     otherwise take the one nearest the middle, where the person is pointing. */
  function choose(results, session, center) {
    var list = (results || []).filter(function (r) { return r && r.text && (r.isValid !== false) && String(r.text).replace(/\s+/g, '').length >= 4; });
    if (!list.length) return '';
    if (session.opts.prefer === 'imei') {
      for (var i = 0; i < list.length; i++) {
        var t = String(list[i].text).replace(/\s+/g, '');
        if (/^\d{15}$/.test(t) && luhnOk(t)) return t;
      }
    }
    var best = list[0], bestD = Infinity;
    if (center) {
      list.forEach(function (r) {
        var p = r.position;
        if (!p || !p.topLeft || !p.bottomRight) return;
        var cx = (p.topLeft.x + p.bottomRight.x) / 2, cy = (p.topLeft.y + p.bottomRight.y) / 2;
        var d = (cx - center.x) * (cx - center.x) + (cy - center.y) * (cy - center.y);
        if (d < bestD) { bestD = d; best = r; }
      });
    }
    return String(best.text);
  }

  function onRead(session, raw, fromPhoto) {
    var code = String(raw || '').replace(/\s+/g, '');
    if (!code || code.length < 4 || session.stopped || (session.busy && !fromPhoto)) return;
    var now = Date.now();
    if (code === session.lastCode && now - session.lastAt < COOLDOWN_MS && !fromPhoto) { session.lastAt = now; return; }
    session.lastCode = code; session.lastAt = now;
    beep();
    deliver(session, code);
  }

  function deliver(session, code) {
    var opts = session.opts;
    if (!opts.continuous) {
      close();
      try { opts.onCode(code); } catch (e) { /* the page shows its own errors */ }
      return;
    }
    session.busy = true;
    ui.link.hidden = true; session.unknown = '';
    say('Looking up ' + code + '…');
    Promise.resolve().then(function () { return opts.onCode(code); }).then(function (r) {
      r = r || {};
      say(r.message || '', r.ok === false ? 'bad' : 'good');
      if (r.ok === false && r.unknownCode && opts.onUnknown) { session.unknown = r.unknownCode; ui.link.hidden = false; }
    }).catch(function () { say('Something went wrong. Try again.', 'bad'); })
      .then(function () { session.busy = false; session.lastAt = Date.now(); });
  }

  function engineReady(session, name) {
    if (session.engines.indexOf(name) < 0) session.engines.push(name);
    if (!session.stopped && !session.photoMode) say('Point the camera at a barcode.');
  }

  /* ── reader 1: the phone's own (Chrome on Android) ── */
  function startNative(session) {
    var D = window.BarcodeDetector;
    return D.getSupportedFormats().then(function (list) {
      var formats = WANT.filter(function (f) { return list.indexOf(f) >= 0; });
      if (!formats.length) throw new Error('no formats');
      var detector = new D({ formats: formats }), last = 0, inFlight = false, fails = 0, raf = 0, off = false;
      function tick(ts) {
        if (session.stopped || off) return;
        if (!inFlight && ts - last > 110 && ui.video.readyState >= 2) {
          last = ts; inFlight = true;
          detector.detect(ui.video).then(function (codes) { fails = 0; if (codes && codes.length) onRead(session, codes[0].rawValue); })
            .catch(function () {                                                 // some phones say they have a reader but cannot use it: stop asking
              if (++fails >= 8) { off = true; cancelAnimationFrame(raf); }
            }).then(function () { inFlight = false; });
        }
        raf = requestAnimationFrame(tick);
      }
      raf = requestAnimationFrame(tick);
      session.stops.push(function () { off = true; cancelAnimationFrame(raf); });
      engineReady(session, 'built-in reader (BarcodeDetector)');
    });
  }

  /* The part of the camera picture the person is aiming at: the on-screen frame, widened a little. The picture fills the
     screen with object-fit: cover, so the screen box has to be mapped back to picture pixels. */
  function aimedRegion(v) {
    var vw = v.videoWidth, vh = v.videoHeight, whole = { x: 0, y: 0, w: vw, h: vh };
    try {
      var vr = v.getBoundingClientRect(), fr = ui.frame.getBoundingClientRect();
      if (!vr.width || !vr.height || !fr.width) return whole;
      var s = Math.max(vr.width / vw, vr.height / vh);
      var ox = (vr.width - vw * s) / 2, oy = (vr.height - vh * s) / 2;
      var cx = ((fr.left + fr.width / 2) - vr.left - ox) / s, cy = ((fr.top + fr.height / 2) - vr.top - oy) / s;
      var w = Math.min(vw, Math.max(vw * 0.6, (fr.width / s) * 1.4)), hgt = Math.min(vh, Math.max(vh * 0.3, (fr.height / s) * 2.6));
      var x = Math.max(0, Math.min(vw - w, cx - w / 2)), y = Math.max(0, Math.min(vh - hgt, cy - hgt / 2));
      return { x: x, y: y, w: w, h: hgt };
    } catch (e) { return whole; }
  }

  /* ── reader 2: ZXing-C++ in WebAssembly (everywhere, including iPhone Safari) ── */
  function startWasm(session, Z) {
    var canvas = document.createElement('canvas'), g = canvas.getContext('2d', { willReadFrequently: true });
    var stopped = false, n = 0, t0 = Date.now(), timer = 0, errs = 0;
    function again() { if (!stopped && !session.stopped) timer = setTimeout(step, 90); }
    function step() {
      var v = ui.video;
      if (stopped || session.stopped) return;
      if (v.readyState < 2 || !v.videoWidth) { timer = setTimeout(step, 150); return; }
      var whole = (n++ % 4 === 3) && (Date.now() - t0 > FULL_FRAME_AFTER_MS);
      var r = whole ? { x: 0, y: 0, w: v.videoWidth, h: v.videoHeight } : aimedRegion(v);
      var k = Math.min(1, 1600 / Math.max(r.w, r.h));
      canvas.width = Math.max(16, Math.round(r.w * k)); canvas.height = Math.max(16, Math.round(r.h * k));
      try { g.drawImage(v, r.x, r.y, r.w, r.h, 0, 0, canvas.width, canvas.height); } catch (e) { again(); return; }
      var data = g.getImageData(0, 0, canvas.width, canvas.height);
      Z.readBarcodes(data, wasmOptions(session, false)).then(function (res) {
        errs = 0;
        var code = choose(res, session, { x: canvas.width / 2, y: canvas.height / 2 });
        if (code) onRead(session, code);
      }).catch(function () {
        if (++errs >= 8 && !stopped) { stopped = true; session.wasmFailed = true; startFallback(session); }
      }).then(again);
    }
    step();
    session.stops.push(function () { stopped = true; clearTimeout(timer); });
    engineReady(session, 'ZXing-C++ (WebAssembly, served from this site)');
  }

  /* ── reader 3: the older JavaScript ZXing, only if WebAssembly cannot be used ── */
  function startZX(session) {
    return loadZX().then(function (ZX) {
      var hints = new Map();
      var fm = [ZX.BarcodeFormat.EAN_13, ZX.BarcodeFormat.EAN_8, ZX.BarcodeFormat.UPC_A, ZX.BarcodeFormat.UPC_E, ZX.BarcodeFormat.CODE_128];
      hints.set(ZX.DecodeHintType.POSSIBLE_FORMATS, fm);
      hints.set(ZX.DecodeHintType.TRY_HARDER, true);
      var reader = new ZX.BrowserMultiFormatReader(hints, 120);
      reader.decodeFromStream(session.stream, ui.video, function (result) { if (result) onRead(session, result.getText()); });
      session.stops.push(function () { try { reader.reset(); } catch (e) { /* already stopped */ } });
      engineReady(session, 'ZXing JavaScript library (fallback)');
    });
  }

  function startFallback(session) {
    if (session.stopped || session.fallbackStarted) return Promise.resolve();
    session.fallbackStarted = true;
    return startZX(session).catch(function () {
      if (!session.stopped && !session.engines.length) say('The scanner could not be loaded. You can still type the number below.', 'bad');
    });
  }

  function startEngines(session) {
    var nativeP = window.BarcodeDetector ? startNative(session).then(function () { return true; }, function () { return false; }) : Promise.resolve(false);
    var wasmStarted = loadWasm().then(function (Z) { if (!session.stopped) startWasm(session, Z); return true; }, function () { return false; });
    return Promise.all([nativeP, wasmStarted]).then(function (ok) {
      if (session.stopped) return;
      if (!ok[0] && !ok[1]) return startFallback(session);
    });
  }

  /* ── a photo taken with the phone's own camera app: full resolution, its own autofocus ── */
  function pictureOf(file) {
    if (window.createImageBitmap) {
      return createImageBitmap(file).catch(function () { return viaImage(file); });
    }
    return viaImage(file);
  }
  function viaImage(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('image')); };
      img.src = url;
    });
  }
  function readPhoto(file, session) {
    return pictureOf(file).then(function (pic) {
      var w = pic.width || pic.naturalWidth, hh = pic.height || pic.naturalHeight, k = Math.min(1, 2800 / Math.max(w, hh));
      var c = document.createElement('canvas'); c.width = Math.max(16, Math.round(w * k)); c.height = Math.max(16, Math.round(hh * k));
      var g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(pic, 0, 0, c.width, c.height);
      if (pic.close) pic.close();
      return loadWasm().then(function (Z) {
        return Z.readBarcodes(g.getImageData(0, 0, c.width, c.height), wasmOptions(session, true))
          .then(function (res) { return choose(res, session, { x: c.width / 2, y: c.height / 2 }); });
      }).catch(function () {                                                           // no WebAssembly: the JavaScript reader on the same picture
        return loadZX().then(function (ZX) {
          var hints = new Map();
          hints.set(ZX.DecodeHintType.POSSIBLE_FORMATS, [ZX.BarcodeFormat.EAN_13, ZX.BarcodeFormat.EAN_8, ZX.BarcodeFormat.UPC_A, ZX.BarcodeFormat.UPC_E, ZX.BarcodeFormat.CODE_128]);
          hints.set(ZX.DecodeHintType.TRY_HARDER, true);
          var reader = new ZX.MultiFormatReader(); reader.setHints(hints);
          try { return reader.decode(new ZX.BinaryBitmap(new ZX.HybridBinarizer(new ZX.HTMLCanvasElementLuminanceSource(c)))).getText(); } catch (e) { return ''; }
        });
      });
    });
  }

  function stopLive(session) {
    if (!session) return;
    session.stops.splice(0).forEach(function (fn) { try { fn(); } catch (e) { /* already stopped */ } });
    session.fallbackStarted = false;
    if (session.stream) { session.stream.getTracks().forEach(function (t) { t.stop(); }); session.stream = null; }
    try { ui.video.srcObject = null; } catch (e) { /* ignore */ }
  }

  function startLive(session) {
    ui.torch.hidden = true; ui.zoom.hidden = true;
    var p = problem();
    if (p) { say(p + ' You can still type the number below.', 'bad'); return; }
    say('Starting the camera…');
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false })
      .then(function (stream) {
        if (session.stopped || session.photoMode) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
        session.stream = stream;
        ui.video.srcObject = stream;
        var track = stream.getVideoTracks()[0];
        try {
          var caps = track && track.getCapabilities ? track.getCapabilities() : {};
          if (caps && caps.focusMode && caps.focusMode.indexOf('continuous') >= 0) {
            track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }).catch(function () { /* not allowed */ });
          }
          if (caps && caps.torch) {
            ui.torch.hidden = false;
            var on = false;
            ui.torch.onclick = function () { on = !on; track.applyConstraints({ advanced: [{ torch: on }] }).catch(function () { /* not allowed */ }); };
          }
          if (caps && caps.zoom && caps.zoom.max >= 2) {                           // a small IMEI label: zoom in instead of leaning closer than the lens can focus
            ui.zoom.hidden = false; ui.zoom.textContent = 'Zoom 2×';
            var zoomed = false, lo = caps.zoom.min || 1;
            ui.zoom.onclick = function () {
              zoomed = !zoomed;
              track.applyConstraints({ advanced: [{ zoom: zoomed ? Math.min(2, caps.zoom.max) : lo }] }).catch(function () { /* not allowed */ });
              ui.zoom.textContent = zoomed ? 'Zoom 1×' : 'Zoom 2×';
            };
          }
        } catch (e) { /* no torch / zoom / focus control */ }
        var started = ui.video.play ? ui.video.play() : null;
        return Promise.resolve(started).catch(function () { /* autoplay refused: the stream still decodes */ }).then(function () {
          if (session.stopped || session.photoMode) return;
          return startEngines(session);
        });
      })
      .catch(function (err) { if (!session.stopped) say(cameraMessage(err), 'bad'); });
  }

  function open(opts) {
    opts = opts || {};
    build();
    if (run) close();
    var session = { opts: opts, stopped: false, busy: false, lastCode: '', lastAt: 0, stream: null, stops: [], unknown: '', engines: [], photoMode: false, fallbackStarted: false };
    run = session;
    ui.title.textContent = opts.title || 'Scan a barcode';
    ui.link.hidden = true; ui.input.value = ''; ui.photo.textContent = 'Take a photo instead';
    ui.root.hidden = false; document.body.classList.add('tpscan-open');
    startLive(session);
  }

  function close() {
    var session = run;
    run = null;
    if (!ui) return;
    if (session) {
      session.stopped = true;
      lastEngineName = session.engines.join(' + ');
      stopLive(session);
    }
    ui.root.hidden = true;
    document.body.classList.remove('tpscan-open');
  }

  /* ── "this barcode isn't on any product yet — which product is it?" ── */
  var dlg = null;
  function linkDialog(opts) {
    var code = opts.code;
    if (!dlg) {
      var root = h('div', 'tplink'); root.hidden = true; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true');
      var box = h('div', 'tplink-box'), head = h('div', 'tplink-head'), body = h('p', 'tplink-text'), find = h('input'), list = h('div', 'tplink-list'),
        err = h('div', 'tplink-err'), cancel = h('button', 'tpscan-btn', 'Cancel');
      find.type = 'search'; find.placeholder = 'Search by name, model or SKU'; find.autocomplete = 'off';
      cancel.type = 'button';
      box.appendChild(head); box.appendChild(body); box.appendChild(find); box.appendChild(list); box.appendChild(err); box.appendChild(cancel);
      root.appendChild(box); document.body.appendChild(root);
      dlg = { root: root, head: head, body: body, find: find, list: list, err: err, opts: null, timer: 0 };
      cancel.addEventListener('click', function () { root.hidden = true; });
      root.addEventListener('click', function (e) { if (e.target === root) root.hidden = true; });
      find.addEventListener('input', function () {
        clearTimeout(dlg.timer);
        dlg.timer = setTimeout(search, 250);
      });
    }
    dlg.opts = opts;
    dlg.head.textContent = 'Link barcode ' + code;
    if (!CAN_LINK) {
      dlg.body.textContent = 'No product has this barcode yet. Only the owner or a manager can link it to a product.';
      dlg.find.hidden = true; dlg.list.textContent = ''; dlg.err.textContent = '';
    } else {
      dlg.body.textContent = 'No product has this barcode yet. Which product is it? The next scan of it will find that product.';
      dlg.find.hidden = false; dlg.find.value = ''; dlg.list.textContent = ''; dlg.err.textContent = '';
    }
    dlg.root.hidden = false;
    if (CAN_LINK) dlg.find.focus();
  }

  function search() {
    var q = dlg.find.value.trim();
    if (q.length < 2) { dlg.list.textContent = ''; return; }
    fetch('/products/search.json?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        dlg.list.textContent = '';
        if (!j.ok || !j.products.length) { dlg.list.appendChild(h('div', 'tplink-none', 'Nothing matches “' + q + '”.')); return; }
        j.products.forEach(function (p) {
          var b = h('button', 'tplink-item'); b.type = 'button';
          b.appendChild(h('span', 'tplink-name', p.name));
          b.appendChild(h('small', '', p.sku + (p.barcode ? ' · already has a barcode' : '')));
          b.addEventListener('click', function () { link(p); });
          dlg.list.appendChild(b);
        });
      })
      .catch(function () { dlg.err.textContent = 'Could not search. Check the connection.'; });
  }

  function link(p) {
    if (p.barcode && !window.confirm(p.name + ' already has the barcode ' + p.barcode + '. Replace it with ' + dlg.opts.code + '?')) return;
    dlg.err.textContent = '';
    fetch('/products/' + encodeURIComponent(p.id) + '/barcode', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': CSRF, 'X-Scan-Ajax': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'barcode=' + encodeURIComponent(dlg.opts.code)
    }).then(function (r) { return r.json().catch(function () { return { ok: false, error: 'Unexpected answer (' + r.status + ').' }; }); })
      .then(function (j) {
        if (!j.ok) { dlg.err.textContent = j.error || 'Could not save.'; return; }
        dlg.root.hidden = true;
        if (dlg.opts.onLinked) dlg.opts.onLinked(j.product, j.warning || '');
      })
      .catch(function () { dlg.err.textContent = 'Could not save. Check the connection.'; });
  }

  /* Any button marked  data-scan-fill="inputId"  opens the camera and drops the read number into that box.
     Add  data-scan-submit="1"  to also submit the box's form (search boxes). */
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-scan-fill]') : null;
    if (!b) return;
    e.preventDefault();
    var box = document.getElementById(b.getAttribute('data-scan-fill'));
    if (!box) return;
    open({ title: b.getAttribute('data-scan-title') || 'Scan a barcode', continuous: false, prefer: b.getAttribute('data-scan-prefer') || '', onCode: function (code) {
      box.value = code;
      box.dispatchEvent(new Event('input', { bubbles: true }));
      if (b.getAttribute('data-scan-submit') === '1' && box.form) { if (box.form.requestSubmit) box.form.requestSubmit(); else box.form.submit(); }
      else box.focus();
    } });
  });

  window.TPScan = { open: open, close: close, linkDialog: linkDialog, canLink: CAN_LINK, csrf: CSRF, problem: problem,
                    engine: function () { return run ? run.engines.join(' + ') : lastEngineName; } };   // v52: which reader is doing the work (shown on the Scanner test page)
})();
