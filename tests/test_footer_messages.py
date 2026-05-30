"""Tests for the footer message feature."""
import time

import pytest

import CTS_Scoreboard as cts
from CTS_Scoreboard import settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_footer_state():
    """Snapshot/restore footer_messages global state around each test."""
    original = list(settings.get('footer_messages') or [])
    original_event = cts.last_event_sent
    settings['footer_messages'] = []
    yield
    settings['footer_messages'] = original
    cts.last_event_sent = original_event
    try:
        cts.save_settings()
    except Exception:
        pass


def _make_msg(id='m1', text='hello', is_default=False,
              genders=None, distances=None, strokes=None, age_groups=None,
              created_at=None, align='left'):
    return {
        'id': id,
        'text': text,
        'align': align,
        'is_default': is_default,
        'genders': genders or [],
        'distances': distances or [],
        'strokes': strokes or [],
        'age_groups': age_groups or [],
        'created_at': created_at if created_at is not None else time.time(),
    }


def _meta(stroke_code=1, distance=50, sex_codes=(2,), age_min=11, age_max=12):
    return {
        'stroke_code': stroke_code,
        'distance': distance,
        'relay': False,
        'age_min': age_min,
        'age_max': age_max,
        'sex_codes': list(sex_codes),
        'is_mixed': len(sex_codes) > 1,
        'gender_age': None,
    }


# ---------------------------------------------------------------------------
# _event_matches_footer
# ---------------------------------------------------------------------------

class TestEventMatchesFooter:
    def test_empty_selectors_match_anything(self):
        assert cts._event_matches_footer(_make_msg(), _meta()) is True

    def test_gender_match(self):
        m = _make_msg(genders=['Female'])
        assert cts._event_matches_footer(m, _meta(sex_codes=(2,))) is True
        assert cts._event_matches_footer(m, _meta(sex_codes=(1,))) is False

    def test_gender_mixed(self):
        m = _make_msg(genders=['Mixed'])
        assert cts._event_matches_footer(m, _meta(sex_codes=(1, 2))) is True
        assert cts._event_matches_footer(m, _meta(sex_codes=(1,))) is False

    def test_gender_or_within_category(self):
        m = _make_msg(genders=['Female', 'Male'])
        assert cts._event_matches_footer(m, _meta(sex_codes=(1,))) is True
        assert cts._event_matches_footer(m, _meta(sex_codes=(2,))) is True
        assert cts._event_matches_footer(m, _meta(sex_codes=(1, 2))) is False

    def test_distance_match(self):
        m = _make_msg(distances=[25])
        assert cts._event_matches_footer(m, _meta(distance=25)) is True
        assert cts._event_matches_footer(m, _meta(distance=50)) is False

    def test_stroke_match(self):
        m = _make_msg(strokes=['Freestyle'])
        assert cts._event_matches_footer(m, _meta(stroke_code=1)) is True
        assert cts._event_matches_footer(m, _meta(stroke_code=2)) is False

    def test_age_group_overlap_within(self):
        m = _make_msg(age_groups=['11-12'])
        assert cts._event_matches_footer(m, _meta(age_min=11, age_max=12)) is True

    def test_age_group_overlap_partial(self):
        m = _make_msg(age_groups=['11-12'])
        # Event spans 11-14 — overlaps with 11-12.
        assert cts._event_matches_footer(m, _meta(age_min=11, age_max=14)) is True

    def test_age_group_no_overlap(self):
        m = _make_msg(age_groups=['8-Under'])
        assert cts._event_matches_footer(m, _meta(age_min=11, age_max=12)) is False

    def test_age_group_open_matches_all(self):
        m = _make_msg(age_groups=['Open'])
        assert cts._event_matches_footer(m, _meta(age_min=11, age_max=12)) is True
        assert cts._event_matches_footer(m, _meta(age_min=None, age_max=None)) is True

    def test_age_group_8_under_with_no_min(self):
        # An "8 & Under" event has age_min=None, age_max=8.
        m = _make_msg(age_groups=['8-Under'])
        assert cts._event_matches_footer(m, _meta(age_min=None, age_max=8)) is True

    def test_and_across_categories(self):
        # Both must match.
        m = _make_msg(genders=['Female'], distances=[25])
        assert cts._event_matches_footer(
            m, _meta(sex_codes=(2,), distance=25)) is True
        assert cts._event_matches_footer(
            m, _meta(sex_codes=(2,), distance=50)) is False
        assert cts._event_matches_footer(
            m, _meta(sex_codes=(1,), distance=25)) is False

    def test_none_meta_returns_false(self):
        assert cts._event_matches_footer(_make_msg(genders=['Female']), None) is False


# ---------------------------------------------------------------------------
# _select_footer_message
# ---------------------------------------------------------------------------

class TestSelectFooterMessage:
    def test_no_messages_returns_none(self, clean_footer_state):
        assert cts._select_footer_message(_meta()) is None

    def test_no_match_no_default_returns_none(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(id='a', distances=[25]),
        ]
        assert cts._select_footer_message(_meta(distance=50)) is None

    def test_default_fallback(self, clean_footer_state):
        d = _make_msg(id='def', text='default!', is_default=True)
        settings['footer_messages'] = [d]
        result = cts._select_footer_message(_meta())
        assert result['id'] == 'def'

    def test_specific_beats_default(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(id='def', is_default=True, created_at=100),
            _make_msg(id='spec', distances=[25], created_at=50),
        ]
        result = cts._select_footer_message(_meta(distance=25))
        assert result['id'] == 'spec'

    def test_more_specific_wins(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(id='one', distances=[25], created_at=200),
            _make_msg(id='two', distances=[25], genders=['Female'], created_at=50),
        ]
        result = cts._select_footer_message(_meta(distance=25, sex_codes=(2,)))
        assert result['id'] == 'two'

    def test_tie_broken_by_most_recent(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(id='old', distances=[25], created_at=100),
            _make_msg(id='new', distances=[25], created_at=200),
        ]
        result = cts._select_footer_message(_meta(distance=25))
        assert result['id'] == 'new'

    def test_multiple_defaults_most_recent_wins(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(id='d1', is_default=True, created_at=100),
            _make_msg(id='d2', is_default=True, created_at=200),
        ]
        result = cts._select_footer_message(_meta())
        assert result['id'] == 'd2'


# ---------------------------------------------------------------------------
# _render_footer_message_html
# ---------------------------------------------------------------------------

class TestRenderFooterMessageHtml:
    def test_none_returns_empty(self):
        assert cts._render_footer_message_html(None) == ''

    def test_empty_text_returns_empty(self):
        assert cts._render_footer_message_html(_make_msg(text='')) == ''

    def test_basic_render(self):
        html = cts._render_footer_message_html(_make_msg(text='hello'))
        assert 'hello' in html
        assert 'scoreboard-footer-message' in html
        assert 'align-left' in html

    def test_align_class(self):
        html = cts._render_footer_message_html(_make_msg(text='x', align='center'))
        assert 'align-center' in html

    def test_markdown_rendered(self):
        html = cts._render_footer_message_html(_make_msg(text='**bold**'))
        assert '<strong>bold</strong>' in html

    def test_qr_token_stripped(self):
        # The [[QR]] token must NOT render an SVG in the footer.
        html = cts._render_footer_message_html(_make_msg(text='before[[QR]]after'))
        assert '[[QR]]' not in html
        assert '<svg' not in html
        assert 'before' in html and 'after' in html


# ---------------------------------------------------------------------------
# _render_and_cache_footer_message
# ---------------------------------------------------------------------------

class TestRenderAndCacheFooterMessage:
    def test_caches_empty_when_no_messages(self, clean_footer_state):
        settings['footer_messages'] = []
        key = cts._render_and_cache_footer_message()
        cached_key, cached_html = cts._cache_get('footer_message')
        assert cached_key == key
        assert cached_html == ''

    def test_caches_rendered_html(self, clean_footer_state):
        settings['footer_messages'] = [
            _make_msg(text='see you later', is_default=True),
        ]
        cts._render_and_cache_footer_message()
        _, cached_html = cts._cache_get('footer_message')
        assert 'see you later' in cached_html


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

class TestFooterMessageRoute:
    def test_empty_returns_200(self, clean_footer_state):
        key = cts._render_and_cache_footer_message()
        with cts.app.test_client() as c:
            resp = c.get('/api/footer-message/' + key)
            assert resp.status_code == 200

    def test_key_round_trip(self, clean_footer_state):
        settings['footer_messages'] = [_make_msg(text='hi', is_default=True)]
        key = cts._render_and_cache_footer_message()
        etag = '"' + key + '"'
        with cts.app.test_client() as c:
            r1 = c.get('/api/footer-message/' + key)
            assert r1.status_code == 200
            assert r1.headers.get('ETag') == etag
            assert b'hi' in r1.data
            # Stale key (key changed) returns 404; client re-fetches via the
            # next update_scoreboard event with the new key.
            r2 = c.get('/api/footer-message/deadbeefdead')
            assert r2.status_code == 404
