import { describe, it, expect } from 'vitest';
const kiosk = require('../../static/js/wifi_kiosk.js');

describe('wifi_kiosk helpers', () => {
    it('normalizes display URLs to local scoreboard paths', () => {
        expect(kiosk.normalizeDisplayUrl('/web/home')).toBe('/web/home');
        expect(kiosk.normalizeDisplayUrl('/web/home?event=2')).toBe('/web/home?event=2');
        expect(kiosk.normalizeDisplayUrl('https://example.com')).toBe('/web/home');
        expect(kiosk.normalizeDisplayUrl('/web/kiosk')).toBe('/web/home');
        expect(kiosk.normalizeDisplayUrl('/web/kiosk?display=/web/home')).toBe('/web/home');
    });

    it('detects provisioning states and setup_active', () => {
        expect(kiosk.isProvisioningStatus({ state: 'setup' })).toBe(true);
        expect(kiosk.isProvisioningStatus({ state: 'normal', setup_active: true })).toBe(true);
        expect(kiosk.isProvisioningStatus({ state: 'normal', setup_active: false })).toBe(false);
    });

    it('picks the setup display when provisioning is active', () => {
        const status = { state: 'joining', setup_active: true };
        expect(kiosk.pickFrameUrl(status, '/web/home', '/wifi/display')).toBe('/wifi/display');
    });

    it('builds a concise status label', () => {
        const label = kiosk.buildStatusLabel({
            state: 'normal',
            message: 'Connected',
            wifi_ssid: 'CTS-Scoreboard',
            ipv4: '192.168.1.42',
        });
        expect(label).toContain('normal');
        expect(label).toContain('Connected');
        expect(label).toContain('CTS-Scoreboard');
        expect(label).toContain('192.168.1.42');
    });

    it('updates simple element-like objects', () => {
        const iframe = {
            src: '',
            attrs: {},
            getAttribute(name) { return this.attrs[name] || null; },
            setAttribute(name, value) { this.attrs[name] = value; },
        };
        const target = kiosk.updateShellElements(
            { iframe },
            { state: 'setup', message: 'Join CTS-Scoreboard', setup_active: true },
            '/web/home',
            '/wifi/display'
        );
        expect(target).toBe(true);
        expect(iframe.src).toBe('/wifi/display');
        const second = kiosk.updateShellElements(
            { iframe },
            { state: 'setup', message: 'Still joining', setup_active: true },
            '/web/home',
            '/wifi/display'
        );
        expect(second).toBe(false);
        expect(iframe.src).toBe('/wifi/display');
    });

    it('keeps the scoreboard iframe target stable when status does not change', () => {
        const iframe = {
            src: '',
            attrs: {},
            getAttribute(name) { return this.attrs[name] || null; },
            setAttribute(name, value) { this.attrs[name] = value; },
        };
        kiosk.updateShellElements(
            { iframe },
            { state: 'normal', ipv4: '192.168.1.42' },
            '/web/home?event=2&heat=3',
            '/wifi/display'
        );
        expect(iframe.src).toBe('/web/home?event=2&heat=3');
        iframe.src = 'unchanged';
        iframe.attrs['data-current-url'] = '/web/home?event=2&heat=3';
        const target = kiosk.updateShellElements(
            { iframe },
            { state: 'normal', ipv4: '192.168.1.42' },
            '/web/home?event=2&heat=3',
            '/wifi/display'
        );
        expect(target).toBe(false);
        expect(iframe.src).toBe('unchanged');
    });

    it('refreshes setup instructions only when displayed status changes', () => {
        let loads = 0;
        const iframe = {
            attrs: {},
            set src(value) { loads++; },
            getAttribute(name) { return this.attrs[name] || null; },
            setAttribute(name, value) { this.attrs[name] = value; },
        };
        const elements = { iframe };
        const setup = { state: 'setup', setup_active: true, message: 'Ready' };
        kiosk.updateShellElements(elements, setup, '/web/home', '/wifi/display');
        kiosk.updateShellElements(elements, setup, '/web/home', '/wifi/display');
        expect(loads).toBe(1);
        kiosk.updateShellElements(elements, { ...setup, message: 'Password was incorrect' }, '/web/home', '/wifi/display');
        expect(loads).toBe(2);
        kiosk.updateShellElements(elements, { state: 'normal', ipv4: '10.0.0.2' }, '/web/home', '/wifi/display');
        expect(loads).toBe(3);
        kiosk.updateShellElements(elements, { state: 'normal', ipv4: '10.0.0.3', message: 'Address changed' }, '/web/home', '/wifi/display');
        expect(loads).toBe(3);
    });
});
