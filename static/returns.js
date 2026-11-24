/* v52: the "Return items" screen — one button to tick everything that's left. */
(function () {
  'use strict';
  var btn = document.getElementById('ret-all');
  if (!btn) return;
  btn.addEventListener('click', function () {
    document.querySelectorAll('#return-form input[type=number][data-left]').forEach(function (i) {
      i.value = i.getAttribute('data-left');
    });
  });
})();
