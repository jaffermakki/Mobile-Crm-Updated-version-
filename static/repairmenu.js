/* v54: repair price menu on the New Repair Ticket form.
   When a phone is picked or typed, ask the server (GET /repair-menu?device=...) which repairs the owner has priced for it and
   show them as buttons. Tapping one fills the Issue and Estimated Cost boxes. Both boxes stay editable, and without JavaScript
   the form works exactly as before. Everything from the server is written with textContent, never as HTML. */
(function () {
  'use strict';
  var form = document.querySelector('#new-ticket-panel form[action="/repairs/add"]');
  var box = document.getElementById('repair-menu');
  if (!form || !box) return;
  var text = form.querySelector('.dp-text');
  var issue = form.querySelector('select[name=issue]');
  var cost = form.querySelector('input[name=estimated_cost]');
  var chips = box.querySelector('.rm-chips');
  var info = box.querySelector('.rm-info');
  var head = box.querySelector('.rm-head');
  if (!text || !issue || !cost || !chips || !info) return;

  var timer = null, seq = 0, lastDevice = null;

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function money(n) { return '$' + Number(n).toFixed(2); }
  function line(cls, msg) {
    var d = document.createElement('div');
    d.className = cls; d.textContent = msg;
    info.appendChild(d);
  }

  function hide() { box.hidden = true; clear(chips); clear(info); }

  function pick(item, btn) {
    var found = false, i;
    for (i = 0; i < issue.options.length; i++) {
      if (issue.options[i].value === item.repair) { found = true; break; }
    }
    if (!found) {
      var o = document.createElement('option');
      o.value = item.repair; o.textContent = item.repair;
      issue.appendChild(o);
    }
    issue.value = item.repair;
    cost.value = Number(item.price).toFixed(2);
    cost.dispatchEvent(new Event('input', { bubbles: true }));
    var all = chips.querySelectorAll('button');
    for (i = 0; i < all.length; i++) all[i].setAttribute('aria-pressed', all[i] === btn ? 'true' : 'false');
    clear(info);
    line('rm-note', 'Filled in from your price list. You can still change the estimate.');
    if (item.part) {
      var out = item.part.stock <= 0;
      line('rm-part' + (out ? ' rm-out' : ''), 'Usual part: ' + item.part.sku + ' · ' + item.part.name + ' (' +
        (out ? 'none in stock, order it first' : item.part.stock + ' in stock') + ')');
    }
    if (item.minutes) line('rm-note', 'Usually about ' + item.minutes + ' minutes.');
  }

  function render(items) {
    clear(chips); clear(info);
    if (!items || !items.length) { box.hidden = true; return; }
    items.forEach(function (item) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'rm-chip';
      b.setAttribute('aria-pressed', 'false');
      b.textContent = item.repair + ' · ' + money(item.price);
      b.addEventListener('click', function () { pick(item, b); });
      chips.appendChild(b);
    });
    box.hidden = false;
  }

  function load() {
    var d = (text.value || '').trim();
    if (d === lastDevice) return;
    lastDevice = d;
    if (!d) { hide(); return; }
    var mine = ++seq;
    fetch('/repair-menu?device=' + encodeURIComponent(d), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (mine === seq && j && j.ok) render(j.items); })
      .catch(function () { if (mine === seq) hide(); });
  }

  function soon(ms) { clearTimeout(timer); timer = setTimeout(load, ms); }
  form.addEventListener('input', function (e) { if (e.target === text) soon(300); });
  form.addEventListener('change', function (e) {
    if (e.target === text || (e.target.classList && e.target.classList.contains('dp-model'))) soon(40);
  });
  text.addEventListener('blur', function () { soon(0); });
  if ((text.value || '').trim()) soon(0);
})();
