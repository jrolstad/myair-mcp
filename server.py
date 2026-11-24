#!/usr/bin/env python3
"""ResMed myAir MCP Server: read-only CPAP therapy data over local stdio."""

import datetime as dt
import logging
import time
from typing import Optional

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

load_dotenv()

mcp = MCPServer("myair")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

# httpx logs full request URLs at INFO, and the Okta authorize URL carries a session token.
logging.getLogger("httpx").setLevel(logging.WARNING)

CACHE_MAX_AGE = 30 * 60  # myAir data only changes once a day, after the machine uploads.
HISTORY_DAYS = 3650
_cache: dict = {}
_client = None


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_MAX_AGE:
        return entry["data"]
    return None


def _cache_set(key: str, data) -> None:
    _cache[key] = {"ts": time.time(), "data": data}


def _get_client():
    global _client
    if _client is None:
        from myair_client import MyAirClient
        _client = MyAirClient()
    return _client


async def _fetch_all_sleep_records() -> list:
    cached = _cache_get("sleep_records")
    if cached is not None:
        return cached
    today = dt.date.today()
    result = await _get_client().get_sleep_records(today - dt.timedelta(days=HISTORY_DAYS), today)
    _cache_set("sleep_records", result)
    return result


@mcp.tool(annotations=READ_ONLY)
async def get_devices() -> dict:
    """
    List the CPAP device(s) and mask on the myAir account.

    Returns `devices` (each with `name`, `manufacturer`, `series`, `family`,
    `serial_number`, `last_sleep_data_report_time`) and `masks` (mask codes).

    `last_sleep_data_report_time` is an ISO-8601 timestamp of the machine's
    last upload. The machine uploads over cellular after a session ends, so if
    it is earlier than this morning, last night's data is not available yet.
    """
    cached = _cache_get("devices")
    if cached is not None:
        return cached
    result = await _get_client().get_devices()
    _cache_set("devices", result)
    return result


@mcp.tool(annotations=READ_ONLY)
async def get_sleep_records(days: int = 30) -> list:
    """
    Get nightly CPAP therapy records for the last `days` nights, newest first.

    Each record:
    - `date`: the therapy night (the evening the session started), YYYY-MM-DD
    - `usage_minutes`: total mask-on time in minutes. Nights with no use are
      included with `usage_minutes: 0`.
    - `ahi`: apnea-hypopnea index, events per hour
    - `leak_95th_l_per_min`: 95th-percentile mask leak in L/min
    - `mask_on_off_count`: number of times the mask was put on/taken off
    - `myair_score`: total myAir score 0-100, the sum of `usage_score`
      (max 70), `ahi_score` (max 5), `mask_score` (max 5) and `leak_score`
      (max 20)

    The full history is fetched once and cached, so any `days` value costs at
    most one API call per 30 minutes.

    Args:
        days: Number of nights back from today to include (minimum 1).
    """
    cutoff = (dt.date.today() - dt.timedelta(days=max(days, 1))).isoformat()
    records = await _fetch_all_sleep_records()
    return [r for r in records if r["date"] >= cutoff]


@mcp.tool(annotations=READ_ONLY)
async def get_latest_sleep() -> Optional[dict]:
    """
    Get the most recent nightly record (same fields as `get_sleep_records`).

    Served from the cached history with no extra API call. Returns None if the
    account has no records. Check `date`: if the machine has not uploaded
    yet this morning, this is an earlier night.
    """
    records = await _fetch_all_sleep_records()
    return records[0] if records else None


@mcp.tool()
async def reset_cache() -> str:
    """Clear cached myAir data and the login token, forcing a fresh login and fetch."""
    global _client
    _cache.clear()
    _client = None
    return "Cache cleared."


if __name__ == "__main__":
    mcp.run()
