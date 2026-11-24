/* v49: repair photos — shrink on the phone, upload one at a time, and view in a full-screen viewer.
   The phone shrinks each picture to about 1600 px (and a 320 px preview) before it is sent, so a photo costs
   ~200 KB instead of 4-8 MB. Re-drawing the picture also drops its hidden data (location, camera details). */
(function () {
  'use strict';
  var box = document.getElementById('photos');
  if (!box) return;
  var repairId = box.getAttribute('data-repair');
  var token = box.getAttribute('data-csrf');
  var max = parseInt(box.getAttribute('data-max'), 10) || 12;
  var count = parseInt(box.getAttribute('data-count'), 10) || 0;
  var status = document.getElementById('photo-status');
  var labelSel = document.getElementById('photo-label');
  var inputs = [document.getElementById('photo-cam'), document.getElementById('photo-pick')].filter(Boolean);
  var FULL_SIDE = 1600, THUMB_SIDE = 320;

  function say(text, isError) {
    if (!status) return;
    status.textContent = text || '';
    status.className = 'photo-status' + (isError ? ' err' : '');
  }

  function loadBitmap(file) {
    if (window.createImageBitmap) {
      return createImageBitmap(file, { imageOrientation: 'from-image' }).catch(function () { return createImageBitmap(file); });
    }
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('decode')); };
      img.src = url;
    });
  }

  function dims(img) { return { w: img.naturalWidth || img.width, h: img.naturalHeight || img.height }; }

  function shrink(img, side, quality) {
    var d = dims(img), k = Math.min(1, side / Math.max(d.w, d.h));
    var cw = Math.max(1, Math.round(d.w * k)), ch = Math.max(1, Math.round(d.h * k));
    var canvas = document.createElement('canvas');
    canvas.width = cw; canvas.height = ch;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cw, ch);          // a transparent PNG becomes white, not black
    ctx.drawImage(img, 0, 0, cw, ch);
    return new Promise(function (resolve, reject) {
      canvas.toBlob(function (b) { b ? resolve(b) : reject(new Error('encode')); }, 'image/jpeg', quality);
    });
  }

  function send(full, thumb, label) {
    var form = new FormData();
    form.append('file', full, 'photo.jpg');
    form.append('thumb', thumb, 'thumb.jpg');
    form.append('label', label);
    return fetch('/repairs/' + encodeURIComponent(repairId) + '/photos', {
      method: 'POST', body: form, credentials: 'same-origin', headers: { 'X-CSRF-Token': token }
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'The server sent an unexpected answer (' + r.status + ').' }; })
        .then(function (j) { j.status = r.status; return j; });
    });
  }

  async function handle(files) {
    files = Array.prototype.slice.call(files || []);
    if (!files.length) return;
    var label = labelSel ? labelSel.value : 'other';
    inputs.forEach(function (i) { i.disabled = true; });
    var done = 0, failed = 0, lastError = '';
    for (var n = 0; n < files.length; n++) {
      if (count >= max) { lastError = 'This ticket holds ' + max + ' photos at most.'; failed += files.length - n; break; }
      say('Sending photo ' + (n + 1) + ' of ' + files.length + '…');
      try {
        var img = await loadBitmap(files[n]);
        var full = await shrink(img, FULL_SIDE, 0.8);
        var thumb = await shrink(img, THUMB_SIDE, 0.7);
        if (img.close) img.close();
        var res = await send(full, thumb, label);
        if (res.ok) { done++; count = res.count; }
        else { failed++; lastError = res.error || 'The photo was not saved.'; if (res.status === 401 || res.status === 409) break; }
      } catch (e) {
        failed++; lastError = 'Could not read "' + (files[n].name || 'that picture') + '". Take it again, or choose a JPEG.';
      }
    }
    inputs.forEach(function (i) { i.disabled = false; i.value = ''; });
    if (failed) say((done ? done + ' saved. ' : '') + lastError, true);
    if (done) {
      if (!failed) say(done + (done === 1 ? ' photo saved.' : ' photos saved.'));
      setTimeout(function () { location.hash = 'photos'; location.reload(); }, failed ? 1800 : 400);
    }
  }

  inputs.forEach(function (input) { input.addEventListener('change', function () { handle(input.files); }); });

  /* ── full-screen viewer ── */
  var lb = document.getElementById('lightbox');
  if (!lb) return;
  var lbImg = document.getElementById('lb-img'), lbCap = document.getElementById('lb-caption');
  var lbOpen = document.getElementById('lb-open'), lbDel = document.getElementById('lb-delete');
  function openViewer(tile) {
    var id = tile.getAttribute('data-id'), url = tile.getAttribute('data-full');
    lbImg.src = url; lbImg.alt = tile.getAttribute('aria-label') || 'Photo';
    lbCap.textContent = tile.getAttribute('data-caption') || '';
    lbOpen.href = url;
    if (lbDel) lbDel.action = '/repairs/' + encodeURIComponent(repairId) + '/photos/' + encodeURIComponent(id) + '/delete';
    lb.classList.add('open');
    document.getElementById('lb-close').focus();
  }
  function closeViewer() { lb.classList.remove('open'); lbImg.removeAttribute('src'); }
  Array.prototype.forEach.call(document.querySelectorAll('.photo-tile'), function (t) {
    t.addEventListener('click', function () { openViewer(t); });
  });
  document.getElementById('lb-close').addEventListener('click', closeViewer);
  lb.addEventListener('click', function (e) { if (e.target === lb) closeViewer(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeViewer(); });
})();
