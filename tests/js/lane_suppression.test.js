import { describe, it, expect } from 'vitest';
const sb = require('../../static/js/scoreboard.js');

describe('shouldSuppressLaneField', () => {
    const suppressedStates = ['PreRace', 'ClearPreRace', 'BlankPreRace', 'TotalBlankPreRace', 'Clear'];
    const passthroughStates = ['Running', 'Finished', 'Blank', 'TotalBlank', 'Unknown', '', null, undefined];

    it('suppresses lane_time<N> in *PreRace and Clear states', () => {
        for (const state of suppressedStates) {
            expect(sb.shouldSuppressLaneField(state, 'lane_time1')).toBe(true);
            expect(sb.shouldSuppressLaneField(state, 'lane_time10')).toBe(true);
        }
    });

    it('suppresses lane_place<N> in *PreRace and Clear states', () => {
        for (const state of suppressedStates) {
            expect(sb.shouldSuppressLaneField(state, 'lane_place1')).toBe(true);
            expect(sb.shouldSuppressLaneField(state, 'lane_place6')).toBe(true);
        }
    });

    it('does not suppress in Running/Finished/Blank/TotalBlank/other states', () => {
        for (const state of passthroughStates) {
            expect(sb.shouldSuppressLaneField(state, 'lane_time1')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'lane_place1')).toBe(false);
        }
    });

    it('does not suppress unrelated fields even in suppressed states', () => {
        for (const state of suppressedStates) {
            expect(sb.shouldSuppressLaneField(state, 'lane_name1')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'lane_team1')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'lane_running1')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'lane_seed_time1')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'score_home')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'event_name')).toBe(false);
            expect(sb.shouldSuppressLaneField(state, 'lane_time')).toBe(false); // no digit
            expect(sb.shouldSuppressLaneField(state, 'lane_timer1')).toBe(false);
        }
    });
});
