"""The state client and Socket.IO must use the same TLS, non-cluster endpoint."""
from urllib.parse import quote

import pytest
from redis.asyncio.connection import SSLConnection

from app.config import Settings
from app.main import _build_state_redis, build_app


@pytest.mark.parametrize(
    ("host", "port"),
    [("example.westus.redis.azure.net", 10000), ("example.redis.cache.windows.net", 6380)],
)
async def test_tls_connection_url(monkeypatch, host, port):
    password = "a+b/c=d@e"
    url = f"rediss://:{quote(password, safe='')}@{host}:{port}/0"
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(redis_url=url))
    state_client = _build_state_redis(url)
    _, sio, _ = build_app()
    sio.manager._redis_connect()
    pubsub_client = sio.manager.redis
    try:
        for client in (state_client, pubsub_client):
            pool = client.connection_pool
            assert pool.connection_class is SSLConnection
            assert pool.connection_kwargs["host"] == host
            assert pool.connection_kwargs["port"] == port
            assert pool.connection_kwargs["password"] == password
            assert pool.connection_kwargs["db"] == 0
        assert state_client.connection_pool.max_connections == 10
        assert "socket_timeout" not in pubsub_client.connection_pool.connection_kwargs
    finally:
        await sio.manager.pubsub.aclose()
        await state_client.aclose()
        await pubsub_client.aclose()
