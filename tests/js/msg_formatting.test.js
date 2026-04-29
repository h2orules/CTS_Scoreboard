import { describe, it, expect } from 'vitest';
const fmt = require('../../static/js/msg_formatting.js');

// Helper: create a mock textarea-like object
function mockTA(value, selStart, selEnd) {
    if (selEnd === undefined) selEnd = selStart;
    return { value, selectionStart: selStart, selectionEnd: selEnd };
}

// ---------------------------------------------------------------------------
// visibleLen
// ---------------------------------------------------------------------------
describe('visibleLen', () => {
    it('returns length for plain text', () => {
        expect(fmt.visibleLen('hello world')).toBe(11);
    });

    it('strips heading markers', () => {
        expect(fmt.visibleLen('# Heading')).toBe(7);      // "Heading"
        expect(fmt.visibleLen('## Heading')).toBe(7);
        expect(fmt.visibleLen('### Heading')).toBe(7);
        expect(fmt.visibleLen('#### Heading')).toBe(7);
    });

    it('strips bullet prefix', () => {
        expect(fmt.visibleLen('- item')).toBe(4);          // "item"
        expect(fmt.visibleLen('* item')).toBe(4);
    });

    it('strips numbered list prefix', () => {
        expect(fmt.visibleLen('1. item')).toBe(4);
        expect(fmt.visibleLen('12. item')).toBe(4);
    });

    it('strips bold markers', () => {
        expect(fmt.visibleLen('**bold**')).toBe(4);
    });

    it('strips italic markers', () => {
        expect(fmt.visibleLen('*italic*')).toBe(6);
    });

    it('strips underline markers', () => {
        expect(fmt.visibleLen('_underline_')).toBe(9);
    });

    it('strips strikethrough markers', () => {
        expect(fmt.visibleLen('~~strike~~')).toBe(6);
    });

    it('strips code markers', () => {
        expect(fmt.visibleLen('`code`')).toBe(4);
    });

    it('strips combined formats', () => {
        // "**bold** and *italic*" → "bold and italic"
        expect(fmt.visibleLen('**bold** and *italic*')).toBe(15);
    });
});

// ---------------------------------------------------------------------------
// getCurrentLine
// ---------------------------------------------------------------------------
describe('getCurrentLine', () => {
    it('returns entire value for single-line input', () => {
        const ta = mockTA('hello', 3);
        const line = fmt.getCurrentLine(ta);
        expect(line).toEqual({ start: 0, end: 5, text: 'hello' });
    });

    it('returns correct line on second line', () => {
        const ta = mockTA('line1\nline2\nline3', 8); // cursor in "line2"
        const line = fmt.getCurrentLine(ta);
        expect(line).toEqual({ start: 6, end: 11, text: 'line2' });
    });

    it('returns first line when cursor at start', () => {
        const ta = mockTA('aaa\nbbb', 0);
        const line = fmt.getCurrentLine(ta);
        expect(line).toEqual({ start: 0, end: 3, text: 'aaa' });
    });

    it('returns last line when cursor at end', () => {
        const ta = mockTA('aaa\nbbb', 7);
        const line = fmt.getCurrentLine(ta);
        expect(line).toEqual({ start: 4, end: 7, text: 'bbb' });
    });
});

// ---------------------------------------------------------------------------
// getSelectedLines
// ---------------------------------------------------------------------------
describe('getSelectedLines', () => {
    it('returns full line range spanning selection', () => {
        const ta = mockTA('aaa\nbbb\nccc', 5, 9); // selecting across bbb and ccc
        const lines = fmt.getSelectedLines(ta);
        expect(lines.text).toBe('bbb\nccc');
    });

    it('returns single line when selection is within one line', () => {
        const ta = mockTA('hello world', 2, 5);
        const lines = fmt.getSelectedLines(ta);
        expect(lines.text).toBe('hello world');
    });
});

// ---------------------------------------------------------------------------
// detectHeading
// ---------------------------------------------------------------------------
describe('detectHeading', () => {
    it('returns 0 for non-heading lines', () => {
        expect(fmt.detectHeading('plain text')).toBe(0);
        expect(fmt.detectHeading('')).toBe(0);
    });

    it('detects heading levels 1-4', () => {
        expect(fmt.detectHeading('# Heading')).toBe(1);
        expect(fmt.detectHeading('## Heading')).toBe(2);
        expect(fmt.detectHeading('### Heading')).toBe(3);
        expect(fmt.detectHeading('#### Heading')).toBe(4);
    });

    it('returns 0 for 5+ hashes', () => {
        expect(fmt.detectHeading('##### Heading')).toBe(0);
    });

    it('requires space after hashes', () => {
        expect(fmt.detectHeading('#NoSpace')).toBe(0);
    });
});

// ---------------------------------------------------------------------------
// detectFormats — cursor inside formatted text
// ---------------------------------------------------------------------------
describe('detectFormats', () => {
    it('detects bold when cursor is inside **text**', () => {
        const ta = mockTA('**bold**', 4);
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(true);
        expect(f.italic).toBe(false);
    });

    it('detects italic when cursor is inside *text*', () => {
        const ta = mockTA('*italic*', 4);
        const f = fmt.detectFormats(ta);
        expect(f.italic).toBe(true);
        expect(f.bold).toBe(false);
    });

    it('detects underline when cursor is inside _text_', () => {
        const ta = mockTA('_underline_', 5);
        const f = fmt.detectFormats(ta);
        expect(f.underline).toBe(true);
    });

    it('detects strikethrough when cursor is inside ~~text~~', () => {
        const ta = mockTA('~~strike~~', 5);
        const f = fmt.detectFormats(ta);
        expect(f.strike).toBe(true);
    });

    it('detects code when cursor is inside `text`', () => {
        const ta = mockTA('`code`', 3);
        const f = fmt.detectFormats(ta);
        expect(f.code).toBe(true);
    });

    it('returns all false when cursor is outside markers', () => {
        const ta = mockTA('plain text', 5);
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(false);
        expect(f.italic).toBe(false);
        expect(f.underline).toBe(false);
        expect(f.strike).toBe(false);
        expect(f.code).toBe(false);
    });

    it('detects bold+italic when cursor is inside ***text***', () => {
        const ta = mockTA('***both***', 5);
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(true);
        expect(f.italic).toBe(true);
    });

    it('detects nested formats', () => {
        // _*text*_ → underline + italic
        const ta = mockTA('_*text*_', 4);
        const f = fmt.detectFormats(ta);
        expect(f.underline).toBe(true);
        expect(f.italic).toBe(true);
    });

    it('does not detect format when cursor is before opening marker', () => {
        const ta = mockTA('**bold**', 0);
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(false);
    });

    it('does not detect format when cursor is after closing marker', () => {
        const ta = mockTA('**bold**', 8);
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(false);
    });

    it('handles cursor on multi-line at correct line', () => {
        const ta = mockTA('plain\n**bold**', 10); // inside bold on line 2
        const f = fmt.detectFormats(ta);
        expect(f.bold).toBe(true);
    });

    it('ignores markers inside code spans for other formats', () => {
        // cursor inside code, bold markers after the closing backtick should not count
        const ta = mockTA('`**text**`', 5);
        const f = fmt.detectFormats(ta);
        expect(f.code).toBe(true);
        expect(f.bold).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// detectPrefix
// ---------------------------------------------------------------------------
describe('detectPrefix', () => {
    it('detects bullet prefix', () => {
        const ta = mockTA('- item', 3);
        expect(fmt.detectPrefix(ta, '- ')).toBe(true);
    });

    it('detects numbered list prefix', () => {
        const ta = mockTA('1. item', 3);
        expect(fmt.detectPrefix(ta, '1. ')).toBe(true);
    });

    it('returns false when no prefix', () => {
        const ta = mockTA('plain text', 3);
        expect(fmt.detectPrefix(ta, '- ')).toBe(false);
    });

    it('detects prefix with leading whitespace', () => {
        const ta = mockTA('  - item', 5);
        expect(fmt.detectPrefix(ta, '- ')).toBe(true);
    });
});

// ---------------------------------------------------------------------------
// toggleWrap — adding markers
// ---------------------------------------------------------------------------
describe('toggleWrap — add markers', () => {
    it('wraps selection with bold markers', () => {
        const ta = mockTA('hello world', 6, 11); // select "world"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('hello **world**');
        expect(ta.selectionStart).toBe(8);
        expect(ta.selectionEnd).toBe(13);
    });

    it('wraps selection with italic markers', () => {
        const ta = mockTA('hello world', 0, 5); // select "hello"
        fmt.toggleWrap(ta, ['*', '*']);
        expect(ta.value).toBe('*hello* world');
        expect(ta.selectionStart).toBe(1);
        expect(ta.selectionEnd).toBe(6);
    });

    it('wraps selection with underline markers', () => {
        const ta = mockTA('text', 0, 4);
        fmt.toggleWrap(ta, ['_', '_']);
        expect(ta.value).toBe('_text_');
    });

    it('wraps selection with strikethrough markers', () => {
        const ta = mockTA('text', 0, 4);
        fmt.toggleWrap(ta, ['~~', '~~']);
        expect(ta.value).toBe('~~text~~');
    });

    it('wraps selection with code markers', () => {
        const ta = mockTA('text', 0, 4);
        fmt.toggleWrap(ta, ['`', '`']);
        expect(ta.value).toBe('`text`');
    });

    it('inserts empty markers at collapsed cursor', () => {
        const ta = mockTA('hello', 5);
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('hello****');
        expect(ta.selectionStart).toBe(7); // cursor between markers
    });
});

// ---------------------------------------------------------------------------
// toggleWrap — removing markers
// ---------------------------------------------------------------------------
describe('toggleWrap — remove markers', () => {
    it('removes bold markers when text is selected', () => {
        const ta = mockTA('**hello**', 2, 7); // select "hello"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('hello');
    });

    it('removes italic markers when text is selected', () => {
        const ta = mockTA('*hello*', 1, 6);
        fmt.toggleWrap(ta, ['*', '*']);
        expect(ta.value).toBe('hello');
    });

    it('removes underline markers when text is selected', () => {
        const ta = mockTA('_hello_', 1, 6);
        fmt.toggleWrap(ta, ['_', '_']);
        expect(ta.value).toBe('hello');
    });

    it('removes strikethrough markers when text is selected', () => {
        const ta = mockTA('~~hello~~', 2, 7);
        fmt.toggleWrap(ta, ['~~', '~~']);
        expect(ta.value).toBe('hello');
    });

    it('removes code markers when text is selected', () => {
        const ta = mockTA('`hello`', 1, 6);
        fmt.toggleWrap(ta, ['`', '`']);
        expect(ta.value).toBe('hello');
    });

    it('removes bold at collapsed cursor inside **text**', () => {
        const ta = mockTA('**hello**', 5); // cursor inside "hello"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('hello');
    });

    it('removes italic at collapsed cursor inside *text*', () => {
        const ta = mockTA('*hello*', 4);
        fmt.toggleWrap(ta, ['*', '*']);
        expect(ta.value).toBe('hello');
    });

    it('removes only bold from ***bold+italic*** leaving italic', () => {
        const ta = mockTA('***text***', 2, 8); // select inner including single star
        // With selection on "**text**" inside the triple stars
        const ta2 = mockTA('***text***', 3, 7); // select "text"
        fmt.toggleWrap(ta2, ['**', '**']);
        expect(ta2.value).toBe('*text*');
    });

    it('removes only italic from ***bold+italic*** leaving bold', () => {
        const ta = mockTA('***text***', 3, 7);
        fmt.toggleWrap(ta, ['*', '*']);
        expect(ta.value).toBe('**text**');
    });
});

// ---------------------------------------------------------------------------
// toggleWrap — multi-line context
// ---------------------------------------------------------------------------
describe('toggleWrap — multi-line', () => {
    it('wraps on second line without affecting first', () => {
        const ta = mockTA('first\nsecond', 6, 12); // select "second"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('first\n**second**');
    });

    it('removes markers on second line', () => {
        const ta = mockTA('first\n**second**', 8, 14); // select "second"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('first\nsecond');
    });
});

// ---------------------------------------------------------------------------
// togglePrefix — bullet lists
// ---------------------------------------------------------------------------
describe('togglePrefix — bullet list', () => {
    it('adds bullet prefix to plain line', () => {
        const ta = mockTA('item', 0, 4);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('- item');
    });

    it('removes bullet prefix from bulleted line', () => {
        const ta = mockTA('- item', 0, 6);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('item');
    });

    it('adds bullet prefix to multiple lines', () => {
        const ta = mockTA('aaa\nbbb\nccc', 0, 11);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('- aaa\n- bbb\n- ccc');
    });

    it('removes bullet prefix from multiple bulleted lines', () => {
        const ta = mockTA('- aaa\n- bbb\n- ccc', 0, 18);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('aaa\nbbb\nccc');
    });

    it('switches from numbered list to bullet list', () => {
        const ta = mockTA('1. item', 0, 7);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('- item');
    });
});

// ---------------------------------------------------------------------------
// togglePrefix — numbered lists
// ---------------------------------------------------------------------------
describe('togglePrefix — numbered list', () => {
    it('adds numbered prefix to plain line', () => {
        const ta = mockTA('item', 0, 4);
        fmt.togglePrefix(ta, '1. ', 'ol');
        expect(ta.value).toBe('1. item');
    });

    it('removes numbered prefix from numbered line', () => {
        const ta = mockTA('1. item', 0, 7);
        fmt.togglePrefix(ta, '1. ', 'ol');
        expect(ta.value).toBe('item');
    });

    it('adds sequential numbers to multiple lines', () => {
        const ta = mockTA('aaa\nbbb\nccc', 0, 11);
        fmt.togglePrefix(ta, '1. ', 'ol');
        expect(ta.value).toBe('1. aaa\n2. bbb\n3. ccc');
    });

    it('removes numbered prefix from multiple lines', () => {
        const ta = mockTA('1. aaa\n2. bbb\n3. ccc', 0, 20);
        fmt.togglePrefix(ta, '1. ', 'ol');
        expect(ta.value).toBe('aaa\nbbb\nccc');
    });

    it('switches from bullet list to numbered list', () => {
        const ta = mockTA('- item', 0, 6);
        fmt.togglePrefix(ta, '1. ', 'ol');
        expect(ta.value).toBe('1. item');
    });
});

// ---------------------------------------------------------------------------
// setHeading
// ---------------------------------------------------------------------------
describe('setHeading', () => {
    it('adds heading level 1 to plain text', () => {
        const ta = mockTA('hello', 3);
        fmt.setHeading(ta, 1);
        expect(ta.value).toBe('# hello');
    });

    it('adds heading level 2', () => {
        const ta = mockTA('hello', 3);
        fmt.setHeading(ta, 2);
        expect(ta.value).toBe('## hello');
    });

    it('adds heading level 3', () => {
        const ta = mockTA('hello', 3);
        fmt.setHeading(ta, 3);
        expect(ta.value).toBe('### hello');
    });

    it('adds heading level 4', () => {
        const ta = mockTA('hello', 3);
        fmt.setHeading(ta, 4);
        expect(ta.value).toBe('#### hello');
    });

    it('removes heading when level is 0', () => {
        const ta = mockTA('## hello', 5);
        fmt.setHeading(ta, 0);
        expect(ta.value).toBe('hello');
    });

    it('changes heading level', () => {
        const ta = mockTA('# hello', 5);
        fmt.setHeading(ta, 3);
        expect(ta.value).toBe('### hello');
    });

    it('works on second line of multi-line text', () => {
        const ta = mockTA('first\nhello', 8);
        fmt.setHeading(ta, 1);
        expect(ta.value).toBe('first\n# hello');
    });

    it('replaces heading on second line', () => {
        const ta = mockTA('first\n## hello', 10);
        fmt.setHeading(ta, 4);
        expect(ta.value).toBe('first\n#### hello');
    });
});

// ---------------------------------------------------------------------------
// fmtToggle — high-level integration
// ---------------------------------------------------------------------------
describe('fmtToggle', () => {
    it('applies bold via fmtToggle', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.fmtToggle(ta, 'bold');
        expect(ta.value).toBe('**hello**');
    });

    it('applies italic via fmtToggle', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.fmtToggle(ta, 'italic');
        expect(ta.value).toBe('*hello*');
    });

    it('applies underline via fmtToggle', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.fmtToggle(ta, 'underline');
        expect(ta.value).toBe('_hello_');
    });

    it('applies strike via fmtToggle', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.fmtToggle(ta, 'strike');
        expect(ta.value).toBe('~~hello~~');
    });

    it('applies code via fmtToggle', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.fmtToggle(ta, 'code');
        expect(ta.value).toBe('`hello`');
    });

    it('applies bullet list via fmtToggle', () => {
        const ta = mockTA('item', 0, 4);
        fmt.fmtToggle(ta, 'ul');
        expect(ta.value).toBe('- item');
    });

    it('applies numbered list via fmtToggle', () => {
        const ta = mockTA('item', 0, 4);
        fmt.fmtToggle(ta, 'ol');
        expect(ta.value).toBe('1. item');
    });

    it('toggles bold off via fmtToggle', () => {
        const ta = mockTA('**hello**', 2, 7);
        fmt.fmtToggle(ta, 'bold');
        expect(ta.value).toBe('hello');
    });
});

// ---------------------------------------------------------------------------
// findMarkerPair / findStarMarkerPair
// ---------------------------------------------------------------------------
describe('findMarkerPair', () => {
    it('finds tilde marker pair around selection', () => {
        const val = '~~hello~~';
        const line = { start: 0, end: 9, text: val };
        const result = fmt.findMarkerPair(val, 2, 7, '~~', '~~', line);
        expect(result).not.toBeNull();
        expect(result.openStart).toBe(0);
        expect(result.openEnd).toBe(2);
        expect(result.closeStart).toBe(7);
        expect(result.closeEnd).toBe(9);
    });

    it('finds underscore marker pair', () => {
        const val = '_hello_';
        const line = { start: 0, end: 7, text: val };
        const result = fmt.findMarkerPair(val, 1, 6, '_', '_', line);
        expect(result).not.toBeNull();
        expect(result.openStart).toBe(0);
        expect(result.closeStart).toBe(6);
    });

    it('finds backtick marker pair', () => {
        const val = '`hello`';
        const line = { start: 0, end: 7, text: val };
        const result = fmt.findMarkerPair(val, 1, 6, '`', '`', line);
        expect(result).not.toBeNull();
    });

    it('returns null when no markers found', () => {
        const val = 'hello';
        const line = { start: 0, end: 5, text: val };
        const result = fmt.findMarkerPair(val, 0, 5, '~~', '~~', line);
        expect(result).toBeNull();
    });

    it('delegates to findStarMarkerPair for star markers', () => {
        const val = '**hello**';
        const line = { start: 0, end: 9, text: val };
        const result = fmt.findMarkerPair(val, 2, 7, '**', '**', line);
        expect(result).not.toBeNull();
        expect(result.removeCount).toBe(2);
    });
});

describe('findStarMarkerPair', () => {
    it('finds bold star pair', () => {
        const val = '**hello**';
        const result = fmt.findStarMarkerPair(val, 2, 7, 2, 0, 9);
        expect(result).not.toBeNull();
        expect(result.leftStars).toBe(2);
        expect(result.rightStars).toBe(2);
        expect(result.removeCount).toBe(2);
    });

    it('finds italic star pair', () => {
        const val = '*hello*';
        const result = fmt.findStarMarkerPair(val, 1, 6, 1, 0, 7);
        expect(result).not.toBeNull();
        expect(result.leftStars).toBe(1);
        expect(result.rightStars).toBe(1);
        expect(result.removeCount).toBe(1);
    });

    it('finds bold within bold+italic (triple stars)', () => {
        const val = '***hello***';
        const result = fmt.findStarMarkerPair(val, 3, 8, 2, 0, 11);
        expect(result).not.toBeNull();
        expect(result.leftStars).toBe(3);
        expect(result.removeCount).toBe(2);
    });

    it('returns null when no star markers present', () => {
        const val = 'hello';
        const result = fmt.findStarMarkerPair(val, 0, 5, 2, 0, 5);
        expect(result).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// removeMarkerPair
// ---------------------------------------------------------------------------
describe('removeMarkerPair', () => {
    it('removes bold star markers at cursor', () => {
        const ta = mockTA('**hello**', 5);
        const line = { start: 0, end: 9, text: '**hello**' };
        fmt.removeMarkerPair(ta, '**', '**', line);
        expect(ta.value).toBe('hello');
    });

    it('removes tilde markers at cursor', () => {
        const ta = mockTA('~~hello~~', 5);
        const line = { start: 0, end: 9, text: '~~hello~~' };
        fmt.removeMarkerPair(ta, '~~', '~~', line);
        expect(ta.value).toBe('hello');
    });

    it('removes underscore markers at cursor', () => {
        const ta = mockTA('_hello_', 4);
        const line = { start: 0, end: 7, text: '_hello_' };
        fmt.removeMarkerPair(ta, '_', '_', line);
        expect(ta.value).toBe('hello');
    });

    it('removes backtick markers at cursor', () => {
        const ta = mockTA('`hello`', 3);
        const line = { start: 0, end: 7, text: '`hello`' };
        fmt.removeMarkerPair(ta, '`', '`', line);
        expect(ta.value).toBe('hello');
    });

    it('removes only bold from triple-star text', () => {
        const ta = mockTA('***hello***', 5);
        const line = { start: 0, end: 11, text: '***hello***' };
        fmt.removeMarkerPair(ta, '**', '**', line);
        expect(ta.value).toBe('*hello*');
    });

    it('removes only italic from triple-star text', () => {
        const ta = mockTA('***hello***', 5);
        const line = { start: 0, end: 11, text: '***hello***' };
        fmt.removeMarkerPair(ta, '*', '*', line);
        expect(ta.value).toBe('**hello**');
    });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------
describe('edge cases', () => {
    it('toggleWrap on empty string inserts markers', () => {
        const ta = mockTA('', 0);
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('****');
        expect(ta.selectionStart).toBe(2);
    });

    it('setHeading on empty string', () => {
        const ta = mockTA('', 0);
        fmt.setHeading(ta, 1);
        expect(ta.value).toBe('# ');
    });

    it('togglePrefix on empty string adds prefix', () => {
        const ta = mockTA('', 0, 0);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('- ');
    });

    it('applying bold twice with selection round-trips', () => {
        const ta = mockTA('hello', 0, 5);
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('**hello**');
        // Now remove
        ta.selectionStart = 2;
        ta.selectionEnd = 7;
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('hello');
    });

    it('preserves surrounding text when wrapping', () => {
        const ta = mockTA('aaa bbb ccc', 4, 7); // select "bbb"
        fmt.toggleWrap(ta, ['**', '**']);
        expect(ta.value).toBe('aaa **bbb** ccc');
    });

    it('handles indented bullet list toggle', () => {
        const ta = mockTA('  item', 0, 6);
        fmt.togglePrefix(ta, '- ', 'ul');
        expect(ta.value).toBe('  - item');
    });
});
