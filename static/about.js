/* v54: "Copy support info" on the About page. Uses the clipboard when the browser allows it, otherwise selects the text so it can be copied by hand. */
(function () {
  'use strict';
  var btn = document.getElementById('copy-support');
  var box = document.getElementById('support-text');
  var status = document.getElementById('copy-status');
  if (!btn || !box) return;
  function say(msg) { if (status) status.textContent = msg; }
  btn.addEventListener('click', function () {
    var text = box.value;
    function fallback() {
      box.focus(); box.select();
      try { say(document.execCommand('copy') ? 'Copied. Paste it into your message.' : 'Press and hold the text, then choose Copy.'); }
      catch (e) { say('Press and hold the text, then choose Copy.'); }
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { say('Copied. Paste it into your message.'); }, fallback);
    } else { fallback(); }
  });
})();
