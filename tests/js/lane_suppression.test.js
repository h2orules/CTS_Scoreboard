import { describe, it, expect } from 'vitest';
const sb = require('../../static/js/scoreboard.js');

describe('isServerOwnedLaneDisplay', () => {
    it('is true for "server" and falsy modes', () => {
        expect(sb.isServerOwnedLaneDisplay('server')).toBe(true);
        expect(sb.isServerOwnedLaneDisplay('')).toBe(true);
        expect(sb.isServerOwnedLaneDisplay(null)).toBe(true);
        expect(sb.isServerOwnedLaneDisplay(undefined)).toBe(true);
    });

    it('is false for client-owned modes', () => {
        expect(sb.isServerOwnedLaneDisplay('seed_times')).toBe(false);
        expect(sb.isServerOwnedLaneDisplay('clear')).toBe(false);
    });
});

describe('shouldSuppressLaneField', () => {
    const laneCells = ['lane_time1', 'lane_time10', 'lane_place1', 'lane_place6'];
    const other = ['lane_name1', 'lane_team1', 'lane_running1', 'lane_seed_time1',
        'score_home', 'event_name', 'lane_time', 'lane_timer1'];

    it('suppresses lane cells when mode is seed_times', () => {
        for (const k of laneCells) {
            expect(sb.shouldSuppressLaneField('seed_times', k)).toBe(true);
        }
    });

    it('suppresses lane cells when mode is clear', () => {
        for (const k of laneCells) {
            expect(sb.shouldSuppressLaneField('clear', k)).toBe(true);
        }
    });

    it('does not suppress lane cells when mode is server', () => {
        for (const k of laneCells) {
            expect(sb.shouldSuppressLaneField('server', k)).toBe(false);
            expect(sb.shouldSuppressLaneField(null, k)).toBe(false);
            expect(sb.shouldSuppressLaneField(undefined, k)).toBe(false);
        }
    });

    it('never suppresses non-lane fields regardless of mode', () => {
        for (const mode of ['server', 'seed_times', 'clear', null]) {
            for (const k of other) {
                expect(sb.shouldSuppressLaneField(mode, k)).toBe(false);
            }
        }
    });
});
