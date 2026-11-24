"""Tests for the myAir client: Okta login, GraphQL calls, and record shaping."""

import datetime as dt
import json
import time

import httpx
import jwt
import pytest
import respx

import myair_client
from myair_client import MyAirAuthError, MyAirClient, MyAirError

AUTHN = f"{myair_client.OKTA_URL}/api/v1/authn"
AUTHORIZE = f"{myair_client.OKTA_URL}/oauth2/{myair_client.AUTH_SERVER_ID}/v1/authorize"
TOKEN = f"{myair_client.OKTA_URL}/oauth2/{myair_client.AUTH_SERVER_ID}/v1/token"
GRAPHQL = myair_client.GRAPHQL_URL


def _token(claims: dict) -> str:
    return jwt.encode(claims, "test-signing-key-at-least-32-bytes-long", algorithm="HS256")


def _tokens(access_exp_offset: int = 3600) -> dict:
    now = int(time.time())
    return {
        "access_token": _token({"iat": now, "exp": now + access_exp_offset}),
        "id_token": _token({"myAirCountryId": "US"}),
    }


def _mock_login(router: respx.MockRouter, tokens: dict | None = None) -> None:
    router.post(AUTHN).respond(200, json={"status": "SUCCESS", "sessionToken": "sess"})
    router.get(AUTHORIZE).respond(
        302, headers={"location": "https://myair.resmed.com#code=abc&state=myair-mcp"}
    )
    router.post(TOKEN).respond(200, json=tokens or _tokens())


def _sleep_body(items: list[dict]) -> dict:
    return {"data": {"getPatientWrapper": {"sleepRecords": {"items": items}}}}


def _raw_record(date: str, usage: int = 420) -> dict:
    return {
        "startDate": date,
        "totalUsage": usage,
        "sleepScore": 100,
        "usageScore": 70,
        "ahiScore": 5,
        "maskScore": 5,
        "leakScore": 20,
        "ahi": 0.4,
        "maskPairCount": 1,
        "leakPercentile": 13.2,
    }


@pytest.fixture
def client() -> MyAirClient:
    return MyAirClient(username="user@example.com", password="pw")


@respx.mock
async def test_login_then_query_sends_required_headers(client):
    _mock_login(respx)
    gql = respx.post(GRAPHQL).respond(200, json=_sleep_body([]))

    await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))

    headers = gql.calls.last.request.headers
    assert headers["x-api-key"] == myair_client.APPSYNC_API_KEY
    assert headers["rmdcountry"] == "US"
    assert headers["Authorization"].startswith("Bearer ")
    sent = json.loads(gql.calls.last.request.content)
    assert sent["operationName"] == "GetPatientSleepRecords"
    assert 'startMonth: "2026-11-01"' in sent["query"]


@respx.mock
async def test_token_is_reused_across_queries(client):
    _mock_login(respx)
    respx.post(GRAPHQL).respond(200, json=_sleep_body([]))

    await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))
    await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))

    assert respx.routes[0].call_count == 1


@respx.mock
async def test_expired_token_triggers_new_login(client):
    _mock_login(respx, tokens=_tokens(access_exp_offset=30))
    respx.post(GRAPHQL).respond(200, json=_sleep_body([]))

    await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))
    await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))

    assert respx.routes[0].call_count == 2


@respx.mock
async def test_mfa_required_raises_auth_error(client):
    respx.post(AUTHN).respond(200, json={"status": "MFA_REQUIRED", "stateToken": "st"})

    with pytest.raises(MyAirAuthError, match="MFA"):
        await client.get_devices()


@respx.mock
async def test_bad_password_raises_auth_error(client):
    respx.post(AUTHN).respond(401, json={"errorSummary": "Authentication failed"})

    with pytest.raises(MyAirAuthError, match="Authentication failed"):
        await client.get_devices()


@respx.mock
async def test_missing_authorization_code_raises(client):
    respx.post(AUTHN).respond(200, json={"status": "SUCCESS", "sessionToken": "sess"})
    respx.get(AUTHORIZE).respond(
        302, headers={"location": "https://myair.resmed.com#error=login_required"}
    )

    with pytest.raises(MyAirAuthError, match="authorization code"):
        await client.get_devices()


@respx.mock
async def test_unauthorized_graphql_relogs_once_then_succeeds(client):
    _mock_login(respx)
    unauthorized = {"errors": [{"message": "no", "errorInfo": {"errorType": "unauthorized"}}]}
    respx.post(GRAPHQL).mock(
        side_effect=[
            httpx.Response(200, json=unauthorized),
            httpx.Response(200, json=_sleep_body([_raw_record("2026-11-23")])),
        ]
    )

    records = await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24))

    assert len(records) == 1
    assert respx.routes[0].call_count == 2


@respx.mock
async def test_persistent_unauthorized_raises_without_looping(client):
    _mock_login(respx)
    gql = respx.post(GRAPHQL).respond(401, json={"errors": [{"message": "Unauthorized"}]})

    with pytest.raises(MyAirAuthError):
        await client.get_devices()
    assert gql.call_count == 2
    assert respx.routes[0].call_count == 2


@respx.mock
async def test_graphql_errors_raise(client):
    _mock_login(respx)
    respx.post(GRAPHQL).respond(200, json={"errors": [{"message": "boom"}]})

    with pytest.raises(MyAirError, match="boom"):
        await client.get_devices()


@respx.mock
async def test_sleep_records_filtered_to_window_and_sorted_newest_first(client):
    _mock_login(respx)
    items = [
        _raw_record("2026-11-01"),
        _raw_record("2026-11-23"),
        _raw_record("2026-11-10", usage=0),
        _raw_record("2026-11-25"),
    ]
    respx.post(GRAPHQL).respond(200, json=_sleep_body(items))

    records = await client.get_sleep_records(dt.date(2026, 11, 10), dt.date(2026, 11, 24))

    assert [r["date"] for r in records] == ["2026-11-23", "2026-11-10"]
    assert records[1]["usage_minutes"] == 0
    assert records[0] == {
        "date": "2026-11-23",
        "usage_minutes": 420,
        "ahi": 0.4,
        "leak_95th_l_per_min": 13.2,
        "mask_on_off_count": 1,
        "myair_score": 100,
        "usage_score": 70,
        "ahi_score": 5,
        "mask_score": 5,
        "leak_score": 20,
    }


@respx.mock
async def test_null_sleep_items_returns_empty_list(client):
    _mock_login(respx)
    respx.post(GRAPHQL).respond(
        200, json={"data": {"getPatientWrapper": {"sleepRecords": {"items": None}}}}
    )

    assert await client.get_sleep_records(dt.date(2026, 11, 1), dt.date(2026, 11, 24)) == []


@respx.mock
async def test_get_devices_normalizes_fields(client):
    _mock_login(respx)
    respx.post(GRAPHQL).respond(
        200,
        json={
            "data": {
                "getPatientWrapper": {
                    "masks": [{"maskCode": "F30"}, {"maskCode": ""}],
                    "fgDevices": [
                        {
                            "serialNumber": "SN1",
                            "localizedName": "AirSense 11 AutoSet",
                            "deviceSeries": "air11",
                            "deviceFamily": "airSense",
                            "lastSleepDataReportTime": "2026-11-24T13:55:38.000+00:00",
                            "fgDeviceManufacturerName": "ResMed",
                        }
                    ],
                }
            }
        },
    )

    result = await client.get_devices()

    assert result["masks"] == ["F30"]
    assert result["devices"][0]["name"] == "AirSense 11 AutoSet"
    assert result["devices"][0]["last_sleep_data_report_time"] == "2026-11-24T13:55:38.000+00:00"
