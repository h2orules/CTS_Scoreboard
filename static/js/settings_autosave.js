/* Auto-save layer for the Settings page.
 *
 * Any <form data-autosave="1"> gets:
 *   - debounced auto-save on text/textarea/number input (1.2s after last keypress)
 *   - immediate auto-save on select/checkbox/radio change
 *   - inline status pill (Saving… / Saved / Save failed → Retry)
 *
 * Forms still post to their existing action (typically /settings) with
 * FormData(form); the server is unchanged except for an optional JSON
 * early-return when the request includes X-Autosave: 1.
 */
(function () {
    var DEBOUNCE_MS = 1200;
    var SAVED_FADE_MS = 2000;

    function debounce(fn, ms) {
        var t = null;
        return function () {
            if (t) clearTimeout(t);
            t = setTimeout(function () { t = null; fn(); }, ms);
        };
    }

    function ensureStatus(form) {
        var status = form.querySelector(':scope > .autosave-status');
        if (status) return status;
        status = document.createElement('span');
        status.className = 'autosave-status';
        // Attach inline near the bottom of the form. If the form has a
        // hidden Update button, drop the status next to it for layout
        // consistency; otherwise append to the form.
        var anchor = form.querySelector('.autosave-hide');
        if (anchor && anchor.parentNode) {
            anchor.parentNode.appendChild(status);
        } else {
            form.appendChild(status);
        }
        return status;
    }

    function setStatus(status, kind, text) {
        // kind: '', 'saving', 'saved', 'error'
        status.className = 'autosave-status' + (kind ? ' autosave-' + kind : '');
        status.textContent = text || '';
    }

    function attach(form) {
        var status = ensureStatus(form);
        var inflight = null;
        var pending = false;
        var nextFormData = null; // captured at save-trigger time

        function doSave(fd) {
            if (!fd) fd = new FormData(form);
            if (inflight) {
                pending = true;
                nextFormData = fd;
                return;
            }
            setStatus(status, 'saving', 'Saving…');
            inflight = fetch(form.action || window.location.pathname, {
                method: 'POST',
                body: fd,
                credentials: 'same-origin',
                headers: { 'X-Autosave': '1' }
            }).then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                // Try JSON; tolerate empty/non-JSON bodies.
                return r.text().then(function (body) {
                    if (!body) return { ok: true };
                    try { return JSON.parse(body); } catch (e) { return { ok: true }; }
                });
            }).then(function (payload) {
                if (payload && payload.ok === false) {
                    var msg = (payload.errors && payload.errors.length)
                        ? payload.errors.join('; ') : 'Save failed';
                    throw new Error(msg);
                }
                setStatus(status, 'saved', 'Saved');
                setTimeout(function () {
                    if (status.classList.contains('autosave-saved')) {
                        setStatus(status, '', '');
                    }
                }, SAVED_FADE_MS);
            }).catch(function (err) {
                var msg = (err && err.message) ? err.message : 'Save failed';
                setStatus(status, 'error', 'Save failed: ' + msg + ' ');
                var retry = document.createElement('button');
                retry.type = 'button';
                retry.className = 'btn btn-xs btn-warning autosave-retry-btn';
                retry.textContent = 'Retry';
                retry.addEventListener('click', function () {
                    // Re-send the most recent state of the form.
                    doSave(new FormData(form));
                });
                status.appendChild(retry);
                var hint = document.createElement('span');
                hint.className = 'autosave-error-hint';
                hint.textContent = ' (or reload the page and try again)';
                status.appendChild(hint);
            }).then(function () {
                inflight = null;
                if (pending) {
                    pending = false;
                    var fd2 = nextFormData;
                    nextFormData = null;
                    doSave(fd2);
                }
            });
        }

        var debouncedSave = debounce(function () { doSave(); }, DEBOUNCE_MS);

        function isTextish(target) {
            if (!target) return false;
            var tag = (target.tagName || '').toLowerCase();
            if (tag === 'textarea') return true;
            if (tag !== 'input') return false;
            var type = (target.type || 'text').toLowerCase();
            return (type === 'text' || type === 'number' || type === 'search'
                || type === 'tel' || type === 'url' || type === 'email'
                || type === 'password');
        }

        function isPickish(target) {
            if (!target) return false;
            var tag = (target.tagName || '').toLowerCase();
            if (tag === 'select') return true;
            if (tag !== 'input') return false;
            var type = (target.type || '').toLowerCase();
            return (type === 'checkbox' || type === 'radio');
        }

        form.addEventListener('input', function (e) {
            var t = e.target;
            if (!t) return;
            if ((t.type || '').toLowerCase() === 'file') return;
            if (isTextish(t)) {
                debouncedSave();
            } else if (isPickish(t)) {
                // change handler will fire too; rely on that.
            }
        });

        form.addEventListener('change', function (e) {
            var t = e.target;
            if (!t) return;
            if ((t.type || '').toLowerCase() === 'file') return;
            if (isPickish(t) || (t.tagName || '').toLowerCase() === 'select') {
                doSave();
            }
        });

        // Intercept the form's submit event so:
        //  - Enter-in-text-field saves immediately instead of reloading
        //  - action <button name=...> clicks (e.g. ad_up_N) still submit
        //    their key=value pair
        // Buttons that need a fresh server-rendered page (reorder, remove)
        // opt out with data-autosave-reload="1" and trigger a normal POST.
        form.addEventListener('submit', function (e) {
            if (e.submitter && e.submitter.dataset && e.submitter.dataset.autosaveReload === '1') {
                // Let the browser perform the normal POST + reload.
                return;
            }
            e.preventDefault();
            var fd = new FormData(form);
            // SubmitEvent.submitter is the actual element that triggered
            // submission (a specific button). Include its name/value so
            // server-side action handlers (footer_add, …) see it.
            if (e.submitter && e.submitter.name) {
                fd.append(e.submitter.name, e.submitter.value || '');
            }
            doSave(fd);
        });

        // Public hook so other inline scripts (e.g. message-pages add/remove)
        // can request an immediate save after DOM-only mutations.
        form._autosaveBump = function () { doSave(); };
    }

    // Public helper: trigger autosave for a form (by element or id).
    window.SettingsAutosave = {
        save: function (formOrId) {
            var f = (typeof formOrId === 'string')
                ? document.getElementById(formOrId) : formOrId;
            if (f && typeof f._autosaveBump === 'function') f._autosaveBump();
        }
    };

    function init() {
        document.body.classList.add('autosave-ready');
        var forms = document.querySelectorAll('form[data-autosave="1"]');
        for (var i = 0; i < forms.length; i++) attach(forms[i]);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
