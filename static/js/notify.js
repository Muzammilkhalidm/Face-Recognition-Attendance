/* notify.js - employee notification popup + sound + live numbered counters.
   Polls the server; every new admin announcement / task / reply / warning opens a popup with numbered
   items and plays the ringtone. Ringtone file: static/sounds/notify.mp3 (or .ogg/.m4a/.wav) - see /notification-sound */
(function () {
    'use strict';
    var meta = document.querySelector('meta[name="csrf-token"]');
    var csrf = meta ? meta.content : '';
    var root = document.getElementById('notify-root');
    if (!root) return;

    var isAdmin = root.getAttribute('data-who') === 'admin';
    var POLL_URL = root.getAttribute('data-poll') || '/api/notifications/poll';
    var READ_URL = root.getAttribute('data-read') || '/api/notifications/read';
    var KIND_LABEL = { announcement: 'Announcement', task: 'Task', message: 'Message', warning: 'Warning',
                       leave: 'Leave', wfh: 'Work from home', approval: 'Approval', security: 'Security alert', salary: 'Salary', hr: 'HR' };
    var baseTitle = document.title;
    var shown = [];                // notifications currently inside the popup
    var audio = new Audio('/notification-sound?v=5');
    audio.preload = 'auto';
    audio.volume = 1;
    var soundBlocked = false;

    function playSound() {
        try {
            audio.currentTime = 0;
            var p = audio.play();
            if (p && p.catch) p.catch(function () { soundBlocked = true; });
        } catch (e) { soundBlocked = true; }
    }
    // Browsers block sound until the user has touched the page once; play it as soon as they do.
    ['click', 'keydown', 'touchstart'].forEach(function (ev) {
        window.addEventListener(ev, function () {
            if (soundBlocked) { soundBlocked = false; playSound(); }
        }, { passive: true });
    });

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    function updateCounts(counts) {
        document.querySelectorAll('[data-count-kind]').forEach(function (b) {
            var n = counts[b.getAttribute('data-count-kind')] || 0;
            b.textContent = n;
            b.hidden = n === 0;
        });
        document.title = (counts.total ? '(' + counts.total + ') ' : '') + baseTitle;
    }

    function render() {
        root.textContent = '';
        if (!shown.length) return;
        var back = el('div', 'popup-backdrop');
        var box = el('div', 'popup');
        box.setAttribute('role', 'alertdialog');
        box.setAttribute('aria-modal', 'true');
        var head = el('div', 'popup-head');
        head.appendChild(el('span', 'num', shown.length));
        var who = isAdmin ? 'from employees' : 'from Admin';
        head.appendChild(el('h3', null, shown.length === 1 ? 'New notification ' + who : shown.length + ' new notifications ' + who));
        var list = el('div', 'popup-list');
        shown.forEach(function (n, i) {
            var item = el('div', 'popup-item ' + n.kind);
            item.appendChild(el('span', 'num', i + 1));
            var body = el('div');
            var h = el('h4');
            if (n.url) { var a = el('a', null, n.title); a.href = n.url; h.appendChild(a); } else { h.textContent = n.title; }
            body.appendChild(h);
            if (n.body) body.appendChild(el('p', null, n.body));
            body.appendChild(el('small', null, (KIND_LABEL[n.kind] || 'Notice') + ' · ' + (n.created_at || '').slice(0, 16)));
            item.appendChild(body);
            list.appendChild(item);
        });
        var foot = el('div', 'popup-foot');
        var ok = el('button', 'btn small', 'OK');
        ok.type = 'button';
        ok.addEventListener('click', dismiss);
        foot.appendChild(ok);
        if (shown[0].url) {
            var open = el('a', 'btn secondary small', 'Open');
            open.href = shown[0].url;
            foot.insertBefore(open, ok);
        }
        box.appendChild(head); box.appendChild(list); box.appendChild(foot);
        back.appendChild(box);
        root.appendChild(back);
        ok.focus();
    }

    function dismiss() {
        var ids = shown.map(function (n) { return n.id; });
        shown = [];
        render();
        fetch(READ_URL, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify({ ids: ids })
        }).then(function (r) { return r.json(); }).then(function (d) { if (d.counts) updateCounts(d.counts); })
          .catch(function () {});
    }
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && shown.length) dismiss(); });

    function poll() {
        fetch(POLL_URL, { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d || !d.ok) return;
                updateCounts(d.counts);
                if (d.popups && d.popups.length) {
                    shown = shown.concat(d.popups);
                    render();
                    playSound();
                }
            }).catch(function () {});
    }

    /* ======================= MEETING ALARM (employees only) =======================
       Polls every 3 s. When admin rings a meeting alarm the whole screen flashes, a loud siren loops,
       phones vibrate and (if allowed) a system notification is shown, until the employee taps "I'm coming". */
    if (!isAdmin) {
        var alarmAudio = new Audio('/static/sounds/alarm.wav?v=1');
        alarmAudio.loop = true; alarmAudio.volume = 1; alarmAudio.preload = 'auto';
        var unlocked = false, enableBtn = null, ringing = null, vibTimer = null, alarmEl = null, sysNotified = {};

        function unlockAudio() {
            if (unlocked) return;
            [audio, alarmAudio].forEach(function (a) {
                try {
                    a.muted = true;
                    var p = a.play();
                    var done = function () { a.pause(); a.currentTime = 0; a.muted = false; };
                    if (p && p.then) p.then(done).catch(function () { a.muted = false; }); else done();
                } catch (e) {}
            });
            unlocked = true;
            if (enableBtn) { enableBtn.remove(); enableBtn = null; }
            try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
            if (ringing) startRing();
        }
        ['click', 'touchstart', 'keydown'].forEach(function (ev) { window.addEventListener(ev, unlockAudio, { passive: true }); });
        // until the first tap the browser blocks sound: show a small, friendly button
        setTimeout(function () {
            if (unlocked) return;
            enableBtn = el('button', 'alarm-enable', '\uD83D\uDD14 Tap to enable alarm sound');
            enableBtn.type = 'button';
            enableBtn.addEventListener('click', unlockAudio);
            document.body.appendChild(enableBtn);
        }, 1200);

        function startRing() {
            try {
                var p = alarmAudio.play();
                if (p && p.catch) p.catch(function () {
                    var tip = alarmEl && alarmEl.querySelector('.alarm-sound-tip'); if (tip) tip.hidden = false;
                });
            } catch (e) {}
            if (!vibTimer && navigator.vibrate) {
                var buzz = function () { try { navigator.vibrate([500, 200, 500, 200, 500]); } catch (e) {} };
                buzz(); vibTimer = setInterval(buzz, 1900);
            }
        }
        function stopRing() {
            try { alarmAudio.pause(); alarmAudio.currentTime = 0; } catch (e) {}
            if (vibTimer) { clearInterval(vibTimer); vibTimer = null; }
            try { if (navigator.vibrate) navigator.vibrate(0); } catch (e) {}
        }
        function hideAlarm() {
            if (alarmEl) { alarmEl.remove(); alarmEl = null; }
            stopRing(); ringing = null; document.title = baseTitle;
        }
        function showAlarm(a) {
            ringing = a;
            if (alarmEl) alarmEl.remove();
            alarmEl = el('div', 'alarm-overlay');
            alarmEl.setAttribute('role', 'alertdialog');
            var box = el('div', 'alarm-box');
            box.appendChild(el('div', 'alarm-bell', '\uD83D\uDD14'));
            box.appendChild(el('small', null, 'MEETING ALERT'));
            box.appendChild(el('h2', null, a.title));
            if (a.message) box.appendChild(el('p', null, a.message));
            box.appendChild(el('div', 'alarm-from', 'From ' + (a.created_by || 'Admin') + ' \u00B7 ' + a.department_name));
            var btn = el('button', null, "\u2714 I'm coming \u2014 stop alarm");
            btn.type = 'button';
            btn.addEventListener('click', function () { ackAlarm(a.id); });
            box.appendChild(btn);
            var tip = el('div', 'alarm-sound-tip', 'Tap anywhere on the screen to turn the sound on');
            tip.hidden = true; box.appendChild(tip);
            alarmEl.appendChild(box);
            alarmEl.addEventListener('click', function (e) { if (e.target !== btn) { unlocked = true; startRing(); tip.hidden = true; } });
            document.body.appendChild(alarmEl);
            document.title = '\uD83D\uDEA8 MEETING ALERT - ' + baseTitle;
            startRing();
            if (!sysNotified[a.id]) {
                sysNotified[a.id] = 1;
                try {
                    if ('Notification' in window && Notification.permission === 'granted')
                        new Notification('\uD83D\uDD14 ' + a.title, { body: a.message || 'Meeting alert from admin', requireInteraction: true, vibrate: [500, 200, 500], tag: 'alarm-' + a.id });
                } catch (e) {}
            }
        }
        function ackAlarm(id) {
            hideAlarm();
            fetch('/api/alarm/ack', { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify({ id: id }) }).catch(function () {});
        }
        function pollAlarm() {
            fetch('/api/alarm/poll', { credentials: 'same-origin', cache: 'no-store' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (d) {
                    if (!d || !d.ok) return;
                    if (d.alarms && d.alarms.length) {
                        var a = d.alarms[0];
                        if (!ringing || ringing.id !== a.id) showAlarm(a);
                    } else if (ringing) { hideAlarm(); }   // admin stopped it / expired / acknowledged elsewhere
                }).catch(function () {});
        }
        pollAlarm();
        setInterval(pollAlarm, 3000);
        document.addEventListener('visibilitychange', function () { if (!document.hidden) pollAlarm(); });
    }

    poll();
    setInterval(poll, 15000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
