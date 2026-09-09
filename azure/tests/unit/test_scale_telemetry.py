from unittest.mock import AsyncMock, Mock

import pytest

from app.scale_telemetry import ScaleTelemetry
from app.state import MeetStateStore


@pytest.mark.parametrize("limit", [None, 285000000])
async def test_memory_gauge_omits_unsupported_limit(monkeypatch, limit):
    info = {"used_memory": 1234, "connected_clients": 2}
    if limit is not None:
        info["maxmemory"] = limit
    redis = Mock(info=AsyncMock(return_value=info))
    telemetry = ScaleTelemetry(store=MeetStateStore(redis), redis=redis)
    telemetry.snapshot.maxmemory_bytes = 999
    await telemetry._poll_once()
    assert telemetry.snapshot.maxmemory_bytes == limit

    meter = Mock()
    monkeypatch.setattr("opentelemetry.metrics.get_meter", lambda _name: meter)
    telemetry._register_observable_gauges()
    registration = next(
        call for call in meter.create_observable_gauge.call_args_list
        if call.args[0] == "redis_memory_bytes"
    )
    readings = registration.kwargs["callbacks"][0](None)
    values = {reading.attributes["kind"]: reading.value for reading in readings}
    assert values["used"] == 1234
    if limit is None:
        assert "max" not in values
    else:
        assert values["max"] == limit
