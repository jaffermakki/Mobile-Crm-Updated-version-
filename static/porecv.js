/* v51: receive a delivery by scanning it against its purchase order.

   The page already has one "quantity actually received" box per line (templates/po_detail.html). Scanning only fills those
   boxes in: nothing reaches the stock until "Confirm receipt" is pressed, exactly as before. Starting a scan session sets
   every box to 0 and each scanned item adds 1, so a line that never turns up is left at 0. A line can't go past what was
   ordered (the server never adds more than that either); the screen says so instead of silently ignoring the extra. */
(function () {
  'use strict';
  var root = document.getElementById('po-recv');
  if (!root || !window.TPScan) return;
  var PO = root.getAttribute('data-po');
  var startBtn = document.getElementById('po-scan-start');
  var resetBtn = document.getElementById('po-scan-reset');
  var statusBox = document.getElementById('po-scan-status');
  var form = document.getElementById('po-recv-form');
  var scanning = false;

  function inputs() { return root.querySelectorAll('input[data-line]'); }
  function boxFor(lineId) {
    var all = inputs();
    for (var i = 0; i < all.length; i++) if (all[i].getAttribute('data-line') === lineId) return all[i];
    return null;
  }
  function say(text, kind) { if (statusBox) { statusBox.textContent = text; statusBox.className = 'po-scan-status' + (kind ? ' ' + kind : ''); statusBox.hidden = !text; } }

  function paint(box) {
    var row = box.closest('tr'), ordered = parseInt(box.getAttribute('data-ordered'), 10) || 0, got = parseInt(box.value, 10) || 0;
    var cell = row ? row.querySelector('.po-progress') : null;
    if (cell) {
      cell.textContent = got + ' of ' + ordered;
      cell.className = 'po-progress ' + (got === ordered ? 'full' : got === 0 ? 'none' : 'part');
    }
  }
  function paintAll() { inputs().forEach(paint); }

  function summary() {
    var short = 0, units = 0, all = inputs();
    all.forEach(function (b) {
      var ordered = parseInt(b.getAttribute('data-ordered'), 10) || 0, got = parseInt(b.value, 10) || 0;
      units += got; if (got < ordered) short++;
    });
    return { short: short, units: units, lines: all.length };
  }

  function begin() {
    if (!scanning) {
      inputs().forEach(function (b) { b.value = 0; });
      scanning = true;
      root.classList.add('po-scanning');
      if (resetBtn) resetBtn.hidden = false;
      paintAll();
    }
    window.TPScan.open({
      title: 'Scan each item as it comes out of the box', continuous: true,
      onCode: function (code) {
        return fetch('/purchase-orders/' + encodeURIComponent(PO) + '/scan.json?code=' + encodeURIComponent(code), { credentials: 'same-origin' })
          .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'Unexpected answer (' + r.status + '). Reload the page.' }; }); })
          .then(function (j) {
            if (!j.ok) return { ok: false, message: j.error || 'Not recognised.', unknownCode: j.found === false ? j.code : '' };
            var box = boxFor(j.line_id);
            if (!box) return { ok: false, message: 'That line is not on this page any more. Reload it.' };
            var ordered = parseInt(box.getAttribute('data-ordered'), 10) || 0, got = parseInt(box.value, 10) || 0;
            if (got >= ordered) return { ok: false, message: j.name + ': all ' + ordered + ' ordered are already counted. Set the extra one aside.' };
            box.value = got + 1; paint(box);
            var row = box.closest('tr'); if (row) { row.classList.add('flash'); setTimeout(function () { row.classList.remove('flash'); }, 900); }
            return { ok: true, message: j.name + ' — ' + (got + 1) + ' of ' + ordered };
          })
          .catch(function () { return { ok: false, message: 'No connection. That scan was not counted. Try again.' }; });
      },
      onUnknown: function (code) {
        // a barcode that no product has yet: link it (managers only reach this page), then count it
        window.TPScan.linkDialog({ code: code, onLinked: function (p) { say('Barcode linked to ' + p.name + '. Scan it again to count it.', 'good'); window.TPScan.close(); } });
      }
    });
  }

  if (startBtn) startBtn.addEventListener('click', begin);
  if (resetBtn) resetBtn.addEventListener('click', function () {
    inputs().forEach(function (b) { b.value = b.getAttribute('data-ordered'); });
    scanning = false; root.classList.remove('po-scanning'); resetBtn.hidden = true; say(''); paintAll();
  });
  inputs().forEach(function (b) { b.addEventListener('input', function () { paint(b); }); });
  paintAll();

  if (form) form.addEventListener('submit', function (e) {
    var s = summary();
    if (scanning && s.units === 0) {
      if (!window.confirm('Nothing was counted, so no stock will be added and this order will be closed as received. Continue?')) e.preventDefault();
      return;
    }
    if (s.short > 0 && !window.confirm(s.short + ' line' + (s.short === 1 ? ' is' : 's are') + ' short of what was ordered. Receive only what was counted?')) e.preventDefault();
  });
})();
