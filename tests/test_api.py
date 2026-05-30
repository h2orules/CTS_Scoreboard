"""Tests for REST API endpoints (/api/qualifying-info, /api/message-page).

URLs are content-addressed: the 12-hex SHA-256 key from _cache_put is part
of the path, so responses are immutable and a key mismatch is a 404.
"""
import pytest

from CTS_Scoreboard import app, _cache_put, _content_cache


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def clear_cache():
    _content_cache.clear()
    yield
    _content_cache.clear()


class TestApiQualifyingInfo:
    def test_unknown_key_returns_404(self, client):
        resp = client.get('/api/qualifying-info/000000000000')
        assert resp.status_code == 404

    def test_returns_cached_html_at_current_key(self, client):
        key = _cache_put('qualifying_info', '<div>qt content</div>')
        resp = client.get('/api/qualifying-info/' + key)
        assert resp.status_code == 200
        assert b'qt content' in resp.data
        assert resp.content_type.startswith('text/html')

    def test_immutable_cache_control_header(self, client):
        key = _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info/' + key)
        cc = resp.headers['Cache-Control']
        assert 'public' in cc and 'immutable' in cc and 'max-age=31536000' in cc

    def test_etag_header_matches_key(self, client):
        key = _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info/' + key)
        assert resp.headers['ETag'] == '"' + key + '"'

    def test_stale_key_after_update_returns_404(self, client):
        old_key = _cache_put('qualifying_info', '<div>v1</div>')
        new_key = _cache_put('qualifying_info', '<div>v2</div>')
        assert old_key != new_key
        resp = client.get('/api/qualifying-info/' + old_key)
        assert resp.status_code == 404
        resp = client.get('/api/qualifying-info/' + new_key)
        assert resp.status_code == 200
        assert b'v2' in resp.data


class TestApiMessagePage:
    def test_unknown_key_returns_404(self, client):
        resp = client.get('/api/message-page/0/000000000000')
        assert resp.status_code == 404

    def test_returns_cached_html_at_current_key(self, client):
        key = _cache_put('message_page_0', '<h1>Hello</h1>')
        resp = client.get('/api/message-page/0/' + key)
        assert resp.status_code == 200
        assert b'Hello' in resp.data

    def test_second_page_index(self, client):
        key = _cache_put('message_page_1', '<h1>Page2</h1>')
        resp = client.get('/api/message-page/1/' + key)
        assert resp.status_code == 200
        assert b'Page2' in resp.data


class TestApiFooterMessage:
    def test_unknown_key_returns_404(self, client):
        resp = client.get('/api/footer-message/000000000000')
        assert resp.status_code == 404

    def test_returns_cached_html_at_current_key(self, client):
        key = _cache_put('footer_message', '<span>Go team</span>')
        resp = client.get('/api/footer-message/' + key)
        assert resp.status_code == 200
        assert b'Go team' in resp.data

