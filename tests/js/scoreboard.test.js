import { describe, it, expect } from 'vitest';
const sb = require('../../static/js/scoreboard.js');

// ---------------------------------------------------------------------------
// parseTimeToSeconds
// ---------------------------------------------------------------------------
describe('parseTimeToSeconds', () => {
    it('returns null for null/undefined/empty', () => {
        expect(sb.parseTimeToSeconds(null)).toBeNull();
        expect(sb.parseTimeToSeconds(undefined)).toBeNull();
        expect(sb.parseTimeToSeconds('')).toBeNull();
        expect(sb.parseTimeToSeconds('   ')).toBeNull();
    });

    it('parses seconds-only times', () => {
        expect(sb.parseTimeToSeconds('23.45')).toBeCloseTo(23.45);
        expect(sb.parseTimeToSeconds('   23.45')).toBeCloseTo(23.45);
    });

    it('parses minutes:seconds times', () => {
        expect(sb.parseTimeToSeconds('1:23.45')).toBeCloseTo(83.45);
        expect(sb.parseTimeToSeconds('  1:23.45')).toBeCloseTo(83.45);
    });

    it('parses zero-padded seconds', () => {
        expect(sb.parseTimeToSeconds('1:05.50')).toBeCloseTo(65.50);
    });

    it('returns null for non-numeric input', () => {
        expect(sb.parseTimeToSeconds('abc')).toBeNull();
    });

    it('handles whole-second values without decimals', () => {
        expect(sb.parseTimeToSeconds('30')).toBeCloseTo(30);
        expect(sb.parseTimeToSeconds('2:00')).toBeCloseTo(120);
    });
});

// ---------------------------------------------------------------------------
// formatSeedTime
// ---------------------------------------------------------------------------
describe('formatSeedTime', () => {
    it('returns empty string for invalid inputs', () => {
        expect(sb.formatSeedTime(null)).toBe('');
        expect(sb.formatSeedTime(undefined)).toBe('');
        expect(sb.formatSeedTime('')).toBe('');
        expect(sb.formatSeedTime(NaN)).toBe('');
    });

    it('formats seconds-only values right-aligned in 8 chars', () => {
        const result = sb.formatSeedTime(23.45);
        expect(result).toBe('   23.45');
        expect(result.length).toBe(8);
    });

    it('formats times over 60 seconds with minutes', () => {
        const result = sb.formatSeedTime(83.45);
        expect(result).toBe(' 1:23.45');
        expect(result.length).toBe(8);
    });

    it('formats zero-padded seconds in minute range', () => {
        const result = sb.formatSeedTime(65.50);
        expect(result).toBe(' 1:05.50');
    });

    it('handles exact minute boundaries', () => {
        const result = sb.formatSeedTime(60.0);
        expect(result).toBe(' 1:00.00');
    });

    it('handles string input', () => {
        expect(sb.formatSeedTime('23.45')).toBe('   23.45');
    });
});

// ---------------------------------------------------------------------------
// getThresholdsForLane
// ---------------------------------------------------------------------------
describe('getThresholdsForLane', () => {
    const thresholds = [
        { sex_code: 1, age_min: 8, age_max: 10, time_seconds: 30.0, tag: 'A' },
        { sex_code: 2, age_min: 8, age_max: 10, time_seconds: 32.0, tag: 'A' },
        { sex_code: 1, age_min: 11, age_max: 12, time_seconds: 28.0, tag: 'A' },
    ];

    it('returns empty for empty thresholds', () => {
        expect(sb.getThresholdsForLane(1, [], {})).toEqual([]);
        expect(sb.getThresholdsForLane(1, null, {})).toEqual([]);
    });

    it('returns all thresholds when no age code for lane', () => {
        expect(sb.getThresholdsForLane(1, thresholds, {})).toEqual(thresholds);
    });

    it('filters by sex and age range', () => {
        const ageCodes = { 1: '9B' };
        const result = sb.getThresholdsForLane(1, thresholds, ageCodes);
        expect(result).toEqual([thresholds[0]]);
    });

    it('matches Girls (G/W) codes to sex_code 2', () => {
        const ageCodes = { 3: '10G' };
        const result = sb.getThresholdsForLane(3, thresholds, ageCodes);
        expect(result).toEqual([thresholds[1]]);
    });

    it('excludes lanes outside age range', () => {
        const ageCodes = { 1: '13B' };
        const result = sb.getThresholdsForLane(1, thresholds, ageCodes);
        expect(result).toEqual([]);
    });

    it('handles M and W sex codes (masters)', () => {
        const ageCodes = { 1: '9M' };
        const result = sb.getThresholdsForLane(1, thresholds, ageCodes);
        expect(result).toEqual([thresholds[0]]); // M maps to sex_code 1
    });
});

// ---------------------------------------------------------------------------
// getRecordsForLane
// ---------------------------------------------------------------------------
describe('getRecordsForLane', () => {
    const recSets = [
        {
            set_team_tag: 'ALL',
            records: [
                { sex_code: 1, age_min: 8, age_max: 10, time_seconds: 25.0, color_class: 'rec-color-1' },
                { sex_code: 2, age_min: 8, age_max: 10, time_seconds: 27.0, color_class: 'rec-color-1' },
            ]
        },
        {
            set_team_tag: 'TEAM1',
            records: [
                { sex_code: 1, age_min: 8, age_max: 10, time_seconds: 26.0, color_class: 'rec-color-2' },
            ]
        }
    ];

    it('returns empty for empty record sets', () => {
        expect(sb.getRecordsForLane(1, [], {}, {})).toEqual([]);
        expect(sb.getRecordsForLane(1, null, {}, {})).toEqual([]);
    });

    it('returns all records when no age/team info', () => {
        const result = sb.getRecordsForLane(1, recSets, {}, {});
        // Should include ALL set records + TEAM1 records (no team filter when laneTeam is "")
        // Actually TEAM1 set_team_tag !== 'ALL' and laneTeam "" !== "TEAM1", so TEAM1 is skipped
        expect(result.length).toBe(2); // Only the ALL records
    });

    it('includes team-specific records when team matches', () => {
        const ageCodes = { 1: '9B' };
        const teamCodes = { 1: 'TEAM1' };
        const result = sb.getRecordsForLane(1, recSets, ageCodes, teamCodes);
        expect(result.length).toBe(2); // ALL boy record + TEAM1 boy record
    });

    it('excludes team-specific records when team does not match', () => {
        const ageCodes = { 1: '9B' };
        const teamCodes = { 1: 'OTHERTEAM' };
        const result = sb.getRecordsForLane(1, recSets, ageCodes, teamCodes);
        expect(result.length).toBe(1); // Only ALL boy record
    });

    it('filters by sex and age', () => {
        const ageCodes = { 1: '10G' };
        const teamCodes = { 1: 'TEAM1' };
        const result = sb.getRecordsForLane(1, recSets, ageCodes, teamCodes);
        // Girls → sex_code 2, only the ALL set girl record matches, TEAM1 record is sex_code 1
        expect(result.length).toBe(1);
        expect(result[0].sex_code).toBe(2);
    });

    it('is case-insensitive for team codes', () => {
        const ageCodes = { 1: '9B' };
        const teamCodes = { 1: 'team1' };
        const result = sb.getRecordsForLane(1, recSets, ageCodes, teamCodes);
        expect(result.length).toBe(2);
    });
});

// ---------------------------------------------------------------------------
// evaluateLaneResult
// ---------------------------------------------------------------------------
describe('evaluateLaneResult', () => {
    const thresholds = [
        { time_seconds: 30.0, tag: 'A', color_class: 'qt-color-1' },
        { time_seconds: 28.0, tag: 'AA', color_class: 'qt-color-2' },
    ];

    const records = [
        { time_seconds: 25.0, color_class: 'rec-color-1' },
    ];

    it('returns null type for null/invalid times', () => {
        expect(sb.evaluateLaneResult(null, thresholds, records, null, false).type).toBeNull();
        expect(sb.evaluateLaneResult(0, thresholds, records, null, false).type).toBeNull();
        expect(sb.evaluateLaneResult(-1, thresholds, records, null, false).type).toBeNull();
    });

    it('returns standard when time meets QT threshold', () => {
        const result = sb.evaluateLaneResult(29.5, thresholds, records, null, false);
        expect(result.type).toBe('standard');
        expect(result.tag).toBe('A');
        expect(result.classes).toContain('qt-std');
        expect(result.classes).toContain('qt-highlight');
    });

    it('returns tightest met standard (smallest time_seconds)', () => {
        const result = sb.evaluateLaneResult(27.0, thresholds, records, null, false);
        expect(result.type).toBe('standard');
        expect(result.tag).toBe('AA');
        expect(result.classes).toContain('qt-color-2');
    });

    it('returns record when time beats record (strict less-than)', () => {
        const result = sb.evaluateLaneResult(24.5, [], records, null, false);
        expect(result.type).toBe('record');
        expect(result.tag).toBe('REC');
        expect(result.classes).toContain('qt-rec');
    });

    it('does NOT break record on tie', () => {
        const result = sb.evaluateLaneResult(25.0, [], records, null, false);
        expect(result.type).toBeNull();
    });

    it('returns both when standard met AND record broken', () => {
        const result = sb.evaluateLaneResult(24.5, thresholds, records, null, false);
        expect(result.type).toBe('both');
        expect(result.tag).toBe('AA REC');
        expect(result.classes).toContain('qt-std');
        expect(result.classes).toContain('qt-rec');
    });

    it('returns PR when time beats seed and showPrTags is true', () => {
        const result = sb.evaluateLaneResult(29.0, [], [], 30.0, true);
        expect(result.type).toBe('pr');
        expect(result.tag).toBe('PR');
        expect(result.classes).toContain('qt-pr');
    });

    it('does NOT return PR when showPrTags is false', () => {
        const result = sb.evaluateLaneResult(29.0, [], [], 30.0, false);
        expect(result.type).toBeNull();
    });

    it('does NOT return PR when time equals seed', () => {
        const result = sb.evaluateLaneResult(30.0, [], [], 30.0, true);
        expect(result.type).toBeNull();
    });

    it('does NOT return PR when no seed time', () => {
        const result = sb.evaluateLaneResult(29.0, [], [], null, true);
        expect(result.type).toBeNull();
    });

    it('standard takes priority over PR', () => {
        const result = sb.evaluateLaneResult(29.5, thresholds, [], 31.0, true);
        expect(result.type).toBe('standard');
        expect(result.tag).toBe('A');
    });

    it('returns null type when time does not meet any threshold', () => {
        const result = sb.evaluateLaneResult(35.0, thresholds, records, null, false);
        expect(result.type).toBeNull();
        expect(result.tag).toBe('');
        expect(result.classes).toEqual([]);
    });
});
