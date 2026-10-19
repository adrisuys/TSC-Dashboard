// The little the shell needs beyond htmx: the drill-down drawer, and the
// jump links of the reports (a leaderboard name opening a folded section).
(function () {
  var drawer = document.getElementById('drawer');
  var scrim = document.getElementById('scrim');
  var body = document.getElementById('drawer-body');
  var openLink = document.getElementById('drawer-open');

  function openDrawer(url) {
    body.innerHTML = '<div class="skeleton"></div><div class="skeleton short"></div>' +
                     '<div class="skeleton"></div>';
    openLink.href = url;
    drawer.hidden = false;
    scrim.hidden = false;
    htmx.ajax('GET', url, { target: '#drawer-body', swap: 'innerHTML' });
  }
  function closeDrawer() { drawer.hidden = true; scrim.hidden = true; }

  // Drill-down links are written by the reports as absolute
  // http://127.0.0.1:8734/lines?… URLs (they also have to work from a saved
  // static page); inside the app they open in the drawer. A modifier-click
  // still opens a real tab.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href*="/lines?"]');
    if (!a || a.closest('#drawer') || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    var u = new URL(a.href);
    openDrawer(u.pathname + u.search);
  });
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDrawer(); });

  // A jump link can land on a folded section: open it, then scroll to it.
  function reveal() {
    var id = decodeURIComponent(location.hash.slice(1));
    var el = id && document.getElementById(id);
    if (!el) return;
    if (el.tagName === 'DETAILS' && !el.open) el.open = true;
    el.scrollIntoView();
  }
  window.addEventListener('hashchange', reveal);
  document.addEventListener('DOMContentLoaded', reveal);

  // The top bar is position: fixed (iOS 27 blur, see base.html): the page is
  // padded by its real height, which changes with the safe area and wrapping.
  var bar = document.querySelector('.topbar');
  function fitBar() {
    document.documentElement.style.setProperty('--topbar-h', bar.offsetHeight + 'px');
  }
  if (bar) {
    fitBar();
    if (window.ResizeObserver) new ResizeObserver(fitBar).observe(bar);
    else addEventListener('resize', fitBar);
  }

  // A tab switch starts at the top of the new tab, not halfway down it.
  document.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target.id === 'pane') window.scrollTo(0, 0);
  });
})();

// One tooltip for anything carrying data-tip: grid cells, stacked-bar
// segments, chart columns. Follows the pointer; the text may hold <b>/<br>,
// written by our own template.
(function () {
  var tip = document.createElement('div');
  tip.id = 'tip';
  tip.hidden = true;
  document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(tip); });
  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) { tip.hidden = true; return; }
    tip.innerHTML = el.getAttribute('data-tip');
    tip.hidden = false;
  });
  document.addEventListener('mousemove', function (e) {
    if (tip.hidden) return;
    var x = e.clientX + 14, y = e.clientY + 14;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = e.clientX - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = e.clientY - tip.offsetHeight - 14;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  document.addEventListener('htmx:beforeSwap', function () { tip.hidden = true; });
})();
