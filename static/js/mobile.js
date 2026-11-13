/* CyberAttend - mobile helpers: table labels, double-submit guard, install prompt. No dependencies. */
(function () {
    'use strict';

    // 1) Give every table cell its column title so the phone "card" layout can show it.
    function labelTables() {
        document.querySelectorAll('.table-wrap table').forEach(function (table) {
            var heads = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) { return th.textContent.trim(); });
            if (!heads.length) { table.classList.add('no-stack'); return; }
            table.querySelectorAll('tbody tr').forEach(function (tr) {
                var col = 0;
                Array.prototype.forEach.call(tr.children, function (td) {
                    var span = parseInt(td.getAttribute('colspan') || '1', 10);
                    if (span === 1 && heads[col] && !td.hasAttribute('data-label')) td.setAttribute('data-label', heads[col]);
                    col += span;
                });
            });
        });
    }

    // 2) One tap = one submit (slow mobile networks otherwise create duplicate records).
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.hasAttribute('data-allow-resubmit') || e.defaultPrevented) return;
        var btn = form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
        if (e.submitter) btn = e.submitter;
        if (form.dataset.sent === '1') { e.preventDefault(); return; }
        form.dataset.sent = '1';
        if (btn) { btn.setAttribute('aria-busy', 'true'); btn.style.opacity = '.7'; }
        setTimeout(function () { form.dataset.sent = '0'; if (btn) { btn.removeAttribute('aria-busy'); btn.style.opacity = ''; } }, 6000);
    }, true);

    // 3) Mobile Chrome/Edge "Install app" support (Add to home screen works through the manifest).
    window.addEventListener('beforeinstallprompt', function (e) { window.__caInstall = e; });

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', labelTables);
    else labelTables();
})();
