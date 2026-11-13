/* CyberAttend persistent sidebar: stable fixed rail, remembered state and scroll. */
(function () {
  'use strict';
  var KEY = 'cyberattend.sidebar';
  var SCROLL_KEY = 'cyberattend.sidebar.scroll';
  var body = document.body;
  var sidebar = document.querySelector('.sidebar');
  var mobile = function () { return window.matchMedia('(max-width: 800px)').matches; };
  function read(store, key) { try { return store.getItem(key); } catch (e) { return null; } }
  function write(store, key, value) { try { store.setItem(key, value); } catch (e) {} }
  function apply(collapsed) {
    body.classList.toggle('side-collapsed', collapsed);
    document.querySelectorAll('[data-sidebar-toggle]').forEach(function (button) {
      button.setAttribute('aria-expanded', String(!collapsed));
      button.setAttribute('aria-label', collapsed ? 'Open navigation menu' : 'Close navigation menu');
      button.title = collapsed ? 'Open menu' : 'Close menu';
    });
  }
  /* On desktop, restore the last chosen state. On mobile, start closed. */
  apply(mobile() ? true : read(localStorage, KEY) === '1');
  function restoreScroll() {
    if (!sidebar) return;
    var saved = parseInt(read(sessionStorage, SCROLL_KEY) || '0', 10);
    if (!isNaN(saved)) sidebar.scrollTop = saved;
  }
  restoreScroll();
  requestAnimationFrame(restoreScroll);
  window.addEventListener('pageshow', restoreScroll);
  if (sidebar) sidebar.addEventListener('scroll', function () {
    write(sessionStorage, SCROLL_KEY, String(sidebar.scrollTop));
  }, { passive: true });
  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-sidebar-toggle]');
    if (toggle) {
      var nextCollapsed = !body.classList.contains('side-collapsed');
      apply(nextCollapsed);
      if (!mobile()) write(localStorage, KEY, nextCollapsed ? '1' : '0');
      return;
    }
    var nav = event.target.closest('.sidebar a.nav-item');
    if (nav && sidebar) write(sessionStorage, SCROLL_KEY, String(sidebar.scrollTop));
    if (event.target.closest('[data-sidebar-backdrop]') || (mobile() && nav)) apply(true);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && mobile() && !body.classList.contains('side-collapsed')) apply(true);
  });
  window.addEventListener('resize', function () { if (mobile()) apply(true); });
})();
