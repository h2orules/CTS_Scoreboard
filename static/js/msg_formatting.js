/**
 * Message-board markdown formatting helpers.
 *
 * Pure-logic functions extracted from settings.html so they can be
 * unit-tested in Node / Vitest.  Every function that previously touched
 * a textarea element now takes a plain object with { value, selectionStart,
 * selectionEnd } and mutates it in place, exactly like the DOM version.
 */

(function (exports) {

    /* ── Format definitions ────────────────────────────────────── */

    var FMT = {
        bold:      { wrap: ['**', '**'] },
        italic:    { wrap: ['*', '*'] },
        underline: { wrap: ['_', '_'] },
        strike:    { wrap: ['~~', '~~'] },
        code:      { wrap: ['`', '`'] },
        ul:        { prefix: '- ' },
        ol:        { prefix: '1. ' }
    };

    /* ── Visible-length calculation ────────────────────────────── */

    function visibleLen(line) {
        var s = line
            .replace(/^\s*#{1,4}\s+/, '')
            .replace(/^\s*(\d+\.|[-\*])\s+/, '')
            .replace(/`([^`\n]+)`/g, '$1')
            .replace(/\*\*([^\*\n]+)\*\*/g, '$1')
            .replace(/~~([^~\n]+)~~/g, '$1')
            .replace(/(^|[^\*])\*([^\*\n]+)\*(?!\*)/g, '$1$2')
            .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1$2');
        return s.length;
    }

    /* ── Line helpers ──────────────────────────────────────────── */

    function getCurrentLine(ta) {
        var val = ta.value;
        var pos = ta.selectionStart;
        var lineStart = val.lastIndexOf('\n', pos - 1) + 1;
        var lineEnd = val.indexOf('\n', pos);
        if (lineEnd === -1) lineEnd = val.length;
        return { start: lineStart, end: lineEnd, text: val.substring(lineStart, lineEnd) };
    }

    function getSelectedLines(ta) {
        var val = ta.value;
        var ss = ta.selectionStart, se = ta.selectionEnd;
        var lineStart = val.lastIndexOf('\n', ss - 1) + 1;
        var lineEnd = val.indexOf('\n', se - 1);
        if (lineEnd === -1 || lineEnd < se) lineEnd = val.indexOf('\n', se);
        if (lineEnd === -1) lineEnd = val.length;
        return { start: lineStart, end: lineEnd, text: val.substring(lineStart, lineEnd) };
    }

    /* ── Heading detection ─────────────────────────────────────── */

    function detectHeading(lineText) {
        var m = lineText.match(/^(#{1,4})\s/);
        return m ? m[1].length : 0;
    }

    /* ── Inline-format detection ───────────────────────────────── */

    function detectFormats(ta) {
        var result = { bold: false, italic: false, underline: false, strike: false, code: false };
        var line = getCurrentLine(ta);
        var lt = line.text;
        var cp = ta.selectionStart - line.start;
        if (cp < 0) cp = 0;
        if (cp > lt.length) cp = lt.length;

        var state = { bold: false, italic: false, underline: false, strike: false, code: false };
        var pos = 0;
        while (pos < cp) {
            if (lt[pos] === '`') {
                state.code = !state.code;
                pos++;
            } else if (state.code) {
                pos++;
            } else if (pos + 1 < cp && lt[pos] === '~' && lt[pos + 1] === '~') {
                state.strike = !state.strike;
                pos += 2;
            } else if (lt[pos] === '*') {
                var ss = pos;
                while (pos < cp && lt[pos] === '*') pos++;
                var n = pos - ss;
                if (Math.floor(n / 2) % 2 === 1) state.bold = !state.bold;
                if (n % 2 === 1) state.italic = !state.italic;
            } else if (lt[pos] === '_') {
                state.underline = !state.underline;
                pos++;
            } else {
                pos++;
            }
        }

        var right = lt.substring(cp);

        if (state.code) {
            var bi = right.indexOf('`');
            if (bi >= 0) {
                result.code = true;
                var after = right.substring(bi + 1);
                if (state.bold)      result.bold      = after.indexOf('**') >= 0;
                if (state.italic)    { var s = after.replace(/\*\*/g, ''); result.italic = s.indexOf('*') >= 0; }
                if (state.strike)    result.strike    = after.indexOf('~~') >= 0;
                if (state.underline) result.underline = after.indexOf('_') >= 0;
            }
        } else {
            var clean = right.replace(/`[^`]*`/g, function (m) {
                return '`' + ' '.repeat(Math.max(0, m.length - 2)) + '`';
            });
            if (state.bold)      result.bold      = clean.indexOf('**') >= 0;
            if (state.italic)    { var s = clean.replace(/\*\*/g, ''); result.italic = s.indexOf('*') >= 0; }
            if (state.strike)    result.strike    = clean.indexOf('~~') >= 0;
            if (state.underline) result.underline = clean.indexOf('_') >= 0;
        }

        return result;
    }

    /* ── Prefix detection ──────────────────────────────────────── */

    function detectPrefix(ta, prefix) {
        var line = getCurrentLine(ta);
        return line.text.indexOf(prefix) === 0 || /^\s*/.exec(line.text)[0].length + prefix.length <= line.text.length && line.text.trimStart().indexOf(prefix) === 0;
    }

    /* ── Marker-pair finding ───────────────────────────────────── */

    function findMarkerPair(val, ss, se, open, close, line) {
        var lineStart = line.start;
        var lineEnd = line.end;

        if (open[0] === '*') {
            return findStarMarkerPair(val, ss, se, open.length, lineStart, lineEnd);
        }

        var left = ss;
        while (left >= lineStart + open.length) {
            if (val.substring(left - open.length, left) === open) break;
            left--;
        }
        var openStart = left - open.length;
        if (openStart < lineStart) return null;

        var right = se;
        while (right + close.length <= lineEnd) {
            if (val.substring(right, right + close.length) === close) break;
            right++;
        }
        if (right + close.length > lineEnd) return null;

        return {
            openStart: openStart,
            openEnd: openStart + open.length,
            closeStart: right,
            closeEnd: right + close.length
        };
    }

    function findStarMarkerPair(val, ss, se, starCount, lineStart, lineEnd) {
        var left = ss;
        while (left > lineStart && val[left - 1] !== '*') left--;
        var starEnd = left;
        while (left > lineStart && val[left - 1] === '*') left--;
        var leftStars = starEnd - left;

        var right = se;
        while (right < lineEnd && val[right] !== '*') right++;
        var rightStarStart = right;
        while (right < lineEnd && val[right] === '*') right++;
        var rightStars = right - rightStarStart;

        var minS = Math.min(leftStars, rightStars);
        var isActive = (starCount === 2) ? minS >= 2 : minS % 2 === 1;
        if (!isActive || leftStars === 0 || rightStars === 0) return null;

        if (starCount === 2) {
            return {
                openStart: left, openEnd: left + leftStars,
                closeStart: rightStarStart, closeEnd: rightStarStart + rightStars,
                leftStars: leftStars, rightStars: rightStars, removeCount: 2
            };
        } else {
            return {
                openStart: left, openEnd: left + leftStars,
                closeStart: rightStarStart, closeEnd: rightStarStart + rightStars,
                leftStars: leftStars, rightStars: rightStars, removeCount: 1
            };
        }
    }

    /* ── Remove marker pair at cursor ──────────────────────────── */

    function removeMarkerPair(ta, open, close, line) {
        var val = ta.value;
        var ss = ta.selectionStart;
        var cp = ss - line.start;
        var lt = line.text;

        if (open[0] === '*') {
            var starCount = open.length;
            var contentLeft = cp;
            while (contentLeft > 0 && lt[contentLeft - 1] !== '*') contentLeft--;
            var starLeft = contentLeft;
            while (starLeft > 0 && lt[starLeft - 1] === '*') starLeft--;
            var leftCount = contentLeft - starLeft;

            var contentRight = cp;
            while (contentRight < lt.length && lt[contentRight] !== '*') contentRight++;
            var starRight = contentRight;
            while (starRight < lt.length && lt[starRight] === '*') starRight++;
            var rightCount = starRight - contentRight;

            if (leftCount >= starCount && rightCount >= starCount) {
                var lS = line.start + starLeft;
                var rE = line.start + starRight;
                var nL = leftCount - starCount;
                var nR = rightCount - starCount;
                var inner = val.substring(line.start + contentLeft, line.start + contentRight);
                ta.value = val.substring(0, lS) +
                    '*'.repeat(Math.max(0, nL)) + inner +
                    '*'.repeat(Math.max(0, nR)) + val.substring(rE);
                ta.selectionStart = ta.selectionEnd = lS + Math.max(0, nL) + (cp - contentLeft);
            }
            return;
        }

        var leftPos = cp - 1;
        while (leftPos >= 0) {
            if (lt.substring(leftPos, leftPos + open.length) === open) break;
            leftPos--;
        }
        if (leftPos < 0) return;

        var rightPos = cp;
        while (rightPos + close.length <= lt.length) {
            if (lt.substring(rightPos, rightPos + close.length) === close) break;
            rightPos++;
        }
        if (rightPos + close.length > lt.length) return;

        var mStart = line.start + leftPos;
        var mEnd = line.start + rightPos + close.length;
        var inner = val.substring(mStart + open.length, line.start + rightPos);
        ta.value = val.substring(0, mStart) + inner + val.substring(mEnd);
        ta.selectionStart = ta.selectionEnd = ss - open.length;
    }

    /* ── Toggle wrap (bold / italic / underline / strike / code) ─ */

    function toggleWrap(ta, wrapDef) {
        var open = wrapDef[0], close = wrapDef[1];
        var ss = ta.selectionStart, se = ta.selectionEnd;
        var val = ta.value;
        var line = getCurrentLine(ta);

        if (ss === se) {
            var fmts = detectFormats(ta);
            var fmtName = open === '**' ? 'bold' : open === '*' ? 'italic'
                        : open === '_' ? 'underline' : open === '~~' ? 'strike' : 'code';
            if (fmts[fmtName]) {
                removeMarkerPair(ta, open, close, line);
            } else {
                ta.value = val.substring(0, ss) + open + close + val.substring(ss);
                ta.selectionStart = ta.selectionEnd = ss + open.length;
            }
            return;
        }

        var found = findMarkerPair(val, ss, se, open, close, line);
        if (found) {
            if (found.removeCount !== undefined) {
                var newLeftStars = found.leftStars - found.removeCount;
                var newRightStars = found.rightStars - found.removeCount;
                var content = val.substring(found.openEnd, found.closeStart);
                ta.value = val.substring(0, found.openStart) +
                    '*'.repeat(Math.max(0, newLeftStars)) + content +
                    '*'.repeat(Math.max(0, newRightStars)) + val.substring(found.closeEnd);
                var shift = found.leftStars - Math.max(0, newLeftStars);
                ta.selectionStart = ss - shift;
                ta.selectionEnd = se - shift;
            } else {
                var before = val.substring(0, found.openStart);
                var mid = val.substring(found.openEnd, found.closeStart);
                var after = val.substring(found.closeEnd);
                ta.value = before + mid + after;
                ta.selectionStart = ss - (found.openEnd - found.openStart);
                ta.selectionEnd = se - (found.openEnd - found.openStart);
            }
        } else {
            var sel = val.substring(ss, se);
            ta.value = val.substring(0, ss) + open + sel + close + val.substring(se);
            ta.selectionStart = ss + open.length;
            ta.selectionEnd = se + open.length;
        }
    }

    /* ── Toggle prefix (bullet / numbered list) ────────────────── */

    function togglePrefix(ta, prefix, fmt) {
        var info = getSelectedLines(ta);
        var val = ta.value;
        var lines = info.text.split('\n');
        var allHavePrefix = lines.every(function(l) {
            var stripped = l.replace(/^\s*/, '');
            return stripped.indexOf(prefix) === 0 || (fmt === 'ol' && /^\s*\d+\.\s/.test(l));
        });
        var newLines;
        if (allHavePrefix) {
            newLines = lines.map(function(l) {
                if (fmt === 'ol') return l.replace(/^\s*\d+\.\s/, '');
                return l.replace(new RegExp('^(\\s*)' + prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), '$1');
            });
        } else {
            newLines = lines.map(function(l, i) {
                var indent = /^(\s*)/.exec(l)[1];
                var content = l.substring(indent.length);
                content = content.replace(/^(\d+\.\s|[-*]\s)/, '');
                if (fmt === 'ol') return indent + (i + 1) + '. ' + content;
                return indent + prefix + content;
            });
        }
        var result = newLines.join('\n');
        ta.value = val.substring(0, info.start) + result + val.substring(info.end);
        ta.selectionStart = info.start;
        ta.selectionEnd = info.start + result.length;
    }

    /* ── Set heading level ─────────────────────────────────────── */

    function setHeading(ta, level) {
        var line = getCurrentLine(ta);
        var val = ta.value;
        var lineText = line.text;
        var stripped = lineText.replace(/^#{1,4}\s+/, '');
        var newLine = level > 0 ? ('#'.repeat(level) + ' ' + stripped) : stripped;
        ta.value = val.substring(0, line.start) + newLine + val.substring(line.end);
        ta.selectionStart = ta.selectionEnd = line.start + newLine.length;
    }

    /* ── High-level toggle (called by toolbar buttons) ─────────── */

    function fmtToggle(ta, fmt) {
        var def = FMT[fmt];
        if (def.wrap) {
            toggleWrap(ta, def.wrap);
        } else if (def.prefix) {
            togglePrefix(ta, def.prefix, fmt);
        }
    }

    /* ── Exports ───────────────────────────────────────────────── */

    exports.FMT            = FMT;
    exports.visibleLen      = visibleLen;
    exports.getCurrentLine  = getCurrentLine;
    exports.getSelectedLines = getSelectedLines;
    exports.detectHeading   = detectHeading;
    exports.detectFormats   = detectFormats;
    exports.detectPrefix    = detectPrefix;
    exports.findMarkerPair  = findMarkerPair;
    exports.findStarMarkerPair = findStarMarkerPair;
    exports.removeMarkerPair = removeMarkerPair;
    exports.toggleWrap      = toggleWrap;
    exports.togglePrefix    = togglePrefix;
    exports.setHeading      = setHeading;
    exports.fmtToggle       = fmtToggle;

})(typeof exports !== 'undefined' ? exports : (this.MsgFmt = {}));
