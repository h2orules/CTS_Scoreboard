"""Tests for server-side rendering functions and content cache."""
import re
import pytest

from CTS_Scoreboard import (
    _cache_put,
    _cache_get,
    _content_cache,
    _render_blank_message_html,
    _render_qualifying_html,
    _render_and_cache_message_pages,
    app,
    settings,
)


# ---------------------------------------------------------------------------
# Content cache
# ---------------------------------------------------------------------------
class TestContentCache:
    def setup_method(self):
        _content_cache.clear()

    def test_cache_put_returns_key(self):
        key = _cache_put('test_resource', '<div>hello</div>')
        assert isinstance(key, str)
        assert len(key) == 12

    def test_cache_put_deterministic(self):
        key1 = _cache_put('r1', '<p>same</p>')
        _content_cache.clear()
        key2 = _cache_put('r2', '<p>same</p>')
        assert key1 == key2

    def test_cache_put_different_content_different_key(self):
        key1 = _cache_put('r', '<p>aaa</p>')
        key2 = _cache_put('r', '<p>bbb</p>')
        assert key1 != key2

    def test_cache_get_returns_stored(self):
        _cache_put('res', '<b>data</b>')
        key, html = _cache_get('res')
        assert key is not None
        assert html == '<b>data</b>'

    def test_cache_get_missing_returns_none(self):
        key, html = _cache_get('nonexistent')
        assert key is None
        assert html is None


# ---------------------------------------------------------------------------
# _render_blank_message_html
# ---------------------------------------------------------------------------
class TestRenderBlankMessageHtml:
    def test_none_returns_empty(self):
        assert _render_blank_message_html(None) == ''

    def test_empty_string(self):
        # An empty string is treated as one blank line
        assert _render_blank_message_html('') == '<div class="md-blank"></div>'

    def test_headers(self):
        assert _render_blank_message_html('# Title') == '<h1>Title</h1>'
        assert _render_blank_message_html('## Sub') == '<h2>Sub</h2>'
        assert _render_blank_message_html('### H3') == '<h3>H3</h3>'
        assert _render_blank_message_html('#### H4') == '<h4>H4</h4>'

    def test_bold(self):
        assert '<strong>bold</strong>' in _render_blank_message_html('**bold**')

    def test_italic(self):
        assert '<em>italic</em>' in _render_blank_message_html('*italic*')

    def test_strikethrough(self):
        assert '<s>strike</s>' in _render_blank_message_html('~~strike~~')

    def test_underline(self):
        assert '<u>underline</u>' in _render_blank_message_html('_underline_')

    def test_inline_code(self):
        assert '<code>code</code>' in _render_blank_message_html('`code`')

    def test_unordered_list(self):
        result = _render_blank_message_html('- item1\n- item2')
        assert '<ul>' in result
        assert '<li>item1</li>' in result
        assert '<li>item2</li>' in result

    def test_ordered_list(self):
        result = _render_blank_message_html('1. first\n2. second')
        assert '<ol>' in result
        assert '<li>first</li>' in result

    def test_html_escaping(self):
        result = _render_blank_message_html('<script>alert("xss")</script>')
        assert '<script>' not in result
        assert '&lt;script&gt;' in result

    def test_blank_lines(self):
        result = _render_blank_message_html('line1\n\nline2')
        assert '<div class="md-blank"></div>' in result

    def test_code_protects_from_inline(self):
        result = _render_blank_message_html('`**not bold**`')
        assert '<strong>' not in result
        assert '<code>**not bold**</code>' in result


# ---------------------------------------------------------------------------
# _render_qualifying_html (Jinja partial)
# ---------------------------------------------------------------------------
class TestRenderQualifyingHtml:
    def setup_method(self):
        _content_cache.clear()

    def test_empty_groups_and_records(self):
        with app.app_context():
            key = _render_qualifying_html([], [])
        _, html = _cache_get('qualifying_info')
        assert html.strip() == ''

    def test_single_standards_group(self):
        qt_groups = [{
            'qualifiers': '',
            'items': [{
                'time': '30.00',
                'time_seconds': 30.0,
                'tag': 'A',
                'description': 'A Time',
                'color_class': 'qt-color-0',
                'sex_code': 1,
                'age_min': None,
                'age_max': None,
            }]
        }]
        with app.app_context():
            key = _render_qualifying_html(qt_groups, [])
        _, html = _cache_get('qualifying_info')
        assert 'standards-column' in html
        assert 'qt-color-0' in html
        assert '30.00' in html
        assert '>A<' in html

    def test_records_column_appears(self):
        rec_sets = [{
            'set_name': 'Records',
            'set_team_tag': 'ALL',
            'records': [{
                'time': '25.00',
                'time_seconds': 25.0,
                'color_class': 'rec-color-0',
                'qualifiers': '',
                'sex_code': 1,
                'age_min': None,
                'age_max': None,
                'swimmer_name': 'Test Swimmer',
                'record_team': 'TST',
                'record_year': '2024',
                'relay_names': '',
            }]
        }]
        with app.app_context():
            key = _render_qualifying_html([], rec_sets)
        _, html = _cache_get('qualifying_info')
        assert 'records-column' in html
        assert 'rec-color-0' in html
        assert '25.00' in html

    def test_multiple_groups_show_subheaders(self):
        qt_groups = [
            {
                'qualifiers': 'Boys',
                'items': [{'time': '30.00', 'time_seconds': 30.0, 'tag': 'A',
                           'description': 'A', 'color_class': 'qt-color-0',
                           'sex_code': 1, 'age_min': None, 'age_max': None}]
            },
            {
                'qualifiers': 'Girls',
                'items': [{'time': '32.00', 'time_seconds': 32.0, 'tag': 'A',
                           'description': 'A', 'color_class': 'qt-color-1',
                           'sex_code': 2, 'age_min': None, 'age_max': None}]
            },
        ]
        with app.app_context():
            key = _render_qualifying_html(qt_groups, [])
        _, html = _cache_get('qualifying_info')
        assert 'grid-subheader' in html
        assert 'Boys' in html
        assert 'Girls' in html

    def test_record_year_column(self):
        rec_sets = [{
            'set_name': 'All Time',
            'set_team_tag': 'ALL',
            'records': [{
                'time': '25.00', 'time_seconds': 25.0,
                'color_class': 'rec-color-0', 'qualifiers': '',
                'sex_code': 1, 'age_min': None, 'age_max': None,
                'swimmer_name': 'Fast', 'record_team': '',
                'record_year': '2023', 'relay_names': '',
            }]
        }]
        with app.app_context():
            _render_qualifying_html([], rec_sets)
        _, html = _cache_get('qualifying_info')
        assert 'rec_year' in html
        assert '2023' in html

    def test_team_specific_set_hides_team_column(self):
        rec_sets = [{
            'set_name': 'Team Recs',
            'set_team_tag': 'HW',
            'records': [{
                'time': '26.00', 'time_seconds': 26.0,
                'color_class': 'rec-color-0', 'qualifiers': '',
                'sex_code': 1, 'age_min': None, 'age_max': None,
                'swimmer_name': 'Swimmer', 'record_team': 'HW',
                'record_year': '', 'relay_names': '',
            }]
        }]
        with app.app_context():
            _render_qualifying_html([], rec_sets)
        _, html = _cache_get('qualifying_info')
        assert 'rec_team' not in html

    def test_caching_returns_consistent_key(self):
        qt_groups = [{
            'qualifiers': '',
            'items': [{'time': '30.00', 'time_seconds': 30.0, 'tag': 'A',
                       'description': 'A', 'color_class': 'qt-color-0',
                       'sex_code': 1, 'age_min': None, 'age_max': None}]
        }]
        with app.app_context():
            key1 = _render_qualifying_html(qt_groups, [])
            key2 = _render_qualifying_html(qt_groups, [])
        assert key1 == key2


# ---------------------------------------------------------------------------
# _render_and_cache_message_pages
# ---------------------------------------------------------------------------
class TestRenderAndCacheMessagePages:
    def setup_method(self):
        _content_cache.clear()

    def test_caches_current_settings_pages(self):
        old_pages = settings.get('message_pages', [])
        settings['message_pages'] = [
            {'text': '# Hello', 'align': 'left', 'enabled': True},
            {'text': '## World', 'align': 'center', 'enabled': False},
        ]
        try:
            keys = _render_and_cache_message_pages()
            assert len(keys) == 2
            cached_key_0, html_0 = _cache_get('message_page_0')
            assert cached_key_0 == keys[0]
            assert '<h1>Hello</h1>' in html_0
            cached_key_1, html_1 = _cache_get('message_page_1')
            assert cached_key_1 == keys[1]
            assert '<h2>World</h2>' in html_1
        finally:
            settings['message_pages'] = old_pages
