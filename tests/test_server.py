"""Tests for the FastMCP server tools (cache + windowing)."""

import datetime as dt
import time
from unittest.mock import AsyncMock, patch

import pytest

import server


def _days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


RECORDS = [
    {"date": _days_ago(1), "usage_minutes": 420},
    {"date": _days_ago(2), "usage_minutes": 0},
    {"date": _days_ago(10), "usage_minutes": 390},
    {"date": _days_ago(40), "usage_minutes": 400},
]


@pytest.fixture(autouse=True)
def reset_state():
    server._cache.clear()
    server._client = None
    yield
    server._cache.clear()
    server._client = None


@pytest.fixture
def mock_client():
    instance = AsyncMock()
    instance.get_sleep_records.return_value = RECORDS
    instance.get_devices.return_value = {"devices": [{"name": "AirSense 11"}], "masks": ["F30"]}
    with patch("myair_client.MyAirClient", return_value=instance) as ctor:
        yield ctor, instance


async def test_get_sleep_records_filters_by_days(mock_client):
    result = await server.get_sleep_records(days=7)
    assert [r["date"] for r in result] == [_days_ago(1), _days_ago(2)]


async def test_get_sleep_records_minimum_one_day(mock_client):
    result = await server.get_sleep_records(days=0)
    assert [r["date"] for r in result] == [_days_ago(1)]


async def test_history_fetched_once_for_different_windows(mock_client):
    _ctor, instance = mock_client
    await server.get_sleep_records(days=7)
    await server.get_sleep_records(days=365)
    await server.get_latest_sleep()
    assert instance.get_sleep_records.await_count == 1


async def test_get_latest_sleep_returns_newest(mock_client):
    assert (await server.get_latest_sleep())["date"] == _days_ago(1)


async def test_get_latest_sleep_none_when_empty(mock_client):
    _ctor, instance = mock_client
    instance.get_sleep_records.return_value = []
    assert await server.get_latest_sleep() is None


async def test_client_reused_across_tools(mock_client):
    ctor, _instance = mock_client
    await server.get_devices()
    await server.get_sleep_records()
    assert ctor.call_count == 1


async def test_cache_expires_after_max_age(mock_client):
    _ctor, instance = mock_client
    await server.get_devices()
    server._cache["devices"]["ts"] = time.time() - server.CACHE_MAX_AGE - 1
    await server.get_devices()
    assert instance.get_devices.await_count == 2


async def test_reset_cache_clears_data_and_client(mock_client):
    ctor, instance = mock_client
    await server.get_devices()
    assert await server.reset_cache() == "Cache cleared."
    await server.get_devices()
    assert instance.get_devices.await_count == 2
    assert ctor.call_count == 2
