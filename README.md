# myair-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that wraps the [ResMed myAir](https://myair.resmed.com) cloud API. Query your CPAP therapy data (nightly usage, AHI, mask leak, myAir score) conversationally from Claude Code or any MCP-compatible client.

**Read-only and local-only.** The server only fetches data, and it runs over stdio only. There is no HTTP transport, because this is health data.

> ResMed has no official public API. This wraps the reverse-engineered endpoints the myAir web app uses. Credit to [prestomation/resmed_myair_sensors](https://github.com/prestomation/resmed_myair_sensors), the canonical reference. The endpoints are unsupported and could change without notice.
>
> The data is informational only. Do not use it for medical decisions; talk to your care provider.

## Tools

| Tool | Description |
|------|-------------|
| `get_devices` | CPAP device(s) and mask: name, series, serial, last data upload time |
| `get_sleep_records(days=30)` | Nightly records for the last `days` nights, newest first |
| `get_latest_sleep` | Most recent night, served from the cached history with no extra API call |
| `reset_cache` | Clear cached data and the login token |

Each nightly record has these fields:

| Field | Meaning |
|-------|---------|
| `date` | Therapy night (the evening the session started), `YYYY-MM-DD` |
| `usage_minutes` | Mask-on time. Unused nights are included with `0` |
| `ahi` | Apnea-hypopnea index (events/hour) |
| `leak_95th_l_per_min` | 95th-percentile mask leak (L/min) |
| `mask_on_off_count` | Times the mask was put on or taken off |
| `myair_score` | Total myAir score (0-100). It is the sum of `usage_score` (max 70), `ahi_score` (5), `mask_score` (5) and `leak_score` (20) |

The machine uploads over cellular after a session ends, so last night's record may not exist until mid-morning. Compare `get_devices` → `last_sleep_data_report_time` with the latest `date`.

## Configuration

```text
MYAIR_USERNAME=your_myair_account_email
MYAIR_PASSWORD=your_myair_account_password
```

Only **North America** accounts are supported. Login uses Okta (primary auth, then a PKCE authorization code, then a token). Accounts that require email MFA (the EU region) are rejected with a clear error. myAir issues no refresh token, so the server logs in again when the 1-hour access token expires. After a failed login it does not retry in a loop, which avoids locking the account.

The full history comes back in a single API call. The server caches it in memory for 30 minutes and slices it per request. Nothing is written to disk.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env  # then fill in credentials
.venv\Scripts\python server.py
```

### Connect to Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "myair": {
      "type": "stdio",
      "command": "C:\\path\\to\\myair-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\myair-mcp\\server.py"],
      "env": {
        "MYAIR_USERNAME": "your_myair_account_email",
        "MYAIR_PASSWORD": "your_myair_account_password"
      }
    }
  }
}
```

## Tests

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

## Project structure

```text
myair_client.py   # Okta login + AppSync GraphQL client
server.py         # MCPServer (mcp 2.x) tool definitions + in-process cache (stdio only)
tests/            # respx-mocked client tests + server cache tests
requirements.txt
.env.example
```
