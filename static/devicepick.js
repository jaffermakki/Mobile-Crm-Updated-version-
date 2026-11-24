/* v51: Brand -> Model pickers for the repair ticket and trade-in forms.
   Markup (templates/partials/device_picker.html):
     <div class="device-picker" data-device-picker data-catalog='[{"brand":"Apple","groups":[{"group":"…","models":["…"]}]},…]'>
       <select class="dp-brand">…</select> <select class="dp-model">…</select> <input class="dp-text" name="device">
   The text box is what is saved. The pickers only fill it in, so without JavaScript, or for a phone that is not in the
   list, the box simply works as it always did. */
(function () {
  'use strict';
  var OTHER_BRAND = 'Other', OTHER_MODEL = '__other';

  function opt(value, label) {
    var o = document.createElement('option');
    o.value = value; o.textContent = label;
    return o;
  }

  function init(root) {
    var list, cat = {};
    try { list = JSON.parse(root.getAttribute('data-catalog') || '[]'); } catch (e) { return; }
    if (!Array.isArray(list)) return;
    list.forEach(function (entry) { cat[entry.brand] = entry.groups || []; });
    var brand = root.querySelector('.dp-brand'), model = root.querySelector('.dp-model'), text = root.querySelector('.dp-text');
    if (!brand || !model || !text) return;

    list.forEach(function (entry) {
      brand.appendChild(opt(entry.brand, entry.brand === OTHER_BRAND ? 'Other (type it)' : entry.brand));
    });

    function fillModels(b) {
      while (model.firstChild) model.removeChild(model.firstChild);
      if (!b || b === OTHER_BRAND || !cat[b]) { model.hidden = true; return; }
      model.hidden = false;
      model.appendChild(opt('', 'Choose the model…'));
      cat[b].forEach(function (g) {
        var holder = model;
        if (cat[b].length > 1) {
          holder = document.createElement('optgroup');
          holder.label = g.group;
          model.appendChild(holder);
        }
        g.models.forEach(function (m) { holder.appendChild(opt(m, m)); });
      });
      model.appendChild(opt(OTHER_MODEL, 'Not in the list — type it'));
    }

    brand.addEventListener('change', function () {
      fillModels(brand.value);
      if (brand.value === OTHER_BRAND) { text.value = ''; text.focus(); }
    });
    model.addEventListener('change', function () {
      if (model.value === OTHER_MODEL) { text.value = ''; text.focus(); return; }
      if (model.value) text.value = model.value;
    });

    // The box already holds a phone (the page was re-shown after an error): select it again.
    var current = (text.value || '').trim().toLowerCase(), matchBrand = '', matchModel = '';
    if (current) {
      Object.keys(cat).some(function (b) {
        return cat[b].some(function (g) {
          return g.models.some(function (m) {
            if (m.toLowerCase() !== current) return false;
            matchBrand = b; matchModel = m;
            return true;
          });
        });
      });
    }
    if (matchBrand) { brand.value = matchBrand; fillModels(matchBrand); model.value = matchModel; }
    else fillModels('');
  }

  function boot() {
    var roots = document.querySelectorAll('[data-device-picker]');
    for (var i = 0; i < roots.length; i++) init(roots[i]);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
