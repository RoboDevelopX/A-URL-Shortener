import json
import logging
import sys

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.logging import JsonFormatter, request_id_var
from app.core.redis import RedisGateway, RedisUnavailableError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = request_id_var.set("req-1")
    try:
        record = logging.makeLogRecord(
            {"name": "t", "levelname": "INFO", "msg": "link_created", "code": "abc"}
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["event"] == "link_created"
    assert payload["request_id"] == "req-1"
    assert payload["code"] == "abc"


def test_json_formatter_serialises_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]


async def test_gateway_opens_circuit_after_failure_and_recovers() -> None:
    clock = FakeClock()
    gateway = RedisGateway(client=None, cooldown_seconds=5, clock=clock)  # type: ignore[arg-type]
    calls = 0

    async def failing(_: object) -> str:
        nonlocal calls
        calls += 1
        raise RedisConnectionError("down")

    async def healthy(_: object) -> str:
        return "ok"

    with pytest.raises(RedisUnavailableError):
        await gateway.run(failing)
    assert gateway.is_open

    # While open, Redis is not touched at all: callers fail fast.
    with pytest.raises(RedisUnavailableError):
        await gateway.run(failing)
    assert calls == 1

    clock.now += 6
    assert await gateway.run(healthy) == "ok"
    assert not gateway.is_open
