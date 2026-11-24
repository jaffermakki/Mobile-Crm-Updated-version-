/* v54: Customer pages screen. Keeps the colour picker and the #RRGGBB box in step, and asks before removing the logo. */
(function () {
  'use strict';
  var pick = document.getElementById('br-color-pick');
  var hex = document.getElementById('br-color-hex');
  if (pick && hex) {
    pick.addEventListener('input', function () { hex.value = pick.value; });
    hex.addEventListener('input', function () {
      var v = hex.value.trim();
      if (/^#[0-9a-fA-F]{6}$/.test(v)) pick.value = v.toLowerCase();
    });
  }
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f && f.getAttribute && f.getAttribute('data-confirm') && !window.confirm(f.getAttribute('data-confirm'))) e.preventDefault();
  });
})();
