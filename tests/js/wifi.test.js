import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
const wifi = require('../../static/js/wifi.js');

class FakeEvent {
    constructor(type, options = {}) {
        this.type = type;
        this.bubbles = !!options.bubbles;
        this.cancelable = !!options.cancelable;
        this.defaultPrevented = false;
        this.target = null;
        this.currentTarget = null;
        this._stopped = false;
    }
    preventDefault() { this.defaultPrevented = true; }
    stopPropagation() { this._stopped = true; }
}

class FakeNode {
    constructor() {
        this.parentNode = null;
        this.childNodes = [];
        this._listeners = Object.create(null);
        this._text = '';
        this._html = '';
    }
    appendChild(node) {
        if (typeof node === 'string') node = new FakeText(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }
    removeChild(node) {
        const idx = this.childNodes.indexOf(node);
        if (idx >= 0) this.childNodes.splice(idx, 1);
        node.parentNode = null;
        return node;
    }
    addEventListener(type, fn) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(fn);
    }
    dispatchEvent(event) {
        if (!(event instanceof FakeEvent)) event = new FakeEvent(event.type || event, event);
        if (!event.target) event.target = this;
        let node = this;
        while (node) {
            event.currentTarget = node;
            const handlers = node._listeners[event.type] || [];
            for (const handler of handlers) {
                handler.call(node, event);
                if (event._stopped) return !event.defaultPrevented;
            }
            if (!event.bubbles) break;
            node = node.parentNode;
        }
        return !event.defaultPrevented;
    }
    get textContent() {
        if (this.childNodes.length) return this.childNodes.map(c => c.textContent).join('');
        return this._text;
    }
    set textContent(value) {
        this._text = String(value == null ? '' : value);
        this._html = '';
        this.childNodes = [];
    }
    get innerHTML() { return this._html; }
    set innerHTML(value) {
        this._html = String(value == null ? '' : value);
        this._text = '';
        this.childNodes = [];
    }
    get firstChild() { return this.childNodes[0] || null; }
    closest(selector) {
        let node = this;
        while (node) {
            if (node instanceof FakeElement && node.matches(selector)) return node;
            node = node.parentNode;
        }
        return null;
    }
}

class FakeText extends FakeNode {
    constructor(text) {
        super();
        this._text = String(text == null ? '' : text);
    }
    appendChild() { throw new Error('Cannot append to text node'); }
    set textContent(value) { this._text = String(value == null ? '' : value); }
    get textContent() { return this._text; }
}

class FakeClassList {
    constructor(el) {
        this.el = el;
        this.set = new Set();
    }
    add(...classes) { classes.forEach(c => this.set.add(c)); this._sync(); }
    remove(...classes) { classes.forEach(c => this.set.delete(c)); this._sync(); }
    contains(cls) { return this.set.has(cls); }
    _sync() { this.el.className = Array.from(this.set).join(' '); }
}

class FakeElement extends FakeNode {
    constructor(tagName) {
        super();
        this.tagName = tagName.toUpperCase();
        this._className = '';
        this.classList = new FakeClassList(this);
        this.style = {};
        this.dataset = Object.create(null);
        this.attributes = Object.create(null);
        this.value = '';
        this.checked = false;
        this.type = '';
        this.disabled = false;
        this.id = '';
        this.open = false;
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'id') this.id = String(value);
        if (name === 'class') {
            this._className = String(value);
            this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean));
        }
    }
    set className(value) {
        this._className = String(value == null ? '' : value);
        this.classList.set = new Set(this._className.split(/\s+/).filter(Boolean));
    }
    get className() {
        return this._className;
    }
    getAttribute(name) {
        return this.attributes[name] || null;
    }
    matches(selector) {
        if (!selector) return false;
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        return this.tagName === selector.toUpperCase();
    }
    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }
    querySelectorAll(selector) {
        const results = [];
        const walk = (node) => {
            if (!(node instanceof FakeElement)) return;
            if (node.matches(selector)) results.push(node);
            node.childNodes.forEach(walk);
        };
        this.childNodes.forEach(walk);
        return results;
    }
    focus() {}
    click() { this.dispatchEvent(new FakeEvent('click', { bubbles: true })); }
}

class FakeDocument extends FakeElement {
    constructor() {
        super('#document');
        this.body = new FakeElement('body');
        this.body.parentNode = this;
    }
    createElement(tagName) { return new FakeElement(tagName); }
    createTextNode(text) { return new FakeText(text); }
    getElementById(id) {
        const walk = (node) => {
            if (!(node instanceof FakeElement)) return null;
            if (node.id === id) return node;
            for (const child of node.childNodes) {
                const found = walk(child);
                if (found) return found;
            }
            return null;
        };
        return walk(this.body);
    }
    querySelector(selector) { return this.body.querySelector(selector); }
    querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
}

class FakeWindow {
    constructor(document) {
        this.document = document;
        this._listeners = Object.create(null);
        this.WIFI_CONFIG = null;
        this.confirm = vi.fn(() => true);
        this.Event = FakeEvent;
        this.CustomEvent = FakeEvent;
        this.Node = FakeNode;
        this.HTMLElement = FakeElement;
        this.Headers = class {
            constructor(map = {}) { this.map = map; }
            get(name) { return this.map[String(name).toLowerCase()] || this.map[name] || null; }
        };
        this.setTimeout = setTimeout;
        this.clearTimeout = clearTimeout;
        this.setInterval = setInterval;
        this.clearInterval = clearInterval;
        this.fetch = global.fetch;
        this.pageXOffset = 0;
        this.pageYOffset = 0;
        this.location = { href: 'http://example.test/' };
    }
    addEventListener(type, fn) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(fn);
    }
    dispatchEvent(event) {
        const handlers = this._listeners[event.type] || [];
        handlers.forEach(fn => fn.call(this, event));
    }
}

function makeResponse(body, options = {}) {
    const text = typeof body === 'string' ? body : JSON.stringify(body);
    return {
        ok: options.ok !== undefined ? options.ok : true,
        status: options.status || 200,
        url: options.url || 'http://example.test/wifi/status',
        headers: { get: (name) => (name.toLowerCase() === 'content-type' ? (options.contentType || 'application/json') : null) },
        text: async () => text,
    };
}

function tick() {
    return Promise.resolve().then(() => Promise.resolve());
}

function installDom(mode = 'setup', config = {}) {
    const document = new FakeDocument();
    const body = document.body;
    const append = (el) => { body.appendChild(el); return el; };
    const el = (tag, id, parent = body) => {
        const node = document.createElement(tag);
        if (id) node.id = id;
        parent.appendChild(node);
        return node;
    };

    const statusCard = append(el('div', 'wifi-status-card'));
    el('div', 'wifi-status-line', statusCard);
    el('div', 'wifi-status-message', statusCard);

    const actionCard = append(el('div', 'wifi-action-card'));
    actionCard.classList.add('hidden');
    el('div', 'wifi-action-title', actionCard);
    el('div', 'wifi-action-text', actionCard);
    el('div', 'wifi-setup-note', actionCard);
    const actionRow = el('div', null, actionCard);
    el('button', 'wifi-commit-btn', actionRow).textContent = 'Commit';
    el('button', 'wifi-cancel-btn', actionRow).textContent = 'Cancel';
    el('button', 'wifi-action-refresh-btn', actionRow).textContent = 'Refresh';
    el('button', 'wifi-retry-btn', actionRow).textContent = 'Retry';

    const expando = append(el('details', 'wifi-network-expando'));
    el('summary', null, expando).textContent = 'Networks';
    el('button', 'wifi-refresh-btn').textContent = 'Refresh';
    el('button', 'wifi-hidden-btn').textContent = 'Hidden';
    el('div', 'wifi-network-list');

    const overlay = append(el('div', 'wifi-connect-overlay'));
    overlay.style.display = 'none';
    const overlayCard = el('div', null, overlay);
    el('h4', 'wifi-connect-title', overlayCard);
    el('div', 'wifi-connect-msg', overlayCard);
    el('input', 'wifi-connect-ssid', overlayCard);
    const password = el('input', 'wifi-connect-password', overlayCard);
    password.type = 'password';
    const security = el('select', 'wifi-connect-security', overlayCard);
    ['auto', 'open', 'wpa2', 'wpa3'].forEach(value => {
        const option = el('option', null, security);
        option.value = value;
        option.textContent = value;
    });
    el('input', 'wifi-connect-hidden', overlayCard).type = 'checkbox';
    el('input', 'wifi-connect-profile-id', overlayCard).type = 'hidden';
    el('button', 'wifi-connect-action', overlayCard).textContent = 'Connect';
    el('button', 'wifi-connect-cancel', overlayCard).textContent = 'Cancel';
    el('button', 'wifi-connect-eye', overlayCard).textContent = 'Eye';

    const fakeWindow = new FakeWindow(document);
    global.window = fakeWindow;
    global.document = document;
    global.Headers = fakeWindow.Headers;
    global.Node = fakeWindow.Node;
    global.Event = fakeWindow.Event;
    global.CustomEvent = fakeWindow.CustomEvent;
    global.HTMLElement = fakeWindow.HTMLElement;
    global.navigator = { userAgent: 'fake' };
    global.confirm = fakeWindow.confirm;
    global.fetch = vi.fn().mockImplementation((url) => {
        if (String(url).includes('/wifi/status')) {
            return Promise.resolve(makeResponse({
                success: true,
                status: config.initialStatus || {},
            }));
        }
        if (String(url).includes('/wifi/scan')) {
            return Promise.resolve(makeResponse({
                success: true,
                networks: config.initialNetworks || [],
                saved: config.initialSaved || [],
                profiles: config.initialProfiles || [],
                status: config.initialStatus || {},
            }));
        }
        return Promise.resolve(makeResponse({
            success: true,
            networks: [],
            saved: [],
            profiles: [],
        }));
    });
    global.window.fetch = global.fetch;
    global.window.confirm = global.confirm;
    global.window.setTimeout = setTimeout;
    global.window.clearTimeout = clearTimeout;
    global.window.setInterval = setInterval;
    global.window.clearInterval = clearInterval;
    global.window.Event = FakeEvent;
    global.window.CustomEvent = FakeEvent;
    global.window.Headers = fakeWindow.Headers;
    global.window.Node = FakeNode;
    global.window.HTMLElement = FakeElement;
    global.window.navigator = global.navigator;
    global.window.dispatchEvent = fakeWindow.dispatchEvent.bind(fakeWindow);
    global.window.addEventListener = fakeWindow.addEventListener.bind(fakeWindow);
    global.window.location = fakeWindow.location;
    global.window.document = document;
    global.window.WIFI_CONFIG = {
        mode,
        controllerEnabled: true,
        csrfToken: 'csrf-token',
        statusUrl: '/wifi/status',
        scanUrl: '/wifi/scan',
        connectUrl: '/wifi/connect',
        forgetUrl: '/wifi/forget',
        updatePasswordUrl: '/wifi/update_password',
        prepareUrl: '/wifi/prepare',
        commitUrl: '/wifi/commit',
        cancelUrl: '/wifi/cancel',
        refreshUrl: '/wifi/refresh',
        retryUrl: '/wifi/retry',
        setupUrl: '/wifi/setup',
        initialStatus: config.initialStatus || {},
        initialNetworks: config.initialNetworks || [],
        initialSaved: config.initialSaved || [],
        initialProfiles: config.initialProfiles || [],
    };
    return document;
}

function cleanupDom() {
    delete global.window;
    delete global.document;
    delete global.Headers;
    delete global.Node;
    delete global.Event;
    delete global.CustomEvent;
    delete global.HTMLElement;
    delete global.navigator;
    delete global.confirm;
    delete global.fetch;
}

beforeEach(() => {
    vi.useFakeTimers();
    global.fetch = vi.fn().mockResolvedValue(makeResponse({
        success: true,
        networks: [],
        saved: [],
        profiles: [],
    }));
});

afterEach(() => {
    vi.useRealTimers();
    cleanupDom();
});

describe('pure helpers', () => {
    it('normalizes security values', () => {
        expect(wifi.normalizeSecurity('')).toBe('open');
        expect(wifi.normalizeSecurity('WPA2')).toBe('wpa2');
        expect(wifi.normalizeSecurity('WPA2 WPA3')).toBe('wpa2');
        expect(wifi.normalizeSecurity('WPA3')).toBe('wpa3');
        expect(wifi.normalizeSecurity('Something Weird')).toBe('auto');
    });

    it('uses null-prototype maps safely', () => {
        const map = wifi.createNullMap();
        map.__proto__ = true;
        map.constructor = true;
        expect(map.__proto__).toBe(true);
        expect(map.constructor).toBe(true);
    });

    it('escapes HTML-sensitive characters', () => {
        expect(wifi.escapeHtml('<Home & "Wi-Fi">')).toBe('&lt;Home &amp; &quot;Wi-Fi&quot;&gt;');
    });

    it('treats malformed JSON with JSON content-type as an error', async () => {
        await expect(wifi.parseJsonResponse(makeResponse('not json', {
            contentType: 'application/json',
        }))).rejects.toThrow('Malformed JSON response');
    });

    it('rejects success:false payloads', async () => {
        global.fetch.mockResolvedValueOnce(makeResponse({ success: false, message: 'Denied' }));
        await expect(wifi.requestJson('/wifi/forget', 'csrf-token', { ssid: 'Home' })).rejects.toThrow('Denied');
        expect(global.fetch).toHaveBeenCalledTimes(1);
        const [url, opts] = global.fetch.mock.calls[0];
        expect(url).toBe('/wifi/forget');
        expect(opts.headers['X-CSRF-Token']).toBe('csrf-token');
    });

    it('rejects HTML, non-object JSON and missing operation success', async () => {
        await expect(wifi.parseJsonResponse(makeResponse('<h1>Server error</h1>', {
            contentType: 'text/html',
        }))).rejects.toThrow('Expected JSON');
        await expect(wifi.parseJsonResponse(makeResponse('null'))).rejects.toThrow('Invalid Wi-Fi response');
        global.fetch.mockResolvedValueOnce(makeResponse({}));
        await expect(wifi.requestJson('/wifi/commit', 'token', {})).rejects.toThrow('Invalid Wi-Fi operation');
    });
});

describe('setup orchestration', () => {
    it('renders the prepared target before commit and preserves reconnect URLs', async () => {
        installDom('setup', {
            initialStatus: {
                state: 'join_pending',
                operation_id: 'op-1',
                target_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Review and commit.',
            },
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'join_pending',
                    operation_id: 'op-1',
                    target_ssid: 'Campus WiFi',
                    local_url: 'http://pool.local:5000/',
                    setup_url: 'http://192.168.4.1:5000/wifi/setup',
                    ap_network: '192.168.4.0/24',
                    message: 'Review and commit.',
                }
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [],
                saved: [],
                profiles: [],
                status: {
                    state: 'join_pending',
                    operation_id: 'op-1',
                    target_ssid: 'Campus WiFi',
                    local_url: 'http://pool.local:5000/',
                    setup_url: 'http://192.168.4.1:5000/wifi/setup',
                    ap_network: '192.168.4.0/24',
                    message: 'Review and commit.',
                }
            }));

        wifi.init(window.WIFI_CONFIG);
        await tick();

        expect(document.getElementById('wifi-action-title').textContent).toContain('Join handoff');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('Campus WiFi');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('192.168.4.1:5000/wifi/setup');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('pool.local:5000');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('192.168.4.0/24');
        expect(document.getElementById('wifi-commit-btn').disabled).toBe(false);
        expect(document.getElementById('wifi-cancel-btn').disabled).toBe(false);
        expect(document.getElementById('wifi-retry-btn').style.display).not.toBe('none');
        expect(document.getElementById('wifi-action-card').classList.contains('hidden')).toBe(false);
    });

    it('keeps the target and fallback instructions when the commit response is lost', async () => {
        installDom('setup', {
            initialStatus: {
                state: 'join_pending', operation_id: 'join-1',
                target_ssid: 'Venue LAN', local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
            },
        });
        const state = wifi.init(window.WIFI_CONFIG);
        await tick();
        global.fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
        await state.commitJoin();
        const text = document.getElementById('wifi-action-text').textContent;
        expect(text).toContain('outcome is not yet confirmed');
        expect(text).not.toContain('Commit failed');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('Venue LAN');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('pool.local:5000');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('192.168.4.1');
        expect(document.getElementById('wifi-commit-btn').disabled).toBe(true);
    });

    it('offers a warned retry from first-boot setup without a pending join', async () => {
        installDom('setup', { initialStatus: { state: 'setup', setup_active: true } });
        const state = wifi.init(window.WIFI_CONFIG);
        await tick();
        expect(document.getElementById('wifi-retry-btn').style.display).not.toBe('none');
        expect(document.getElementById('wifi-retry-btn').disabled).toBe(false);
        expect(document.getElementById('wifi-commit-btn').style.display).toBe('none');
        window.confirm.mockReturnValueOnce(false);
        await state.retrySaved();
        expect(window.confirm).toHaveBeenCalled();
        expect(global.fetch.mock.calls.some(([url]) => url === '/wifi/retry')).toBe(false);
    });

    it('does not restart polling when an in-flight response arrives after pagehide', async () => {
        installDom('setup', { initialStatus: { state: 'setup', setup_active: true } });
        wifi.init(window.WIFI_CONFIG);
        window.dispatchEvent(new FakeEvent('pagehide'));
        await tick();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('keeps handoff instructions visible after a transient status failure', async () => {
        installDom('setup', {
            initialStatus: {
                state: 'joining',
                operation_id: 'op-2',
                target_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Joining…',
            },
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'joining',
                    operation_id: 'op-2',
                    target_ssid: 'Campus WiFi',
                    local_url: 'http://pool.local:5000/',
                    setup_url: 'http://192.168.4.1:5000/wifi/setup',
                    ap_network: '192.168.4.0/24',
                    message: 'Joining…',
                }
            }))
            .mockRejectedValueOnce(new Error('socket unavailable'));

        wifi.init(window.WIFI_CONFIG);
        await tick();
        await vi.advanceTimersByTimeAsync(2000);
        await tick();

        expect(document.getElementById('wifi-action-title').textContent).toContain('Joining');
        expect(document.getElementById('wifi-setup-note').textContent).toContain('Campus WiFi');
        expect(document.getElementById('wifi-status-message').textContent).toContain('socket unavailable');
    });

    it('refreshes the cached list after reconnect without issuing a POST refresh automatically', async () => {
        installDom('setup', {
            initialStatus: {
                state: 'joining',
                operation_id: 'op-3',
                target_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Joining…',
            },
        });
        const statusJoining = {
            success: true,
            status: {
                state: 'joining',
                operation_id: 'op-3',
                target_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Joining…',
            }
        };
        const statusNormal = {
            success: true,
            status: {
                state: 'normal',
                wifi_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Connected.',
            }
        };
        const cacheUpdated = {
            success: true,
            networks: [
                { ssid: 'Campus WiFi', signal: 91, security: 'WPA2 WPA3', in_use: true },
                { ssid: 'Guest', signal: 44, security: '', in_use: false }
            ],
            saved: ['Campus WiFi'],
            profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Campus WiFi' }],
            status: statusNormal.status,
        };
        global.fetch
            .mockResolvedValueOnce(makeResponse(statusJoining))
            .mockResolvedValueOnce(makeResponse({ success: true, networks: [], saved: [], profiles: [], status: statusJoining.status }))
            .mockResolvedValueOnce(makeResponse(statusNormal))
            .mockResolvedValueOnce(makeResponse(cacheUpdated));

        wifi.init(window.WIFI_CONFIG);
        await tick();
        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        await vi.advanceTimersByTimeAsync(20);
        await tick();

        const calls = global.fetch.mock.calls.map(([url, opts]) => ({ url, method: (opts && opts.method) || 'GET' }));
        expect(calls.some(call => call.url === '/wifi/refresh' && call.method === 'POST')).toBe(false);
        expect(document.getElementById('wifi-network-list').textContent).toContain('Guest');
        expect(document.getElementById('wifi-network-list').textContent).toContain('Campus WiFi');
    });
});

describe('interactive DOM behavior', () => {
    it('does not let an older successful job erase a newer request failure', async () => {
        const status = {
            state: 'normal', wifi_ssid: 'Venue',
            last_operation: { id: 'old', success: true, message: 'Profile updated' }
        };
        installDom('settings', { initialStatus: status });
        global.fetch.mockImplementation((url) => Promise.resolve(makeResponse(
            url === '/wifi/forget'
                ? { success: false, message: 'Cannot forget this profile' }
                : { success: true, status }
        )));
        const state = wifi.init(window.WIFI_CONFIG);
        await tick();
        await state.forgetNetwork({ ssid: 'Venue' }, { id: 'profile' });
        await state.loadStatus();
        expect(document.getElementById('wifi-status-message').textContent).toContain('Cannot forget this profile');
    });

    it('does not replace a new handoff with an older operation outcome', () => {
        const summary = wifi.statusSummary({
            state: 'join_pending', target_ssid: 'New venue', message: 'Review this join',
            last_operation: { id: 'old', success: true, message: 'Old job succeeded' }
        });
        expect(summary.title).toBe('Preparing join for New venue');
        expect(summary.message).toBe('Review this join');
    });

    it('allows another action after a failed join has restored the hotspot', () => {
        const summary = wifi.statusSummary({
            state: 'error', setup_active: true, operation_id: null,
            last_operation: { id: 'join-failed', success: false, message: 'Join failed: wrong password' }
        });
        expect(summary.busy).toBe(false);
        expect(summary.kind).toBe('error');
        expect(summary.message).toContain('wrong password');
    });

    it('loads legacy Settings status without automatically scanning', async () => {
        installDom('settings');
        window.WIFI_CONFIG.controllerEnabled = false;
        wifi.init(window.WIFI_CONFIG);
        await tick();
        expect(global.fetch.mock.calls.map(([url]) => url)).toEqual(['/wifi/status']);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('can restart Settings polling for another operation after the first finishes', async () => {
        installDom('settings', { initialStatus: { state: 'normal' } });
        let current = { state: 'normal' };
        global.fetch.mockImplementation((url) => Promise.resolve(makeResponse(
            url === '/wifi/scan'
                ? { success: true, networks: [], saved: [], profiles: [], status: current }
                : { success: true, message: '', status: current }
        )));
        const state = wifi.init(window.WIFI_CONFIG);
        await tick();
        current = { state: 'refreshing', operation_id: 'refresh-1' };
        await state.refreshScan(true);
        await tick();
        expect(vi.getTimerCount()).toBeGreaterThan(0);
        current = { state: 'normal' };
        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        expect(vi.getTimerCount()).toBe(0);
        current = { state: 'refreshing', operation_id: 'refresh-2' };
        await state.refreshScan(true);
        await tick();
        expect(vi.getTimerCount()).toBeGreaterThan(0);
    });
    it('opens saved networks with the actual profile UUID and normalized security', async () => {
        installDom('settings', {
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        wifi.init(window.WIFI_CONFIG);
        const row = document.querySelector('.wifi-network-row');
        row.dispatchEvent(new window.Event('click', { bubbles: true }));
        expect(document.getElementById('wifi-connect-profile-id').value).toBe('123e4567-e89b-12d3-a456-426614174000');
        expect(document.getElementById('wifi-connect-security').value).toBe('wpa2');
    });

    it('shows forget failures in the visible status card', async () => {
        installDom('settings', {
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        wifi.init(window.WIFI_CONFIG);
        await tick();
        global.fetch.mockResolvedValueOnce(makeResponse({ success: false, message: 'Forbidden' }));
        const forgetButton = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Forget');
        forgetButton.dispatchEvent(new window.Event('click', { bubbles: true }));
        await vi.runAllTimersAsync();
        await tick();
        expect(document.getElementById('wifi-status-message').textContent).toContain('Forbidden');
    });

    it('keeps queued forget operations busy until the cache refreshes', async () => {
        installDom('settings', {
            initialStatus: { state: 'normal', setup_active: true },
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({ success: true, status: { state: 'normal', setup_active: true } }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                message: 'Profile update queued',
                operation_id: 'forget-1',
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'forget-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'forget-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'normal',
                    setup_active: true,
                    message: 'Saved network removed',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Guest', signal: 44, security: '', in_use: false }
                ],
                saved: [],
                profiles: [],
            }));

        wifi.init(window.WIFI_CONFIG);
        await tick();

        const forgetButton = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Forget');
        forgetButton.dispatchEvent(new window.Event('click', { bubbles: true }));
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Profile update queued');
        expect(document.getElementById('wifi-refresh-btn').disabled).toBe(true);
        expect(document.getElementById('wifi-action-card').classList.contains('hidden')).toBe(false);

        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Saved network removed');
        expect(document.getElementById('wifi-network-list').textContent).toContain('Guest');
        expect(document.getElementById('wifi-network-list').textContent).not.toContain('Saved Net');
        expect(document.getElementById('wifi-refresh-btn').disabled).toBe(false);
        expect(global.fetch.mock.calls.filter(([url, opts]) => url === '/wifi/refresh' && opts && opts.method === 'POST')).toHaveLength(0);
    });

    it('keeps edit password operations queued until the worker reports completion', async () => {
        installDom('settings', {
            initialStatus: { state: 'normal', setup_active: true },
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({ success: true, status: { state: 'normal', setup_active: true } }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                message: 'Profile update queued',
                operation_id: 'update-1',
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'update-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'update-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'normal',
                    setup_active: true,
                    message: 'Password updated',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }));

        wifi.init(window.WIFI_CONFIG);
        await tick();

        const editButton = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Edit');
        editButton.dispatchEvent(new window.Event('click', { bubbles: true }));
        document.getElementById('wifi-connect-password').value = 'new-secret';
        document.getElementById('wifi-connect-action').dispatchEvent(new window.Event('click', { bubbles: true }));
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Profile update queued');
        expect(document.getElementById('wifi-connect-overlay').style.display).toBe('none');
        expect(document.getElementById('wifi-refresh-btn').disabled).toBe(true);

        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Password updated');
        expect(document.getElementById('wifi-refresh-btn').disabled).toBe(false);
        expect(global.fetch.mock.calls.filter(([url, opts]) => url === '/wifi/refresh' && opts && opts.method === 'POST')).toHaveLength(0);
    });

    it('retains a failed forget outcome after the worker returns to normal', async () => {
        installDom('settings', {
            initialStatus: { state: 'normal', setup_active: true },
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({ success: true, status: { state: 'normal', setup_active: true } }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                message: 'Profile update queued',
                operation_id: 'forget-fail-1',
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'forget-fail-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'forget-fail-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'normal',
                    setup_active: true,
                    message: 'Connected',
                    last_operation: {
                        id: 'forget-fail-1',
                        success: false,
                        message: 'Could not forget Saved Net',
                    },
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }));

        wifi.init(window.WIFI_CONFIG);
        await tick();

        const forgetButton = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Forget');
        forgetButton.dispatchEvent(new window.Event('click', { bubbles: true }));
        await tick();
        await tick();

        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Could not forget Saved Net');
        expect(document.getElementById('wifi-status-message').textContent).not.toContain('Connected');
        expect(document.getElementById('wifi-network-list').textContent).toContain('Saved Net');
    });

    it('retains a failed password update outcome after the worker returns to normal', async () => {
        installDom('settings', {
            initialStatus: { state: 'normal', setup_active: true },
            initialNetworks: [
                { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
            ],
            initialSaved: ['Saved Net'],
            initialProfiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
        });
        global.fetch
            .mockResolvedValueOnce(makeResponse({ success: true, status: { state: 'normal', setup_active: true } }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                message: 'Profile update queued',
                operation_id: 'update-fail-1',
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'update-fail-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'refreshing',
                    setup_active: true,
                    operation_id: 'update-fail-1',
                    message: 'Profile update queued',
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                status: {
                    state: 'normal',
                    setup_active: true,
                    message: 'Connected',
                    last_operation: {
                        id: 'update-fail-1',
                        success: false,
                        message: 'Password update failed',
                    },
                },
            }))
            .mockResolvedValueOnce(makeResponse({
                success: true,
                networks: [
                    { ssid: 'Saved Net', signal: 70, security: 'WPA2 WPA3', in_use: false, profile_id: '123e4567-e89b-12d3-a456-426614174000' }
                ],
                saved: ['Saved Net'],
                profiles: [{ id: '123e4567-e89b-12d3-a456-426614174000', ssid: 'Saved Net' }],
            }));

        wifi.init(window.WIFI_CONFIG);
        await tick();

        const editButton = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Edit');
        editButton.dispatchEvent(new window.Event('click', { bubbles: true }));
        document.getElementById('wifi-connect-password').value = 'new-secret';
        document.getElementById('wifi-connect-action').dispatchEvent(new window.Event('click', { bubbles: true }));
        await tick();
        await tick();

        await vi.advanceTimersByTimeAsync(2000);
        await tick();
        await tick();

        expect(document.getElementById('wifi-status-message').textContent).toContain('Password update failed');
        expect(document.getElementById('wifi-status-message').textContent).not.toContain('Connected');
    });

    it('hides commit/cancel and disables retry during a busy operation', async () => {
        installDom('setup', {
            initialStatus: {
                state: 'refreshing',
                operation_id: 'op-4',
                target_ssid: 'Campus WiFi',
                local_url: 'http://pool.local:5000/',
                setup_url: 'http://192.168.4.1:5000/wifi/setup',
                ap_network: '192.168.4.0/24',
                message: 'Refreshing…',
            },
        });
        global.fetch.mockResolvedValueOnce(makeResponse({ success: true, status: {
            state: 'refreshing',
            operation_id: 'op-4',
            target_ssid: 'Campus WiFi',
            local_url: 'http://pool.local:5000/',
            setup_url: 'http://192.168.4.1:5000/wifi/setup',
            ap_network: '192.168.4.0/24',
            message: 'Refreshing…',
        }}));
        wifi.init(window.WIFI_CONFIG);
        await tick();
        expect(document.getElementById('wifi-commit-btn').style.display).toBe('none');
        expect(document.getElementById('wifi-cancel-btn').style.display).toBe('none');
        expect(document.getElementById('wifi-commit-btn').disabled).toBe(true);
        expect(document.getElementById('wifi-cancel-btn').disabled).toBe(true);
        expect(document.getElementById('wifi-retry-btn').style.display).not.toBe('none');
        expect(document.getElementById('wifi-retry-btn').disabled).toBe(true);
    });
});
