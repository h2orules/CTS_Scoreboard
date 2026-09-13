(function (exports) {
    'use strict';

    var STATUS_POLL_MS = 2000;
    var CACHE_POLL_MS = 5000;
    var ACTIVE_STATES = {
        starting: true,
        joining: true,
        refreshing: true,
        recovering: true
    };

    var ICONS = {
        connected: '<svg viewBox="0 0 24 24" width="20" height="20" fill="#333" aria-hidden="true">'
            + '<path d="M1 9l2 2a12.73 12.73 0 0 1 18 0l2-2A15.57 15.57 0 0 0 1 9z"/>'
            + '<path d="M5 13l2 2a8.5 8.5 0 0 1 10 0l2-2a11.36 11.36 0 0 0-14 0z"/>'
            + '<path d="M9 17l3 3 3-3a4.24 4.24 0 0 0-6 0z"/></svg>',
        disconnected: '<svg viewBox="0 0 24 24" width="20" height="20" fill="#999" aria-hidden="true">'
            + '<path d="M1 9l2 2a12.73 12.73 0 0 1 18 0l2-2A15.57 15.57 0 0 0 1 9z" opacity="0.3"/>'
            + '<path d="M5 13l2 2a8.5 8.5 0 0 1 10 0l2-2a11.36 11.36 0 0 0-14 0z" opacity="0.3"/>'
            + '<path d="M9 17l3 3 3-3a4.24 4.24 0 0 0-6 0z" opacity="0.3"/></svg>',
        ethernet: '<svg viewBox="0 0 24 24" width="18" height="18" fill="#333" aria-hidden="true">'
            + '<path d="M7.77 6.76L6.23 5.48.82 12l5.41 6.52 1.54-1.28L3.42 12l4.35-5.24zM14.23 6.76l1.54-1.28L21.18 12l-5.41 6.52-1.54-1.28L18.58 12l-4.35-5.24zM7.5 14h2v-4h-2v4zm3-4v4h2v-4h-2zm5 0v4h2v-4h-2z"/></svg>',
        lock: '<svg class="wifi-lock-icon" viewBox="0 0 16 16" fill="#999" aria-hidden="true">'
            + '<path d="M12 7h-1V5a3 3 0 0 0-6 0v2H4a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1zM6 5a2 2 0 0 1 4 0v2H6V5z"/></svg>'
    };

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function createNullMap() {
        return Object.create(null);
    }

    function isBusyState(status) {
        return !!(status && ACTIVE_STATES[String(status.state || '').toLowerCase()]);
    }

    function isPollingState(status) {
        if (!status) return false;
        var state = String(status.state || '').toLowerCase();
        return state === 'join_pending' || isBusyState(status);
    }

    function normalizeSecurity(raw) {
        if (raw == null) return 'auto';
        var value = String(raw);
        if (value === '') return 'open';
        var compact = value.trim().toUpperCase().replace(/\s+/g, ' ');
        if (/802\.1X|EAP|WEP/.test(compact)) return 'unsupported';
        if (compact === 'OPEN') return 'open';
        if (compact.indexOf('WPA3') !== -1 && compact.indexOf('WPA2') === -1) return 'wpa3';
        if (compact.indexOf('WPA2') !== -1) return 'wpa2';
        return 'auto';
    }

    function normalizeNetwork(network) {
        network = network || {};
        return {
            ssid: network.ssid == null ? '' : String(network.ssid),
            signal: Number(network.signal || 0),
            security: normalizeSecurity(network.security),
            rawSecurity: network.security == null ? '' : String(network.security),
            in_use: !!network.in_use,
            hidden: !!network.hidden,
            profile_id: network.profile_id == null ? '' : String(network.profile_id)
        };
    }

    function normalizeResponsePayload(payload) {
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new Error('Invalid Wi-Fi response');
        }
        if (Object.prototype.hasOwnProperty.call(payload, 'success') && payload.success === false) {
            var msg = payload.message || 'Request failed';
            var err = new Error(msg);
            err.payload = payload;
            throw err;
        }
        return { success: true, payload: payload };
    }

    function parseJsonResponse(response) {
        return response.text().then(function (text) {
            var contentType = response.headers && response.headers.get
                ? (response.headers.get('content-type') || '')
                : '';
            var isJson = contentType.indexOf('application/json') !== -1
                || contentType.indexOf('+json') !== -1;
            var payload = null;

            if (isJson) {
                if (!text || !text.trim()) {
                    throw new Error('Malformed JSON response');
                }
                try {
                    payload = JSON.parse(text);
                } catch (err) {
                    throw new Error('Malformed JSON response');
                }
            } else {
                var trimmed = text ? text.trim() : '';
                if (trimmed) {
                    try {
                        payload = JSON.parse(trimmed);
                    } catch (err2) {
                        payload = { text: text };
                    }
                } else {
                    payload = {};
                }
            }

            if (!response.ok) {
                var message = payload && payload.message
                    ? payload.message
                    : (response.status === 401
                        ? 'Session expired. Please sign in again.'
                        : 'Request failed');
                var error = new Error(message);
                error.status = response.status;
                error.payload = payload;
                throw error;
            }

            if (!isJson) {
                var lower = String(text || '').toLowerCase();
                if (response.url && response.url.indexOf('/login') !== -1
                    || lower.indexOf('swimming scoreboard: login') !== -1) {
                    throw new Error('Session expired. Please sign in again.');
                }
                throw new Error('Expected JSON response from Wi-Fi endpoint');
            }

            return normalizeResponsePayload(payload).payload;
        });
    }

    function requestJson(url, csrfToken, payload) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify(payload || {})
        }).then(parseJsonResponse).then(function (data) {
            if (data.success !== true) {
                throw new Error('Invalid Wi-Fi operation response');
            }
            return data;
        });
    }

    function getJson(url) {
        return fetch(url, { credentials: 'same-origin' }).then(parseJsonResponse).then(function (data) {
            if (data && Object.prototype.hasOwnProperty.call(data, 'success') && data.success === false) {
                var err = new Error(data.message || 'Request failed');
                err.payload = data;
                throw err;
            }
            return data;
        });
    }

    function makeSignalBars(signal) {
        var wrap = document.createElement('span');
        wrap.className = 'wifi-signal-bars';
        var thresholds = [20, 40, 60, 80];
        for (var i = 0; i < thresholds.length; i++) {
            var bar = document.createElement('span');
            if (signal >= thresholds[i]) bar.className = 'filled';
            wrap.appendChild(bar);
        }
        return wrap;
    }

    function makeBadge(text, className) {
        var span = document.createElement('span');
        span.className = 'wifi-badge ' + className;
        span.textContent = text;
        return span;
    }

    function makeIcon(name) {
        var span = document.createElement('span');
        span.innerHTML = ICONS[name] || '';
        return span.firstChild || span;
    }

    function byId(id) {
        return document.getElementById(id);
    }

    function setVisible(el, visible, display) {
        if (!el) return;
        if (visible) el.classList.remove('hidden');
        el.style.display = visible ? (display || '') : 'none';
    }

    function setText(el, text) {
        if (el) el.textContent = text == null ? '' : String(text);
    }

    function clearChildren(el) {
        if (el) el.innerHTML = '';
    }

    function profileMapFromList(profiles) {
        var map = createNullMap();
        (profiles || []).forEach(function (profile) {
            if (profile && profile.ssid != null) {
                map[String(profile.ssid)] = profile;
            }
        });
        return map;
    }

    function savedSetFromList(saved) {
        var set = createNullMap();
        (saved || []).forEach(function (ssid) {
            set[String(ssid)] = true;
        });
        return set;
    }

    function getLastOperation(status) {
        if (!status || typeof status !== 'object') return null;
        if (isBusyState(status) || status.state === 'join_pending') return null;
        if (!status.last_operation || typeof status.last_operation !== 'object') return null;
        if (typeof status.last_operation.id !== 'string'
            || typeof status.last_operation.success !== 'boolean'
            || typeof status.last_operation.message !== 'string') return null;
        return status.last_operation;
    }

    function summarizeStatus(status, handoff) {
        status = status || {};
        handoff = handoff || {};
        var state = String(status.state || 'unknown').toLowerCase();
        var lastOperation = getLastOperation(status);
        var targetSsid = status.target_ssid || handoff.targetSsid || status.wifi_ssid || '';
        var localUrl = status.local_url || handoff.localUrl || '';
        var setupUrl = status.setup_url || handoff.setupUrl || '';
        var apNetwork = status.ap_network || handoff.apNetwork || '';
        var message = (lastOperation && lastOperation.message) || status.message || handoff.message || '';
        var kind = 'neutral';
        var title = 'Wi-Fi not connected';

        if (lastOperation && lastOperation.success === false) {
            kind = 'error';
            title = 'Wi-Fi needs attention';
        } else if (state === 'error') {
            kind = 'error';
            title = 'Wi-Fi needs attention';
        } else if (state === 'join_pending') {
            kind = 'warning';
            title = targetSsid ? 'Preparing join for ' + targetSsid : 'Preparing Wi-Fi join';
        } else if (state === 'joining') {
            kind = 'warning';
            title = targetSsid ? 'Joining ' + targetSsid : 'Joining Wi-Fi';
        } else if (state === 'refreshing') {
            kind = 'warning';
            title = 'Refreshing Wi-Fi scan';
        } else if (state === 'recovering') {
            kind = 'warning';
            title = 'Recovering connectivity';
        } else if (state === 'normal' && status.wifi_ssid) {
            kind = 'success';
            title = 'Connected to ' + status.wifi_ssid;
        } else if (status.ethernet) {
            kind = 'success';
            title = 'Ethernet connected';
        }

        if (lastOperation && lastOperation.success === true && !status.wifi_ssid && !status.ethernet && state !== 'error') {
            kind = 'success';
            if (message) title = message;
        }

        var lines = [];
        if (message) lines.push(message);
        if (targetSsid && state !== 'normal') {
            lines.push('Target SSID: ' + targetSsid);
        }
        if (apNetwork) lines.push('AP subnet: ' + apNetwork);
        if (setupUrl) lines.push('Setup URL: ' + setupUrl);
        if (localUrl) lines.push('Reconnect URL: ' + localUrl);
        if (state === 'joining' || state === 'refreshing' || state === 'recovering') {
            lines.push('Keep this page open while the network changes.');
            lines.push('If the page drops, reopen it from the setup or local URL above.');
        } else if (state === 'join_pending') {
            lines.push('Review the target SSID and reconnect URLs, then commit when ready.');
        }
        if (!lines.length && status.ethernet) {
            lines.push('You can keep using Ethernet while setup continues.');
        }
        if (!lines.length && state === 'error') {
            lines.push('The controller reported an error.');
        }
        if (!lines.length && !targetSsid && !status.ethernet) {
            lines.push('Choose a network to begin setup.');
        }

        return {
            kind: kind,
            title: title,
            lines: lines,
            state: state,
            busy: isBusyState(status),
            targetSsid: targetSsid,
            operationId: status.operation_id || handoff.operationId || null,
            localUrl: localUrl,
            setupUrl: setupUrl,
            apNetwork: apNetwork,
            wifiSsid: status.wifi_ssid || '',
            setupActive: !!status.setup_active,
            message: message,
            lastOperation: lastOperation
        };
    }

    function createState(config) {
        return {
            config: config,
            mode: config.mode || 'settings',
            controllerEnabled: !!config.controllerEnabled,
            initialNetworks: config.initialNetworks || [],
            initialSaved: config.initialSaved || [],
            initialProfiles: config.initialProfiles || [],
            status: config.initialStatus || {},
            summary: summarizeStatus(config.initialStatus || {}, {}),
            handoff: {
                operationId: (config.initialStatus && config.initialStatus.operation_id) || null,
                targetSsid: (config.initialStatus && (config.initialStatus.target_ssid || config.initialStatus.wifi_ssid)) || '',
                localUrl: (config.initialStatus && config.initialStatus.local_url) || '',
                setupUrl: (config.initialStatus && config.initialStatus.setup_url) || '',
                apNetwork: (config.initialStatus && config.initialStatus.ap_network) || '',
                message: (config.initialStatus && config.initialStatus.message) || ''
            },
            polling: false,
            disposed: false,
            requestInFlight: false,
            awaitingHandoff: false,
            statusTimer: null,
            cacheTimer: null,
            cacheFetchInFlight: false,
            statusFetchInFlight: false,
            cacheDirty: false,
            expandoScanned: false,
            lastBusy: isBusyState(config.initialStatus || {}),
            lastCacheSignature: '',
            visibleError: '',
            actionBannerMessage: ''
        };
    }

    function applyStatusToHandoff(state, status, sourcePayload) {
        status = status || {};
        sourcePayload = sourcePayload || {};
        var next = state.handoff || {};
        if (status.operation_id) next.operationId = status.operation_id;
        if (status.target_ssid) {
            next.targetSsid = status.target_ssid;
        } else if (!next.targetSsid && sourcePayload.ssid) {
            next.targetSsid = sourcePayload.ssid;
        }
        if (!next.targetSsid && status.wifi_ssid && isBusyState(status)) {
            next.targetSsid = status.wifi_ssid;
        }
        if (status.local_url) next.localUrl = status.local_url;
        if (status.setup_url) next.setupUrl = status.setup_url;
        if (status.ap_network) next.apNetwork = status.ap_network;
        if (status.message) next.message = status.message;
        state.handoff = next;
        return next;
    }

    function renderStatusCard(state, status, summary) {
        var card = byId('wifi-status-card');
        var line = byId('wifi-status-line');
        var message = byId('wifi-status-message');
        if (!card || !line || !message) return;

        clearChildren(line);
        line.appendChild(makeIcon(summary.kind === 'success' ? 'connected' : 'disconnected'));
        var title = document.createElement('span');
        title.textContent = summary.title;
        line.appendChild(title);

        var visibleError = state && state.visibleError ? String(state.visibleError) : '';
        if (visibleError) {
            message.className = 'wifi-status-message error';
            message.textContent = visibleError;
            return;
        }
        message.className = 'wifi-status-message' + (summary.kind === 'error' ? ' error' : (summary.kind === 'success' ? ' success' : ''));
        message.textContent = summary.lines.join(' ');
    }

    function renderActionCard(state, status, summary) {
        var card = byId('wifi-action-card');
        var title = byId('wifi-action-title');
        var text = byId('wifi-action-text');
        var note = byId('wifi-setup-note');
        var commit = byId('wifi-commit-btn');
        var cancel = byId('wifi-cancel-btn');
        var refresh = byId('wifi-action-refresh-btn');
        var retry = byId('wifi-retry-btn');
        if (!card || !title || !text || !note || !commit || !cancel || !refresh || !retry) return;

        var showAction = state.controllerEnabled && (state.mode === 'setup'
            || summary.state === 'join_pending'
            || summary.busy
            || summary.kind === 'error'
            || (state.handoff.operationId && summary.state !== 'normal'));
        setVisible(card, showAction, 'grid');
        if (!showAction) {
            if (state.refreshButton) state.refreshButton.disabled = false;
            if (state.hiddenButton) state.hiddenButton.disabled = false;
            return;
        }

        var target = summary.targetSsid || state.handoff.targetSsid || '';
        var bodyText = state.actionBannerMessage || summary.lines.join(' ');
        if (state.actionMode === 'commit') {
            setText(title, 'Committing…');
            setText(text, 'Committing join for ' + (target || 'the selected network') + '…');
        } else if (summary.state === 'join_pending') {
            setText(title, 'Join handoff');
            setText(text, bodyText || 'Review the target SSID, reconnect URL, and AP fallback URL before committing.');
        } else if (summary.kind === 'error') {
            setText(title, 'Wi-Fi setup error');
            setText(text, bodyText);
        } else if (summary.busy) {
            setText(title, summary.title);
            setText(text, bodyText);
        } else {
            setText(title, 'Wi-Fi setup');
            setText(text, bodyText);
        }

        clearChildren(note);
        if (target) {
            var t = document.createElement('div');
            t.className = 'wifi-handoff-line';
            t.appendChild(document.createTextNode('Target SSID: '));
            var tcode = document.createElement('code');
            tcode.textContent = target;
            t.appendChild(tcode);
            note.appendChild(t);
        }
        if (summary.localUrl) {
            var l = document.createElement('div');
            l.className = 'wifi-handoff-line';
            l.appendChild(document.createTextNode('Reconnect URL: '));
            var lcode = document.createElement('code');
            lcode.textContent = summary.localUrl;
            l.appendChild(lcode);
            note.appendChild(l);
        }
        if (summary.setupUrl) {
            var s = document.createElement('div');
            s.className = 'wifi-handoff-line';
            s.appendChild(document.createTextNode('Setup/AP URL: '));
            var scode = document.createElement('code');
            scode.textContent = summary.setupUrl;
            s.appendChild(scode);
            note.appendChild(s);
        }
        if (summary.apNetwork) {
            var a = document.createElement('div');
            a.className = 'wifi-handoff-line';
            a.appendChild(document.createTextNode('AP subnet: '));
            var acode = document.createElement('code');
            acode.textContent = summary.apNetwork;
            a.appendChild(acode);
            note.appendChild(a);
        }

        var pending = summary.state === 'join_pending';
        var blocked = summary.busy || state.requestInFlight || state.awaitingHandoff;
        commit.disabled = blocked || !pending;
        cancel.disabled = blocked || !pending;
        setVisible(commit, pending, 'inline-flex');
        setVisible(cancel, pending, 'inline-flex');
        setVisible(refresh, true, 'inline-flex');
        refresh.disabled = blocked || pending;
        retry.disabled = blocked || pending;
        setVisible(retry, state.mode === 'setup', 'inline-flex');
        if (state.refreshButton) state.refreshButton.disabled = blocked || pending;
        if (state.hiddenButton) state.hiddenButton.disabled = blocked || pending;
    }

    function updateVisibleError(state, message, source) {
        state.visibleError = message || '';
        state.visibleErrorSource = message ? (source || 'action') : '';
        if (state.summary && message) {
            state.summary.kind = 'error';
        } else if (!message) {
            state.summary = summarizeStatus(state.status, state.handoff);
        }
        renderStatusCard(state, state.status, state.summary || summarizeStatus(state.status, state.handoff));
        var messageEl = byId('wifi-status-message');
        if (messageEl && message) {
            messageEl.className = 'wifi-status-message error';
            messageEl.textContent = message;
        }
        renderActionCard(state, state.status, state.summary);
    }

    function renderNetworks(state, payload) {
        payload = payload || {};
        var list = byId('wifi-network-list');
        if (!list) return;
        clearChildren(list);

        var networks = Array.isArray(payload.networks) ? payload.networks.map(normalizeNetwork) : [];
        var savedMap = savedSetFromList(payload.saved || []);
        var profileMap = profileMapFromList(payload.profiles || []);

        if (!networks.length) {
            var empty = document.createElement('div');
            empty.className = 'wifi-spinner-row';
            empty.style.color = '#999';
            empty.textContent = state.mode === 'setup' ? 'No cached networks yet.' : 'Open this section to scan for networks.';
            list.appendChild(empty);
            return;
        }

        networks.forEach(function (network) {
            var row = document.createElement('div');
            row.className = 'wifi-network-row';
            row.dataset.ssid = network.ssid;
            if (network.profile_id) row.dataset.profileId = network.profile_id;
            if (network.rawSecurity) row.dataset.security = network.rawSecurity;
            if (network.hidden) row.dataset.hidden = '1';
            if (network.in_use) row.classList.add('active');

            row.appendChild(makeSignalBars(network.signal));
            if (network.security === 'open') {
                var spacer = document.createElement('span');
                spacer.style.width = '12px';
                spacer.style.display = 'inline-block';
                row.appendChild(spacer);
            } else {
                row.appendChild(makeIcon('lock'));
            }

            var ssid = document.createElement('span');
            ssid.className = 'wifi-ssid';
            ssid.textContent = network.ssid;
            row.appendChild(ssid);

            var profile = profileMap[network.ssid] || null;
            var isSaved = !!savedMap[network.ssid] || !!profile;
            if (network.in_use) {
                row.appendChild(makeBadge('Connected', 'wifi-badge-connected'));
            } else if (isSaved) {
                row.appendChild(makeBadge('Saved', 'wifi-badge-saved'));
            } else if (network.security === 'open') {
                row.appendChild(makeBadge('Open', 'wifi-badge-hidden'));
            }

            row.addEventListener('click', function () {
                openOverlay(state, network, profile, 'connect');
            });

            if (isSaved) {
                var connect = document.createElement('button');
                connect.type = 'button';
                connect.className = 'wifi-inline-btn';
                connect.textContent = 'Connect';
                connect.addEventListener('click', function (event) {
                    event.stopPropagation();
                    openOverlay(state, network, profile, 'connect');
                });
                row.appendChild(connect);

                var edit = document.createElement('button');
                edit.type = 'button';
                edit.className = 'wifi-inline-btn';
                edit.textContent = 'Edit';
                edit.addEventListener('click', function (event) {
                    event.stopPropagation();
                    openOverlay(state, network, profile, 'edit');
                });
                row.appendChild(edit);

                var forget = document.createElement('button');
                forget.type = 'button';
                forget.className = 'wifi-inline-btn wifi-btn-danger';
                forget.textContent = 'Forget';
                forget.addEventListener('click', function (event) {
                    event.stopPropagation();
                    forgetNetwork(state, network, profile);
                });
                row.appendChild(forget);
            }

            if (!network.in_use && !isSaved && network.security === 'open') {
                var openBtn = document.createElement('button');
                openBtn.type = 'button';
                openBtn.className = 'wifi-inline-btn';
                openBtn.textContent = 'Connect';
                openBtn.addEventListener('click', function (event) {
                    event.stopPropagation();
                    openOverlay(state, network, null, 'connect');
                });
                row.appendChild(openBtn);
            }

            list.appendChild(row);
        });
    }

    function renderFromScanResponse(state, payload) {
        payload = payload || {};
        renderNetworks(state, payload);
        if (payload.status) {
            applyStatus(state, payload.status, { forceRender: true, sourcePayload: {} });
        }
    }

    function openOverlay(state, network, profile, mode) {
        if (state.summary.busy || state.requestInFlight || state.awaitingHandoff
            || state.summary.state === 'join_pending') {
            updateVisibleError(state, 'Finish or cancel the current network operation first.');
            return;
        }
        if (network && normalizeSecurity(network.rawSecurity || network.security) === 'unsupported') {
            updateVisibleError(state, 'Enterprise and WEP networks are not supported. Choose a personal or open network.');
            return;
        }
        var overlay = byId('wifi-connect-overlay');
        var title = byId('wifi-connect-title');
        var msg = byId('wifi-connect-msg');
        var ssid = byId('wifi-connect-ssid');
        var password = byId('wifi-connect-password');
        var security = byId('wifi-connect-security');
        var hidden = byId('wifi-connect-hidden');
        var profileId = byId('wifi-connect-profile-id');
        var action = byId('wifi-connect-action');
        if (!overlay || !title || !msg || !ssid || !password || !security || !hidden || !profileId || !action) return;

        state.overlayMode = mode || 'connect';
        state.overlayNetwork = network || null;
        state.overlayProfile = profile || null;

        title.textContent = state.overlayMode === 'edit'
            ? ('Update password for ' + (network && network.ssid ? network.ssid : 'network'))
            : ('Connect to ' + (network && network.ssid ? network.ssid : 'network'));
        msg.textContent = '';
        msg.className = 'wifi-overlay-msg';
        ssid.value = network && network.ssid != null ? network.ssid : '';
        password.value = '';
        password.type = 'password';
        hidden.checked = !!(network && network.hidden);
        profileId.value = profile && profile.id ? profile.id : '';
        if (state.overlayMode === 'edit') {
            security.value = normalizeSecurity(network && network.rawSecurity);
        } else if (network && network.security === 'open') {
            security.value = 'open';
        } else if (network && network.security === 'wpa2') {
            security.value = 'wpa2';
        } else if (network && network.security === 'wpa3') {
            security.value = 'wpa3';
        } else {
            security.value = 'auto';
        }
        action.textContent = state.overlayMode === 'edit' ? 'Update' : 'Connect';
        overlay.style.display = 'flex';
        ssid.focus();
    }

    function closeOverlay() {
        var overlay = byId('wifi-connect-overlay');
        if (overlay) overlay.style.display = 'none';
    }

    function overlayPayload(state) {
        var ssid = byId('wifi-connect-ssid');
        var password = byId('wifi-connect-password');
        var security = byId('wifi-connect-security');
        var hidden = byId('wifi-connect-hidden');
        var profileId = byId('wifi-connect-profile-id');
        if (!ssid || !password || !security || !hidden || !profileId) return null;
        if (ssid.value === '') {
            throw new Error('Network name is required');
        }
        var payload = {
            ssid: ssid.value,
            password: password.value || null,
            hidden: !!hidden.checked,
            security: security.value === 'auto' ? null : security.value
        };
        if (profileId.value) payload.profile_id = profileId.value;
        if (!payload.password && (state.overlayMode === 'edit'
            || (!payload.profile_id && (payload.security === 'wpa2' || payload.security === 'wpa3')))) {
            throw new Error('Password is required for this network');
        }
        if (payload.security === 'open' && payload.password) {
            throw new Error('Open networks do not use a password');
        }
        return payload;
    }

    function setOverlayMessage(kind, text) {
        var msg = byId('wifi-connect-msg');
        if (!msg) return;
        msg.className = 'wifi-overlay-msg' + (kind ? ' ' + kind : '');
        msg.textContent = text || '';
    }

    function updateActionFromPrepare(state, payload, result) {
        var status = result && result.status ? result.status : {};
        state.handoff = {
            operationId: result && result.operation_id ? result.operation_id : (status.operation_id || state.handoff.operationId || null),
            targetSsid: payload.ssid || status.target_ssid || state.handoff.targetSsid || '',
            localUrl: status.local_url || state.handoff.localUrl || '',
            setupUrl: status.setup_url || state.handoff.setupUrl || state.config.setupUrl || '',
            apNetwork: status.ap_network || state.handoff.apNetwork || '',
            message: result && result.message ? result.message : (status.message || state.handoff.message || '')
        };
        state.status = status || state.status;
        state.summary = summarizeStatus(status || state.status, state.handoff);
        state.lastBusy = isBusyState(status || state.status);
        renderStatusCard(state, state.status, state.summary);
        renderActionCard(state, state.status, state.summary);
        state.actionMode = '';
    }

    function applyStatus(state, status, options) {
        if (state.disposed) return;
        options = options || {};
        var prevBusy = state.lastBusy;
        state.status = status || {};
        if (status && status.target_ssid) {
            state.handoff.targetSsid = status.target_ssid;
        }
        if (status && status.local_url) state.handoff.localUrl = status.local_url;
        if (status && status.setup_url) state.handoff.setupUrl = status.setup_url;
        if (status && status.ap_network) state.handoff.apNetwork = status.ap_network;
        if (status && status.message) state.handoff.message = status.message;

        state.summary = summarizeStatus(status || {}, state.handoff);
        state.awaitingHandoff = false;
        if (state.summary.lastOperation && state.visibleErrorSource !== 'action') {
            if (state.summary.lastOperation.success === false) {
                state.visibleError = state.summary.lastOperation.message || 'Wi-Fi operation failed';
                state.visibleErrorSource = 'status';
            } else {
                state.visibleError = '';
                state.visibleErrorSource = '';
            }
        } else if (state.visibleErrorSource === 'status') {
            state.visibleError = '';
            state.visibleErrorSource = '';
        }
        if (state.summary.state === 'normal' && !state.summary.busy) {
            state.actionBannerMessage = '';
            if (!state.summary.lastOperation || state.summary.lastOperation.success !== false) {
                if (state.visibleErrorSource !== 'action') state.visibleError = '';
            }
        }
        renderStatusCard(state, status, state.summary);
        renderActionCard(state, status, state.summary);

        state.lastBusy = state.summary.busy;
        if (options.forceRender || (prevBusy && !state.summary.busy)) {
            state.cacheDirty = true;
        }
        if (prevBusy && !state.summary.busy) {
            scheduleCacheRefresh(state, true);
        }
        if (state.mode === 'setup' || isPollingState(status)) {
            startPolling(state);
        } else if (!state.summary.busy && state.mode !== 'setup') {
            stopPolling(state);
        }
    }

    function scheduleCacheRefresh(state, immediate) {
        state.cacheDirty = true;
        if (immediate) {
            loadCache(state, { reason: 'reconnect' });
        }
    }

    function handleQueuedMutationSuccess(state, result, options) {
        options = options || {};
        var status = result && result.status ? result.status : null;
        var message = result && result.message
            ? result.message
            : (options.fallbackMessage || 'Profile update queued');

        state.actionBannerMessage = message;
        updateVisibleError(state, '');

        if (status) {
            applyStatus(state, status, { forceRender: true, sourcePayload: options.sourcePayload || {} });
        } else {
            loadStatus(state);
            if (options.refreshCache !== false) {
                loadCache(state, { force: true });
            }
        }

        if (options.overlayMessage !== false) {
            setOverlayMessage('success', message);
        }
        if (options.closeOverlay) {
            closeOverlay();
        }

        return status;
    }

    function loadStatus(state) {
        if (state.statusFetchInFlight) return state.statusFetchInFlight;
        state.statusFetchInFlight = getJson(state.config.statusUrl)
            .then(function (payload) {
                state.statusFetchInFlight = null;
                if (state.disposed) return payload;
                var status = payload && payload.status ? payload.status : payload;
                if (payload && payload.message && !status.message) status.message = payload.message;
                applyStatus(state, status, { forceRender: true });
                return payload;
            })
            .catch(function (error) {
                state.statusFetchInFlight = null;
                if (state.disposed) return null;
                updateVisibleError(state, error.message || 'Unable to get Wi-Fi status', 'status');
                if (!state.summary.busy && state.mode !== 'setup') {
                    renderActionCard(state, state.status, state.summary);
                }
                return null;
            });
        return state.statusFetchInFlight;
    }

    function loadCache(state, options) {
        options = options || {};
        if (state.cacheFetchInFlight) return state.cacheFetchInFlight;
        state.cacheFetchInFlight = getJson(state.config.scanUrl)
            .then(function (payload) {
                state.cacheFetchInFlight = null;
                if (state.disposed) return payload;
                if (state.visibleErrorSource === 'cache') state.visibleError = '';
                renderNetworks(state, payload || {});
                if (payload && payload.status) {
                    applyStatus(state, payload.status, { forceRender: !!options.forceRender, sourcePayload: {} });
                }
                if (payload && payload.saved && payload.networks) {
                    var sig = JSON.stringify([payload.saved.length, payload.networks.length]);
                    state.lastCacheSignature = sig;
                }
                return payload;
            })
            .catch(function (error) {
                state.cacheFetchInFlight = null;
                if (state.disposed) return null;
                var list = byId('wifi-network-list');
                if (list) {
                    clearChildren(list);
                    var row = document.createElement('div');
                    row.className = 'wifi-spinner-row';
                    row.style.color = '#a94442';
                    row.textContent = error.message || 'Unable to load cached networks';
                    list.appendChild(row);
                }
                updateVisibleError(state, error.message || 'Unable to load cached networks', 'cache');
                return null;
            });
        return state.cacheFetchInFlight;
    }

    function startPolling(state) {
        if (state.polling || state.disposed || !state.controllerEnabled) return;
        state.polling = true;
        if (state.statusTimer == null) {
            state.statusTimer = window.setInterval(function () {
                if (!document.hidden) loadStatus(state);
            }, STATUS_POLL_MS);
        }
        if (state.cacheTimer == null) {
            state.cacheTimer = window.setInterval(function () {
                if (!document.hidden && (state.mode === 'setup' || state.summary.busy || state.cacheDirty)) {
                    state.cacheDirty = false;
                    loadCache(state);
                }
            }, CACHE_POLL_MS);
        }
        if (state.mode === 'setup' || state.summary.busy) {
            loadStatus(state);
            loadCache(state);
        }
    }

    function stopStatusPolling(state) {
        if (state.statusTimer != null) {
            window.clearInterval(state.statusTimer);
            state.statusTimer = null;
        }
    }

    function stopCachePolling(state) {
        if (state.cacheTimer != null) {
            window.clearInterval(state.cacheTimer);
            state.cacheTimer = null;
        }
    }

    function stopPolling(state) {
        state.polling = false;
        stopStatusPolling(state);
        stopCachePolling(state);
    }

    function connectSavedOrManual(state) {
        if (state.requestInFlight) return Promise.resolve(null);
        var payload;
        try {
            payload = overlayPayload(state);
        } catch (error) {
            setOverlayMessage('error', error.message || 'Invalid input');
            return Promise.resolve(null);
        }
        var isEdit = state.overlayMode === 'edit';
        var url = isEdit ? state.config.updatePasswordUrl : state.config.connectUrl;
        var message = isEdit ? 'Updating password…' : (state.controllerEnabled ? 'Preparing handoff…' : 'Connecting…');
        updateVisibleError(state, '');
        setOverlayMessage('', message);
        state.requestInFlight = true;

        return requestJson(url, state.config.csrfToken, payload).then(function (result) {
            if (isEdit) {
                handleQueuedMutationSuccess(state, result, {
                    fallbackMessage: 'Profile update queued',
                    closeOverlay: true,
                    overlayMessage: true
                });
                return result;
            }

            if (state.controllerEnabled) {
                updateActionFromPrepare(state, payload, result || {});
                state.actionBannerMessage = result.message || 'Join prepared';
                setOverlayMessage('success', result.message || 'Join prepared');
                closeOverlay();
                startPolling(state);
                return result;
            }

            setOverlayMessage('success', result.message || 'Connected');
            closeOverlay();
            loadStatus(state);
            loadCache(state, { force: true });
            return result;
        }).catch(function (error) {
            setOverlayMessage('error', error.message || 'Request failed');
            updateVisibleError(state, error.message || 'Request failed');
            return null;
        }).finally(function () {
            state.requestInFlight = false;
            renderActionCard(state, state.status, state.summary);
        });
    }

    function commitJoin(state) {
        var operationId = state.handoff && state.handoff.operationId ? state.handoff.operationId : null;
        if (!operationId || state.requestInFlight || state.summary.state !== 'join_pending') return Promise.resolve(null);
        state.requestInFlight = true;
        state.awaitingHandoff = true;
        state.actionMode = 'commit';
        state.actionBannerMessage = 'Committing…';
        renderActionCard(state, state.status, state.summary);
        setOverlayMessage('', 'Committing…');
        return requestJson(state.config.commitUrl, state.config.csrfToken, { operation_id: operationId })
            .then(function (result) {
                if (result && result.status) {
                    applyStatus(state, result.status, { forceRender: true });
                } else {
                    loadStatus(state);
                }
                loadCache(state, { force: true });
                state.actionBannerMessage = result.message || 'Join committed';
                setOverlayMessage('success', result.message || 'Commit queued');
                state.actionMode = '';
                renderActionCard(state, state.status, state.summary);
                return result;
            })
            .catch(function (error) {
                var rejected = error.status || error.payload;
                state.awaitingHandoff = !rejected;
                state.actionBannerMessage = rejected
                    ? 'Join request rejected: ' + error.message
                    : 'Connection lost during handoff; the outcome is not yet confirmed. Join '
                        + state.handoff.targetSsid + ' on this device, then open the reconnect URL. '
                        + 'If CTS-Scoreboard returns, rejoin it to see the error.';
                if (rejected) updateVisibleError(state, error.message);
                state.actionMode = '';
                renderActionCard(state, state.status, state.summary);
                return null;
            }).finally(function () {
                state.requestInFlight = false;
                state.actionMode = '';
                renderActionCard(state, state.status, state.summary);
                startPolling(state);
            });
    }

    function cancelJoin(state) {
        var operationId = state.handoff && state.handoff.operationId ? state.handoff.operationId : null;
        if (!operationId) return Promise.resolve(null);
        state.actionBannerMessage = 'Cancelling…';
        setOverlayMessage('', 'Cancelling…');
        return requestJson(state.config.cancelUrl, state.config.csrfToken, { operation_id: operationId })
            .then(function (result) {
                loadStatus(state);
                loadCache(state, { force: true });
                state.actionBannerMessage = result.message || 'Join cancelled';
                setOverlayMessage('success', result.message || 'Cancelled');
                return result;
            })
            .catch(function (error) {
                state.actionBannerMessage = 'Cancel failed';
                setText(state.actionText, 'Cancel failed');
                setOverlayMessage('error', error.message || 'Cancel failed');
                updateVisibleError(state, error.message || 'Cancel failed');
                return null;
            });
    }

    function retrySaved(state) {
        if (state.requestInFlight || state.summary.busy || state.summary.state === 'join_pending') return Promise.resolve(null);
        if (!window.confirm('The setup hotspot will disconnect while saved networks are tried. Rejoin CTS-Scoreboard if none work. Continue?')) {
            return Promise.resolve(null);
        }
        state.actionBannerMessage = 'Retrying saved network…';
        setOverlayMessage('', 'Retrying saved network…');
        return requestJson(state.config.retryUrl, state.config.csrfToken, {})
            .then(function (result) {
                if (result && result.status) {
                    applyStatus(state, result.status, { forceRender: true });
                } else {
                    loadStatus(state);
                }
                loadCache(state, { force: true });
                state.actionBannerMessage = result.message || 'Retry queued';
                setOverlayMessage('success', result.message || 'Retry queued');
                return result;
            })
            .catch(function (error) {
                state.actionBannerMessage = 'Retry failed';
                setText(state.actionText, 'Retry failed');
                setOverlayMessage('error', error.message || 'Retry failed');
                updateVisibleError(state, error.message || 'Retry failed');
                return null;
            });
    }

    function refreshScan(state, confirmed) {
        if (state.controllerEnabled) {
            if (!confirmed && window.confirm) {
                if (!window.confirm('The setup hotspot may disconnect briefly to scan. Rejoin CTS-Scoreboard afterward to see the refreshed list. Continue?')) {
                    return Promise.resolve(null);
                }
            }
            return requestJson(state.config.refreshUrl, state.config.csrfToken, { confirmed: true })
                .then(function (result) {
                    if (result && result.status) applyStatus(state, result.status, { forceRender: true });
                    if (result && result.networks) renderNetworks(state, result);
                    else loadCache(state, { force: true });
                    return result;
                })
                .catch(function (error) {
                    updateVisibleError(state, error.message || 'Refresh failed');
                    return null;
                });
        }
        return loadCache(state, { force: true });
    }

    function forgetNetwork(state, network, profile) {
        if (state.requestInFlight || state.summary.busy || state.summary.state === 'join_pending') return Promise.resolve(null);
        var ssid = network && network.ssid ? network.ssid : '';
        if (!ssid) return Promise.resolve(null);
        if (window.confirm && !window.confirm('Forget ' + ssid + '?')) return Promise.resolve(null);
        state.requestInFlight = true;
        renderActionCard(state, state.status, state.summary);
        return requestJson(state.config.forgetUrl, state.config.csrfToken, {
            ssid: ssid,
            profile_id: profile && profile.id ? profile.id : undefined
        }).then(function (result) {
            handleQueuedMutationSuccess(state, result, {
                fallbackMessage: 'Profile update queued',
                overlayMessage: false
            });
            return result;
        }).catch(function (error) {
            updateVisibleError(state, error.message || 'Forget failed');
            return null;
        }).finally(function () {
            state.requestInFlight = false;
            renderActionCard(state, state.status, state.summary);
        });
    }

    function togglePassword(inputId) {
        var inp = byId(inputId);
        if (!inp) return;
        inp.type = inp.type === 'password' ? 'text' : 'password';
    }

    function init(config) {
        config = config || window.WIFI_CONFIG || {};
        if (typeof document === 'undefined' || !document.body || !byId('wifi-status-card')) return;

        var state = createState(config);
        state.refreshButton = byId('wifi-refresh-btn');
        state.actionRefreshButton = byId('wifi-action-refresh-btn');
        state.hiddenButton = byId('wifi-hidden-btn');
        state.commitButton = byId('wifi-commit-btn');
        state.cancelButton = byId('wifi-cancel-btn');
        state.retryButton = byId('wifi-retry-btn');
        state.overlayAction = byId('wifi-connect-action');
        state.overlayCancel = byId('wifi-connect-cancel');
        state.overlayToggle = byId('wifi-connect-eye');
        state.networkList = byId('wifi-network-list');
        state.expando = byId('wifi-network-expando');
        state.statusCard = byId('wifi-status-card');
        state.statusLine = byId('wifi-status-line');
        state.statusMessage = byId('wifi-status-message');
        state.actionCard = byId('wifi-action-card');
        state.actionTitle = byId('wifi-action-title');
        state.actionText = byId('wifi-action-text');
        state.setupNote = byId('wifi-setup-note');

        renderStatusCard(state, state.status, state.summary);
        renderActionCard(state, state.status, state.summary);
        renderNetworks(state, {
            networks: state.initialNetworks,
            saved: state.initialSaved,
            profiles: state.initialProfiles
        });

        if (state.mode === 'setup') {
            startPolling(state);
        } else {
            loadStatus(state);
        }

        if (state.refreshButton) {
            state.refreshButton.addEventListener('click', function () { refreshScan(state, false); });
        }
        if (state.actionRefreshButton) {
            state.actionRefreshButton.addEventListener('click', function () { refreshScan(state, false); });
        }
        if (state.hiddenButton) {
            state.hiddenButton.addEventListener('click', function () {
                openOverlay(state, { ssid: '', security: 'auto', hidden: true }, null, 'connect');
                var hidden = byId('wifi-connect-hidden');
                var security = byId('wifi-connect-security');
                if (hidden) hidden.checked = true;
                if (security) security.value = 'auto';
            });
        }
        if (state.commitButton) {
            state.commitButton.addEventListener('click', function () { commitJoin(state); });
        }
        if (state.cancelButton) {
            state.cancelButton.addEventListener('click', function () { cancelJoin(state); });
        }
        if (state.retryButton) {
            state.retryButton.addEventListener('click', function () { retrySaved(state); });
        }
        if (state.overlayAction) {
            state.overlayAction.addEventListener('click', function () { connectSavedOrManual(state); });
        }
        if (state.overlayCancel) {
            state.overlayCancel.addEventListener('click', function () { closeOverlay(); });
        }
        if (state.overlayToggle) {
            state.overlayToggle.addEventListener('click', function () { togglePassword('wifi-connect-password'); });
        }

        if (state.expando && state.mode === 'settings') {
            state.expando.addEventListener('toggle', function () {
                if (state.expando.open && !state.expandoScanned) {
                    state.expandoScanned = true;
                    loadCache(state);
                }
            });
        }

        if (window.addEventListener) {
            window.addEventListener('pagehide', function () {
                state.disposed = true;
                stopPolling(state);
            });
        }

        state.loadStatus = function () { return loadStatus(state); };
        state.loadCache = function () { return loadCache(state); };
        state.refreshScan = function (confirmed) { return refreshScan(state, confirmed); };
        state.commitJoin = function () { return commitJoin(state); };
        state.cancelJoin = function () { return cancelJoin(state); };
        state.retrySaved = function () { return retrySaved(state); };
        state.forgetNetwork = function (network, profile) { return forgetNetwork(state, network, profile); };
        state.openOverlay = function (network, profile, mode) { return openOverlay(state, network, profile, mode); };
        state.closeOverlay = closeOverlay;
        state.state = state;

        exports._state = state;
        return state;
    }

    exports.escapeHtml = escapeHtml;
    exports.isBusyState = isBusyState;
    exports.normalizeSecurity = normalizeSecurity;
    exports.createNullMap = createNullMap;
    exports.parseJsonResponse = parseJsonResponse;
    exports.requestJson = requestJson;
    exports.statusSummary = summarizeStatus;
    exports.init = init;

    if (typeof window !== 'undefined') {
        window.WifiProvisioning = exports;
        if (window.WIFI_CONFIG && typeof document !== 'undefined' && document.readyState !== 'loading') {
            init(window.WIFI_CONFIG);
        } else if (window.WIFI_CONFIG && typeof document !== 'undefined') {
            document.addEventListener('DOMContentLoaded', function () { init(window.WIFI_CONFIG); });
        }
    }
})(typeof module !== 'undefined' ? module.exports : window);
