import { describe, it, expect } from 'vitest';
import { MeetId } from '../../static/js/meet_id.js';

describe('MeetId.validate', () => {
    const valid = [
        'abcdefghij',          // exactly MIN_LEN, all letters
        'Midlakes-2026',
        'aaaaaaaaaaaaaaaaaaaa', // exactly MAX_LEN
        'A_B_C_D_E_',
        '0123456789',
        'foo-bar-baz_2026',
    ];
    for (const name of valid) {
        it(`accepts ${JSON.stringify(name)}`, () => {
            const r = MeetId.validate(name);
            expect(r.ok).toBe(true);
            expect(r.error).toBe(null);
        });
    }

    const invalid = [
        ['', 'too short'],
        ['short', 'too short'],
        ['abcdefghi', 'one under min'],
        ['a'.repeat(21), 'too long'],
        ['has spaces1', 'space'],
        ['bad!chars1', 'bang'],
        ['dot.name12', 'dot'],
        ['slash/name', 'slash'],
        ['emoji😀12345', 'emoji'],
    ];
    for (const [name, desc] of invalid) {
        it(`rejects ${desc}: ${JSON.stringify(name)}`, () => {
            const r = MeetId.validate(name);
            expect(r.ok).toBe(false);
            expect(typeof r.error).toBe('string');
            expect(r.error.length).toBeGreaterThan(0);
        });
    }

    it('rejects null/undefined gracefully', () => {
        expect(MeetId.validate(null).ok).toBe(false);
        expect(MeetId.validate(undefined).ok).toBe(false);
    });
});

describe('MeetId.deriveDefault', () => {
    it('replaces whitespace with hyphen', () => {
        const out = MeetId.deriveDefault('Midlakes Swim Team', 'XXXX');
        expect(out).toBe('Midlakes-Swim-Team');
        expect(MeetId.validate(out).ok).toBe(true);
    });

    it('strips disallowed characters', () => {
        const out = MeetId.deriveDefault("O'Conner's, Inc.!", 'XXXX');
        // Apostrophes/commas/periods stripped, "OConners-Inc" remains.
        expect(/^[A-Za-z0-9_-]+$/.test(out)).toBe(true);
        expect(out.includes("'")).toBe(false);
    });

    it('collapses repeated separators', () => {
        const out = MeetId.deriveDefault('Foo  Bar', 'XX');
        expect(out.includes('--')).toBe(false);
    });

    it('pads short names to at least MIN_LEN', () => {
        const out = MeetId.deriveDefault('Foo', 'AAAAAA');
        expect(out.length).toBeGreaterThanOrEqual(MeetId.MIN_LEN);
        expect(out.startsWith('Foo-')).toBe(true);
    });

    it('truncates long names to at most MAX_LEN', () => {
        const out = MeetId.deriveDefault('A'.repeat(50), 'XXXX');
        expect(out.length).toBeLessThanOrEqual(MeetId.MAX_LEN);
    });

    it('falls back to a random valid name when input is empty', () => {
        const out = MeetId.deriveDefault('', 'unused');
        expect(MeetId.validate(out).ok).toBe(true);
    });

    it('falls back when input is only disallowed characters', () => {
        const out = MeetId.deriveDefault('!!! ###', 'unused');
        expect(MeetId.validate(out).ok).toBe(true);
    });

    it('produces a valid name for typical team names', () => {
        const samples = ['Midlakes', 'XYZ Aquatics', 'Sharks', 'BSC',
                         'Northeast Tigers', 'a', 'AB'];
        for (const s of samples) {
            const out = MeetId.deriveDefault(s, 'ZZZZZZ');
            expect(MeetId.validate(out).ok).toBe(true);
        }
    });
});
