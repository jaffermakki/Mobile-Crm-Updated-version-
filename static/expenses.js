// Expense form: a bank deposit / owner draw has no tax and must come out of the till.
(function () {
  var cat = document.getElementById('ex-cat'), from = document.getElementById('ex-from');
  var tax = document.getElementById('ex-tax'), taxGroup = document.getElementById('ex-tax-group');
  if (!cat || !from || !tax) return;
  function sync() {
    var opt = cat.options[cat.selectedIndex];
    var moves = !!(opt && opt.getAttribute('data-moves'));
    if (moves) { from.value = 'drawer'; tax.value = ''; }
    from.disabled = false;
    if (taxGroup) taxGroup.style.display = moves ? 'none' : '';
  }
  cat.addEventListener('change', sync);
  sync();
})();
