/**
 * Kiosk-shell helpers for the Pi scoreboard / Wi-Fi setup flow.
 *
 * The browser shell shows the normal scoreboard URL in an iframe, but
 * temporarily switches to /wifi/display while the provisioning controller
 * reports setup/join activity.  The functions stay DOM-agnostic so they can
 * be unit-tested under Vitest.
 */

(function (exports) {
    var PROVISIONING_STATES = {
        setup: true,
        join_pending: true,
        joining: true,
        refreshing: true,
        recovering: true,
        error: true
    };

    function isProvisioningStatus(status) {
        if (!status || typeof status !== 'object') return false;
        if (status.setup_active) return true;
        return !!PROVISIONING_STATES[status.state];
    }

    function normalizeDisplayUrl(displayUrl) {
        if (typeof displayUrl !== 'string' || !displayUrl) return '/web/home';
        if (displayUrl[0] !== '/') return '/web/home';
        if (!/^\/web\/[A-Za-z0-9_-]+(?:\?.*)?$/.test(displayUrl)) return '/web/home';
        if (displayUrl.split('?')[0] === '/web/kiosk') return '/web/home';
        return displayUrl;
    }

    function pickFrameUrl(status, displayUrl, setupUrl) {
        if (isProvisioningStatus(status)) return setupUrl || '/wifi/display';
        return normalizeDisplayUrl(displayUrl);
    }

    function buildStatusLabel(status) {
        if (!status || typeof status !== 'object') return 'Wi-Fi status unavailable';
        var pieces = [];
        if (status.state) pieces.push(String(status.state).replace(/_/g, ' '));
        if (status.message) pieces.push(status.message);
        if (status.wifi_ssid) pieces.push('Wi-Fi: ' + status.wifi_ssid);
        else if (status.ethernet) pieces.push('Ethernet connected');
        if (status.ipv4) pieces.push('LAN ' + status.ipv4);
        return pieces.join(' — ') || 'Scoreboard ready';
    }

    function setFrameUrl(iframe, url) {
        if (!iframe || !url) return false;
        if (iframe.getAttribute('data-current-url') === url) return false;
        iframe.setAttribute('data-current-url', url);
        iframe.src = url;
        return true;
    }

    function ensureToast(container) {
        var toast = container && container.toast;
        if (toast) return toast;
        toast = document.createElement('div');
        toast.id = 'kiosk-toast';
        toast.setAttribute('aria-live', 'polite');
        toast.style.position = 'fixed';
        toast.style.left = '1rem';
        toast.style.bottom = '1rem';
        toast.style.zIndex = '9999';
        toast.style.padding = '.55rem .8rem';
        toast.style.borderRadius = '10px';
        toast.style.background = 'rgba(0,0,0,.8)';
        toast.style.color = '#fff';
        toast.style.font = '600 0.95rem system-ui, sans-serif';
        toast.style.boxShadow = '0 10px 24px rgba(0,0,0,.25)';
        toast.style.pointerEvents = 'none';
        toast.style.maxWidth = 'min(90vw, 28rem)';
        toast.style.opacity = '0';
        toast.style.transition = 'opacity .2s ease';
        document.body.appendChild(toast);
        if (container) container.toast = toast;
        return toast;
    }

    function showToast(container, message) {
        var toast = ensureToast(container);
        if (!toast) return;
        if (container && container.toastTimer) {
            window.clearTimeout(container.toastTimer);
        }
        toast.textContent = message;
        toast.style.opacity = '1';
        container.toastTimer = window.setTimeout(function () {
            toast.style.opacity = '0';
        }, 5000);
    }

    function updateShellElements(elements, status, displayUrl, setupUrl) {
        var iframe = elements && elements.iframe;
        var target = pickFrameUrl(status, displayUrl, setupUrl);
        var changed = setFrameUrl(iframe, target);
        if (iframe && target === (setupUrl || '/wifi/display')) {
            var fingerprint = JSON.stringify([
                status && status.state, status && status.setup_active,
                status && status.message, status && status.ipv4,
                status && status.setup_url, status && status.target_ssid
            ]);
            var previous = iframe.getAttribute('data-setup-status');
            if (!changed && previous && previous !== fingerprint) {
                iframe.src = target;
            }
            iframe.setAttribute('data-setup-status', fingerprint);
        }
        return changed;
    }

    function initKioskShell(options) {
        var settings = options || {};
        var elements = {
            iframe: settings.iframe || null,
            badge: settings.badge || null,
            summary: settings.summary || null
        };
        var displayUrl = normalizeDisplayUrl(settings.displayUrl);
        var setupUrl = settings.setupUrl || '/wifi/display';
        var statusUrl = settings.statusUrl || '/wifi/kiosk-status';
        var pollMs = typeof settings.pollMs === 'number' && settings.pollMs > 0 ? settings.pollMs : 5000;
        var stopped = false;
        var timer = null;
        var lastTarget = null;

        function schedule() {
            if (stopped) return;
            timer = window.setTimeout(poll, pollMs);
        }

        function poll() {
            if (stopped) return;
            fetch(statusUrl, { cache: 'no-store', credentials: 'same-origin' })
                .then(function (response) {
                    return response.json().then(function (json) {
                        return { ok: response.ok, json: json };
                    });
                })
                .then(function (result) {
                    if (stopped) return;
                    var status = result && result.json && result.json.status;
                    if (!result.ok || !result.json.success || !status || typeof status !== 'object') {
                        throw new Error('Wi-Fi status unavailable');
                    }
                    var nextTarget = pickFrameUrl(status, displayUrl, setupUrl);
                    updateShellElements(elements, status, displayUrl, setupUrl);
                    if (nextTarget !== lastTarget) {
                        if (lastTarget === setupUrl && nextTarget === displayUrl && status && status.ipv4) {
                            showToast(elements, 'Connected on LAN ' + status.ipv4);
                        }
                        lastTarget = nextTarget;
                    }
                    schedule();
                })
                .catch(function () {
                    if (stopped) return;
                    var nextTarget = pickFrameUrl({ state: 'error' }, displayUrl, setupUrl);
                    if (nextTarget !== lastTarget) {
                        updateShellElements(elements, { state: 'error', message: 'Wi-Fi status unavailable.' }, displayUrl, setupUrl);
                        lastTarget = nextTarget;
                    }
                    schedule();
                });
        }

        lastTarget = pickFrameUrl(settings.initialStatus || null, displayUrl, setupUrl);
        setFrameUrl(elements.iframe, lastTarget);
        poll();

        return {
            stop: function () {
                stopped = true;
                if (timer !== null) window.clearTimeout(timer);
                if (elements.toastTimer) window.clearTimeout(elements.toastTimer);
            },
            refresh: poll,
            update: function (status) {
                updateShellElements(elements, status, displayUrl, setupUrl);
            }
        };
    }

    exports.PROVISIONING_STATES = PROVISIONING_STATES;
    exports.isProvisioningStatus = isProvisioningStatus;
    exports.normalizeDisplayUrl = normalizeDisplayUrl;
    exports.pickFrameUrl = pickFrameUrl;
    exports.buildStatusLabel = buildStatusLabel;
    exports.setFrameUrl = setFrameUrl;
    exports.ensureToast = ensureToast;
    exports.showToast = showToast;
    exports.updateShellElements = updateShellElements;
    exports.initKioskShell = initKioskShell;
})(typeof module !== 'undefined' && module.exports ? module.exports : window);
