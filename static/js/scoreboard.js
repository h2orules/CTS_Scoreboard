/**
 * Scoreboard logic module — pure functions extracted from home.html.
 *
 * These functions contain the core business logic for time parsing,
 * qualifying-time evaluation, record matching, seed-time formatting,
 * and markdown rendering.  They are free of DOM/Socket.IO dependencies
 * so they can be unit-tested in Node.js.
 *
 * In the browser the module is loaded via a plain <script> tag which
 * puts every export onto `window`.  In Node/Vitest the file is imported
 * as a CommonJS module.
 */

(function (exports) {

    /**
     * Parse a CTS-style display time string like "  1:23.45" or "   23.45"
     * into a number of seconds.  Returns null for blank / unparseable input.
     */
    function parseTimeToSeconds(timeStr) {
        if (!timeStr) return null;
        var t = timeStr.replace(/\s/g, '');
        if (!t || t === '') return null;
        var parts = t.split(':');
        var minutes = 0, secMs;
        if (parts.length === 2) {
            minutes = parseInt(parts[0]) || 0;
            secMs = parseFloat(parts[1]);
        } else {
            secMs = parseFloat(parts[0]);
        }
        if (isNaN(secMs)) return null;
        return minutes * 60 + secMs;
    }

    /**
     * Filter qualifying-time thresholds to those applicable for a lane
     * based on its age/sex code (e.g. "8B", "12G").
     *
     * @param {number|string} lane       Lane number
     * @param {Array}         thresholds All QT items for the current event
     * @param {Object}        ageCodes   Map of lane -> age code string
     * @returns {Array} Applicable thresholds
     */
    function getThresholdsForLane(lane, thresholds, ageCodes) {
        if (!thresholds || thresholds.length === 0) return [];
        var ageCode = (ageCodes && ageCodes[lane]) || "";
        var laneAge = null, laneSexCode = null;
        if (ageCode) {
            var m = ageCode.match(/^(\d+)([BGMW])$/);
            if (m) {
                laneAge = parseInt(m[1]);
                var letterMap = { 'B': 1, 'M': 1, 'G': 2, 'W': 2 };
                laneSexCode = letterMap[m[2]];
            }
        }
        var applicable = [];
        for (var i = 0; i < thresholds.length; i++) {
            var qt = thresholds[i];
            if (laneAge === null || laneSexCode === null) {
                applicable.push(qt);
                continue;
            }
            if (qt.sex_code !== laneSexCode) continue;
            var qMin = qt.age_min || 0;
            var qMax = qt.age_max || 999;
            if (laneAge < qMin || laneAge > qMax) continue;
            applicable.push(qt);
        }
        return applicable;
    }

    /**
     * Filter record sets to those applicable for a lane based on age/sex
     * code and team code.
     *
     * @param {number|string} lane       Lane number
     * @param {Array}         recSets    Array of {set_team_tag, records:[...]}
     * @param {Object}        ageCodes   Map of lane -> age code string
     * @param {Object}        teamCodes  Map of lane -> team code string
     * @returns {Array} Applicable record objects
     */
    function getRecordsForLane(lane, recSets, ageCodes, teamCodes) {
        if (!recSets || recSets.length === 0) return [];
        var ageCode = (ageCodes && ageCodes[lane]) || "";
        var laneTeam = ((teamCodes && teamCodes[lane]) || "").toUpperCase();
        var laneAge = null, laneSexCode = null;
        if (ageCode) {
            var m = ageCode.match(/^(\d+)([BGMW])$/);
            if (m) {
                laneAge = parseInt(m[1]);
                var letterMap = { 'B': 1, 'M': 1, 'G': 2, 'W': 2 };
                laneSexCode = letterMap[m[2]];
            }
        }
        var applicable = [];
        for (var s = 0; s < recSets.length; s++) {
            var set = recSets[s];
            if (set.set_team_tag !== 'ALL') {
                if (laneTeam !== set.set_team_tag.toUpperCase()) continue;
            }
            for (var i = 0; i < set.records.length; i++) {
                var rec = set.records[i];
                if (laneAge === null || laneSexCode === null) {
                    applicable.push(rec);
                    continue;
                }
                if (rec.sex_code !== laneSexCode) continue;
                var rMin = rec.age_min || 0;
                var rMax = rec.age_max || 999;
                if (laneAge < rMin || laneAge > rMax) continue;
                applicable.push(rec);
            }
        }
        return applicable;
    }

    /**
     * Format a seed time in seconds to an 8-char CTS-style display string.
     * Returns "" for null/undefined/NaN input.
     */
    function formatSeedTime(seconds) {
        if (seconds === "" || seconds === null || seconds === undefined || isNaN(seconds)) return "";
        var s = parseFloat(seconds);
        var m = Math.floor(s / 60);
        var rem = s - m * 60;
        if (m > 0) {
            return (" " + m + ":" + ("0" + rem.toFixed(2)).slice(-5)).slice(-8);
        } else {
            return ("        " + rem.toFixed(2)).slice(-8);
        }
    }

    /**
     * Evaluate a lane's time against qualifying standards and records.
     * Returns a result object describing the decoration to apply.
     *
     * @param {number}  seconds      Parsed time in seconds
     * @param {Array}   thresholds   Applicable QT items for this lane
     * @param {Array}   records      Applicable record items for this lane
     * @param {number|null} seedTime Seed time in seconds (for PR check)
     * @param {boolean} showPrTags   Whether PR tags are enabled
     * @returns {Object} { tag, classes[], type: 'standard'|'record'|'both'|'pr'|null }
     */
    function evaluateLaneResult(seconds, thresholds, records, seedTime, showPrTags) {
        if (seconds === null || seconds <= 0) return { tag: '', classes: [], type: null };

        // Check qualifying time standards (<=)
        var bestStd = null;
        if (thresholds && thresholds.length > 0) {
            for (var i = 0; i < thresholds.length; i++) {
                var qt = thresholds[i];
                if (seconds <= qt.time_seconds) {
                    if (bestStd === null || qt.time_seconds < bestStd.time_seconds) {
                        bestStd = qt;
                    }
                }
            }
        }

        // Check records (strict <, tie does not break)
        var bestRec = null;
        if (records && records.length > 0) {
            for (var i = 0; i < records.length; i++) {
                var rec = records[i];
                if (seconds < rec.time_seconds) {
                    if (bestRec === null || rec.time_seconds < bestRec.time_seconds) {
                        bestRec = rec;
                    }
                }
            }
        }

        if (bestStd && bestRec) {
            return {
                tag: bestStd.tag + " REC",
                classes: ['qt-highlight', 'qt-std', 'qt-rec', bestStd.color_class],
                type: 'both'
            };
        } else if (bestStd) {
            return {
                tag: bestStd.tag,
                classes: ['qt-highlight', 'qt-std', bestStd.color_class],
                type: 'standard'
            };
        } else if (bestRec) {
            return {
                tag: "REC",
                classes: ['qt-highlight', 'qt-rec', bestRec.color_class],
                type: 'record'
            };
        } else if (showPrTags && typeof seedTime === 'number' && seedTime > 0 && seconds < seedTime) {
            return {
                tag: "PR",
                classes: ['qt-highlight', 'qt-pr'],
                type: 'pr'
            };
        }

        return { tag: '', classes: [], type: null };
    }

    // Export all functions
    exports.parseTimeToSeconds = parseTimeToSeconds;
    exports.getThresholdsForLane = getThresholdsForLane;
    exports.getRecordsForLane = getRecordsForLane;
    exports.formatSeedTime = formatSeedTime;
    exports.evaluateLaneResult = evaluateLaneResult;

})(typeof module !== 'undefined' && module.exports ? module.exports : window);
