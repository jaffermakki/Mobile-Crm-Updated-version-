/* v50: the counting screen. Every change goes to the server straight away, so a dropped connection or a closed tab
   never loses what was counted. The screen never shows what the system expects, so the count stays honest. */
(function () {
  'use strict';
  var root = document.getElementById('sc-root');
  if (!root) return;
  var COUNT = root.getAttribute('data-count');
  var CSRF = root.getAttribute('data-csrf');
  var list = document.getElementById('sc-list');
  var statusBox = document.getElementById('sc-status');
  var empty = document.getElementById('sc-empty');
  var codeBox = document.getElementById('sc-code');
  var findBox = document.getElementById('sc-find');
  var found = document.getElementById('sc-found');
  var queue = Promise.resolve();           // requests go one at a time, in the order they were made

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }

  function post(path, params) {
    var body = new URLSearchParams(params).toString();
    return fetch('/stock-count/' + encodeURIComponent(COUNT) + path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': CSRF, 'X-Scan-Ajax': '1', 'Content-Type': 'application/x-www-form-urlencoded' }, body: body
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'Unexpected answer (' + r.status + '). Reload the page.' }; });
    }).catch(function () { return { ok: false, error: 'No connection. Nothing was counted for that scan. Try again.' }; });
  }
  function enqueue(fn) { var p = queue.then(fn); queue = p.catch(function () { /* keep the chain alive */ }); return p; }

  function say(text, kind) { statusBox.textContent = text; statusBox.className = 'sc-status' + (kind ? ' ' + kind : ''); }

  function totals() {
    var rows = list.querySelectorAll('.sc-line'), units = 0;
    rows.forEach(function (r) { units += parseInt(r.querySelector('.sc-set').value, 10) || 0; });
    document.getElementById('sc-nprod').textContent = rows.length;
    document.getElementById('sc-nprod-s').textContent = rows.length === 1 ? '' : 's';
    document.getElementById('sc-nunits').textContent = units;
    document.getElementById('sc-nunits-s').textContent = units === 1 ? '' : 's';
    empty.hidden = rows.length > 0;
  }

  function rowFor(pid) {
    var rows = list.querySelectorAll('.sc-line');
    for (var i = 0; i < rows.length; i++) if (rows[i].getAttribute('data-pid') === pid) return rows[i];
    return null;
  }

  function makeRow(p) {
    var row = el('div', 'sc-line'); row.setAttribute('data-pid', p.id);
    var info = el('div', 'sc-info'); info.appendChild(el('b', '', p.name)); info.appendChild(el('small', '', p.sku));
    var q = el('div', 'sc-qty');
    var dec = el('button', '', '−'); dec.type = 'button'; dec.setAttribute('data-act', 'dec'); dec.setAttribute('aria-label', 'One less');
    var inp = el('input', 'sc-set'); inp.type = 'number'; inp.min = '0'; inp.max = '100000'; inp.setAttribute('inputmode', 'numeric');
    inp.setAttribute('aria-label', 'Counted quantity for ' + p.name);
    var inc = el('button', '', '+'); inc.type = 'button'; inc.setAttribute('data-act', 'inc'); inc.setAttribute('aria-label', 'One more');
    q.appendChild(dec); q.appendChild(inp); q.appendChild(inc);
    var rm = el('button', 'sc-rm', 'Remove'); rm.type = 'button'; rm.setAttribute('data-act', 'rm');
    row.appendChild(info); row.appendChild(q); row.appendChild(rm);
    return row;
  }

  function show(p, counted, top) {
    var row = rowFor(p.id);
    if (!row) { row = makeRow(p); list.insertBefore(row, list.firstChild); }
    else if (top && row !== list.firstChild) list.insertBefore(row, list.firstChild);
    var inp = row.querySelector('.sc-set');
    inp.value = counted; inp.setAttribute('data-was', counted);
    row.classList.add('flash');
    setTimeout(function () { row.classList.remove('flash'); }, 900);
    totals();
  }

  /* ── one code: from the camera, a USB scanner or the keyboard ── */
  function countCode(code) {
    return enqueue(function () { return post('/scan', { code: code, qty: 1 }); }).then(function (r) {
      if (r.ok) {
        show(r.product, r.counted, true);
        say(r.message, r.outside ? 'note' : 'good');
        return { ok: true, message: r.message };
      }
      say(r.error || 'Could not count that.', 'bad');
      if (r.found === false) {
        var unknown = r.code || code;
        var b = el('button', 'btn-secondary btn-sm sc-act', 'Link this barcode…'); b.type = 'button';
        b.addEventListener('click', function () { linkUnknown(unknown); });
        statusBox.appendChild(b);
        return { ok: false, message: r.error, unknownCode: unknown };
      }
      return { ok: false, message: r.error };
    });
  }

  function linkUnknown(code) {
    if (!window.TPScan) return;
    TPScan.linkDialog({ code: code, onLinked: function (p) {
      enqueue(function () { return post('/line', { product_id: p.id, mode: 'add', qty: 1 }); }).then(function (r) {
        if (r.ok) { show(r.product, r.counted, true); say('Barcode saved on ' + p.name + ', and counted: ' + r.counted + '.', 'good'); }
        else say(r.error || 'The barcode was saved but the count did not update.', 'bad');
      });
    } });
  }

  document.getElementById('sc-code-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var code = codeBox.value.replace(/\s+/g, '');
    codeBox.value = '';
    if (code) countCode(code).then(function () { codeBox.focus(); });
  });

  document.getElementById('sc-camera').addEventListener('click', function () {
    if (!window.TPScan) { say('The scanner is still loading. Try again in a moment.', 'bad'); return; }
    TPScan.open({
      title: 'Counting: scan each unit', continuous: true,
      onCode: function (code) { return countCode(code); },
      onUnknown: function (code) { linkUnknown(code); }
    });
  });

  /* ── +, −, typed quantity, remove ── */
  list.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var row = btn.closest('.sc-line'), pid = row.getAttribute('data-pid'), act = btn.getAttribute('data-act');
    if (act === 'rm') {
      var have = parseInt(row.querySelector('.sc-set').value, 10) || 0;
      if (have > 0 && !window.confirm('Remove this product and its ' + have + ' counted unit' + (have === 1 ? '' : 's') + ' from the count?')) return;
      enqueue(function () { return post('/remove', { product_id: pid }); }).then(function (r) {
        if (r.ok) { row.remove(); totals(); say('Removed from the count.', ''); }
        else say(r.error || 'Could not remove that.', 'bad');
      });
      return;
    }
    enqueue(function () { return post('/line', { product_id: pid, mode: 'add', qty: act === 'inc' ? 1 : -1 }); }).then(function (r) {
      if (r.ok) show(r.product, r.counted, false); else say(r.error || 'Could not change that.', 'bad');
    });
  });

  list.addEventListener('change', function (e) {
    var inp = e.target;
    if (!inp.classList || !inp.classList.contains('sc-set')) return;
    var row = inp.closest('.sc-line'), pid = row.getAttribute('data-pid'), was = inp.getAttribute('data-was');
    var n = parseInt(inp.value, 10);
    if (isNaN(n) || n < 0 || n > 100000) { inp.value = was; say('Enter a whole number from 0 to 100000.', 'bad'); return; }
    enqueue(function () { return post('/line', { product_id: pid, mode: 'set', qty: n }); }).then(function (r) {
      if (r.ok) { show(r.product, r.counted, false); say(r.message, 'good'); }
      else { inp.value = was; say(r.error || 'Could not change that.', 'bad'); }
    });
  });

  /* ── no barcode: find by name ── */
  var timer = 0;
  findBox.addEventListener('input', function () {
    clearTimeout(timer);
    var q = findBox.value.trim();
    if (q.length < 2) { found.textContent = ''; return; }
    timer = setTimeout(function () {
      fetch('/products/search.json?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          found.textContent = '';
          if (!j.ok) { found.appendChild(el('div', 'sc-empty', j.error || 'Could not search.')); return; }
          if (!j.products.length) { found.appendChild(el('div', 'sc-empty', 'Nothing matches “' + q + '”.')); return; }
          j.products.slice(0, 8).forEach(function (p) {
            var b = el('button'); b.type = 'button';
            b.appendChild(el('span', '', p.name)); b.appendChild(el('small', '', p.sku));
            b.addEventListener('click', function () {
              enqueue(function () { return post('/line', { product_id: p.id, mode: 'add', qty: 1 }); }).then(function (r) {
                if (r.ok) { show(r.product, r.counted, true); say(r.message, 'good'); found.textContent = ''; findBox.value = ''; }
                else say(r.error || 'Could not count that.', 'bad');
              });
            });
            found.appendChild(b);
          });
        })
        .catch(function () { found.textContent = ''; found.appendChild(el('div', 'sc-empty', 'Could not search. Check the connection.')); });
    }, 250);
  });

  totals();
})();
