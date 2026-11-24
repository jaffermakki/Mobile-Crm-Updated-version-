/* v54: small helpers for the Repair prices page.
   1. "Same price on every phone" turns the phone pickers off, so the required phone box does not block the form.
   2. The Remove buttons ask first. Without JavaScript the buttons still work, they just do not ask. */
(function () {
  'use strict';
  var any = document.getElementById('rp-any');
  var form = document.getElementById('rp-add');
  if (any && form) {
    var picker = form.querySelector('[data-device-picker]');
    var text = form.querySelector('.dp-text');
    var sync = function () {
      if (!picker || !text) return;
      picker.classList.toggle('rp-off', any.checked);
      text.required = !any.checked;
      text.disabled = any.checked;
      var sels = picker.querySelectorAll('select');
      for (var i = 0; i < sels.length; i++) sels[i].disabled = any.checked;
    };
    any.addEventListener('change', sync);
    sync();
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-confirm-text]') : null;
    if (b && !window.confirm(b.getAttribute('data-confirm-text'))) e.preventDefault();
  });
})();
