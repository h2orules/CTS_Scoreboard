"""Tests for REST API endpoints (/api/qualifying-info, /api/message-page)."""
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
    def test_empty_cache_returns_200_empty(self, client):
        resp = client.get('/api/qualifying-info')
        assert resp.status_code == 200
        assert resp.data == b''

    def test_returns_cached_html(self, client):
        _cache_put('qualifying_info', '<div>qt content</div>')
        resp = client.get('/api/qualifying-info')
        assert resp.status_code == 200
        assert b'qt content' in resp.data
        assert resp.content_type.startswith('text/html')

    def test_etag_header_present(self, client):
        key = _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info')
        assert 'ETag' in resp.headers
        assert resp.headers['ETag'] == '"' + key + '"'

    def test_cache_control_header(self, client):
        _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info')
        assert 'Cache-Control' in resp.headers
        assert 'public' in resp.headers['Cache-Control']
        assert 'max-age=60' in resp.headers['Cache-Control']

    def test_if_none_match_returns_304(self, client):
        key = _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info',
                          headers={'If-None-Match': '"' + key + '"'})
        assert resp.status_code == 304

    def test_if_none_match_wrong_etag_returns_200(self, client):
        _cache_put('qualifying_info', '<div>test</div>')
        resp = client.get('/api/qualifying-info',
                          headers={'If-None-Match': '"wrongkey12ab"'})
        assert resp.status_code == 200
        assert b'test' in resp.data


class TestApiMessagePage:
    def test_empty_cache_returns_200_empty(self, client):
        resp = client.get('/api/message-page/0')
        assert resp.status_code == 200
        assert resp.data == b''

    def test_returns_cached_html(self, client):
        _cache_put('message_page_0', '<h1>Hello</h1>')
        resp = client.get('/api/message-page/0')
        assert resp.status_code == 200
        assert b'Hello' in resp.data

    def test_etag_header_present(self, client):
        key = _cache_put('message_page_0', '<h1>Test</h1>')
        resp = client.get('/api/message-page/0')
        assert resp.headers['ETag'] == '"' + key + '"'

    def test_if_none_match_returns_304(self, client):
        key = _cache_put('message_page_0', '<h1>Test</h1>')
        resp = client.get('/api/message-page/0',
                          headers={'If-None-Match': '"' + key + '"'})
        assert resp.status_code == 304

    def test_if_none_match_wrong_returns_200(self, client):
        _cache_put('message_page_0', '<h1>Test</h1>')
        resp = client.get('/api/message-page/0',
                          headers={'If-None-Match': '"badkey000000"'})
        assert resp.status_code == 200

    def test_second_page_index(self, client):
        _cache_put('message_page_1', '<h1>Page2</h1>')
        resp = client.get('/api/message-page/1')
        assert resp.status_code == 200
        assert b'Page2' in resp.data
