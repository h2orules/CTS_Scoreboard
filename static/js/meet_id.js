/**
 * Friendly meet-ID validation and default-derivation helpers.
 *
 * Mirrors azure_relay.validate_meet_id / derive_meet_id_default so that
 * the Settings UI can validate input live and suggest a default name based
 * on the host team name.
 *
 * Dual-export IIFE: usable as a browser global (`window.MeetId`) and as a
 * Node module for Vitest.
 */

(function (exports) {

    var MIN_LEN = 10;
    var MAX_LEN = 20;
    var REGEX = /^[A-Za-z0-9_-]{10,20}$/;
    var ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

    function validate(name) {
        if (name == null) {
            return { ok: false, error: 'Name is required' };
        }
        var s = String(name);
        if (s.length < MIN_LEN) {
            return { ok: false, error: 'Must be at least ' + MIN_LEN + ' characters' };
        }
        if (s.length > MAX_LEN) {
            return { ok: false, error: 'Must be at most ' + MAX_LEN + ' characters' };
        }
        if (!REGEX.test(s)) {
            return {
                ok: false,
                error: 'Only letters, digits, "-", and "_" are allowed (no spaces)'
            };
        }
        return { ok: true, error: null };
    }

    function _randomChar() {
        // Use crypto.getRandomValues when available (browser / modern Node).
        var idx;
        var g = (typeof globalThis !== 'undefined') ? globalThis : null;
        if (g && g.crypto && typeof g.crypto.getRandomValues === 'function') {
            var buf = new Uint32Array(1);
            g.crypto.getRandomValues(buf);
            idx = buf[0] % ALPHABET.length;
        } else {
            idx = Math.floor(Math.random() * ALPHABET.length);
        }
        return ALPHABET.charAt(idx);
    }

    function _randomSuffix(n) {
        var out = '';
        for (var i = 0; i < n; i++) {
            out += _randomChar();
        }
        return out;
    }

    /**
     * Derive a friendly default name from the host team name.
     *
     * Mirrors derive_meet_id_default in azure_relay.py:
     *   1. Replace whitespace runs with '-'.
     *   2. Strip characters outside [A-Za-z0-9_-].
     *   3. Collapse repeated '-' / '_'.
     *   4. Trim leading/trailing '-' and '_'.
     *   5. Truncate to MAX_LEN.
     *   6. If shorter than MIN_LEN, pad with '-' + random suffix.
     *   7. If empty after sanitization, fall back to a fully random name.
     */
    function deriveDefault(teamHome, randomSuffix) {
        var s = (teamHome == null) ? '' : String(teamHome);
        s = s.replace(/\s+/g, '-');
        s = s.replace(/[^A-Za-z0-9_-]+/g, '');
        s = s.replace(/-{2,}/g, '-').replace(/_{2,}/g, '_');
        s = s.replace(/^[-_]+|[-_]+$/g, '');
        if (s.length === 0) {
            // Fallback: fully random MIN_LEN-char name.
            return _randomSuffix(MIN_LEN);
        }
        if (s.length > MAX_LEN) {
            s = s.substring(0, MAX_LEN);
            s = s.replace(/[-_]+$/g, '');
        }
        if (s.length < MIN_LEN) {
            var need = MIN_LEN - s.length - 1; // -1 for the '-' separator
            if (need < 1) need = 1;
            var suffix;
            if (typeof randomSuffix === 'string' && randomSuffix.length >= need) {
                suffix = randomSuffix.substring(0, need);
            } else {
                suffix = _randomSuffix(need);
            }
            s = s + '-' + suffix;
            if (s.length > MAX_LEN) {
                s = s.substring(0, MAX_LEN);
            }
        }
        return s;
    }

    exports.MeetId = {
        MIN_LEN: MIN_LEN,
        MAX_LEN: MAX_LEN,
        MEET_ID_REGEX: REGEX,
        validate: validate,
        deriveDefault: deriveDefault,
    };

})(typeof module !== 'undefined' ? module.exports : window);
