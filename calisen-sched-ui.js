/* Presentation helper for the scheduling grid (no data or business logic).
   In Week view the engineer name column and the day grid are separate stacks, so engineer labels drift out of line with
   their rows when a row grows to fit its cards. This keeps each label exactly as tall as its row. */
(function () {
  var raf = 0;
  function sync() {
    raf = 0;
    var list = document.getElementById('engList'), wrap = document.getElementById('rowsWrap');
    if (!list || !wrap) return;
    var labels = list.querySelectorAll('.eng-row');
    var rows = wrap.querySelectorAll('.eng-tl-row');
    if (!labels.length || labels.length !== rows.length) return;
    for (var i = 0; i < labels.length; i++) {
      var h = rows[i].getBoundingClientRect().height;
      if (h > 0 && Math.abs(labels[i].getBoundingClientRect().height - h) > 0.5) {
        labels[i].style.height = h + 'px';
        labels[i].style.minHeight = h + 'px';
      }
    }
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(sync); }
  function start() {
    var wrap = document.getElementById('rowsWrap'), list = document.getElementById('engList');
    if (!wrap || !list) return;
    var mo = new MutationObserver(schedule);
    mo.observe(wrap, { childList: true, subtree: true });
    mo.observe(list, { childList: true });
    window.addEventListener('resize', schedule);
    schedule();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
