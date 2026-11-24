"""ResMed myAir client.

ResMed has no official API. This wraps the reverse-engineered endpoints used by
the myAir web app (reference: https://github.com/prestomation/resmed_myair_sensors).
Auth is Okta: primary authn -> PKCE authorization code -> token. The access token
goes to an AWS AppSync GraphQL endpoint along with a set of `rmd*` headers.
Only the North America region is supported.
"""

import base64
import datetime as dt
import hashlib
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urldefrag

import httpx
import jwt

TIMEOUT_SECONDS = 15.0

OKTA_URL = "https://resmed-ext-1.okta.com"
AUTH_SERVER_ID = "aus4ccsxvnidQgLmA297"
CLIENT_ID = "0oa4ccq1v413ypROi297"
REDIRECT_URI = "https://myair.resmed.com"
GRAPHQL_URL = "https://graphql.myair-prd.dht.live/graphql"
APPSYNC_API_KEY = "da2-cenztfjrezhwphdqtwtbpqvzui"
PRODUCT = "myAir"

# Static values the myAir web app sends; AppSync rejects requests without them.
HANDSET_HEADERS = {
    "rmdhandsetid": "02c1c662-c289-41fd-a9ae-196ff15b5166",
    "rmdlanguage": "en",
    "rmdhandsetmodel": "Chrome",
    "rmdhandsetosversion": "127.0.6533.119",
    "rmdproduct": PRODUCT,
    "rmdappversion": "1.0.0",
    "rmdhandsetplatform": "Web",
    "accept-language": "en-US,en;q=0.9",
}

SLEEP_RECORDS_QUERY = """query GetPatientSleepRecords {
  getPatientWrapper {
    sleepRecords(startMonth: "%s", endMonth: "%s") {
      items {
        startDate totalUsage sleepScore usageScore ahiScore maskScore
        leakScore ahi maskPairCount leakPercentile
      }
    }
  }
}"""

DEVICES_QUERY = """query getPatientWrapper {
  getPatientWrapper {
    masks { maskCode }
    fgDevices {
      serialNumber localizedName deviceSeries deviceFamily
      lastSleepDataReportTime fgDeviceManufacturerName
    }
  }
}"""


class MyAirError(Exception):
    """Raised when myAir login or a query fails."""


class MyAirAuthError(MyAirError):
    """Raised when Okta or AppSync rejects the credentials or token."""


def _pkce_pair() -> tuple[str, str]:
    verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _normalize_sleep_record(item: dict) -> dict:
    return {
        "date": item.get("startDate"),
        "usage_minutes": item.get("totalUsage"),
        "ahi": item.get("ahi"),
        "leak_95th_l_per_min": item.get("leakPercentile"),
        "mask_on_off_count": item.get("maskPairCount"),
        "myair_score": item.get("sleepScore"),
        "usage_score": item.get("usageScore"),
        "ahi_score": item.get("ahiScore"),
        "mask_score": item.get("maskScore"),
        "leak_score": item.get("leakScore"),
    }


def _normalize_device(item: dict) -> dict:
    return {
        "name": item.get("localizedName"),
        "manufacturer": item.get("fgDeviceManufacturerName"),
        "series": item.get("deviceSeries"),
        "family": item.get("deviceFamily"),
        "serial_number": item.get("serialNumber"),
        "last_sleep_data_report_time": item.get("lastSleepDataReportTime"),
    }


class MyAirClient:
    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        self._username = username or os.environ["MYAIR_USERNAME"]
        self._password = password or os.environ["MYAIR_PASSWORD"]
        self._access_token: Optional[str] = None
        self._access_token_exp: float = 0.0
        self._country: Optional[str] = None

    async def _login(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            f"{OKTA_URL}/api/v1/authn",
            json={"username": self._username, "password": self._password},
            headers={"Accept": "application/json"},
        )
        body = resp.json()
        status = body.get("status")
        if resp.status_code == 401 or status is None:
            raise MyAirAuthError(f"myAir login rejected: {body.get('errorSummary', resp.status_code)}")
        if status == "MFA_REQUIRED":
            raise MyAirAuthError("myAir login requires MFA, which this server does not support")
        if status != "SUCCESS":
            raise MyAirAuthError(f"Unexpected myAir login status: {status}")

        verifier, challenge = _pkce_pair()
        resp = await client.get(
            f"{OKTA_URL}/oauth2/{AUTH_SERVER_ID}/v1/authorize",
            params={
                "client_id": CLIENT_ID,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "none",
                "redirect_uri": REDIRECT_URI,
                "response_mode": "fragment",
                "response_type": "code",
                "sessionToken": body["sessionToken"],
                "scope": "openid profile email",
                "state": "myair-mcp",
            },
        )
        fragment = urldefrag(resp.headers.get("location", "")).fragment
        codes = parse_qs(fragment).get("code")
        if not codes:
            raise MyAirAuthError("myAir authorize step returned no authorization code")

        resp = await client.post(
            f"{OKTA_URL}/oauth2/{AUTH_SERVER_ID}/v1/token",
            data={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
                "code": codes[0],
            },
            headers={"Accept": "application/json"},
        )
        tokens = resp.json()
        if "access_token" not in tokens or "id_token" not in tokens:
            raise MyAirAuthError(f"myAir token exchange failed: {tokens.get('error', resp.status_code)}")

        # Signature verification is skipped: we only read claims from tokens Okta just issued to us.
        id_claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
        access_claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
        country = id_claims.get("myAirCountryId")
        if not country:
            raise MyAirError("myAirCountryId claim missing from id_token")

        self._access_token = tokens["access_token"]
        self._access_token_exp = float(access_claims.get("exp", 0))
        self._country = country

    def _token_valid(self) -> bool:
        return self._access_token is not None and time.time() < self._access_token_exp - 60

    async def _post_graphql(self, client: httpx.AsyncClient, operation: str, query: str) -> httpx.Response:
        return await client.post(
            GRAPHQL_URL,
            json={"operationName": operation, "variables": {}, "query": query},
            headers={
                **HANDSET_HEADERS,
                "x-api-key": APPSYNC_API_KEY,
                "Authorization": f"Bearer {self._access_token}",
                "rmdcountry": self._country or "",
            },
        )

    @staticmethod
    def _is_unauthorized(resp: httpx.Response, body: dict) -> bool:
        if resp.status_code == 401:
            return True
        for err in body.get("errors") or []:
            info = err.get("errorInfo") or {}
            if info.get("errorType") == "unauthorized" or err.get("errorType") == "UnauthorizedException":
                return True
        return False

    async def _query(self, operation: str, query: str) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            if not self._token_valid():
                await self._login(client)
            resp = await self._post_graphql(client, operation, query)
            body = resp.json()
            if self._is_unauthorized(resp, body):
                # One fresh login only: repeated failed logins can lock the Okta account.
                await self._login(client)
                resp = await self._post_graphql(client, operation, query)
                body = resp.json()
                if self._is_unauthorized(resp, body):
                    raise MyAirAuthError(f"myAir rejected a fresh token for {operation}")
        if body.get("errors"):
            messages = [e.get("message", "unknown error") for e in body["errors"]]
            raise MyAirError(f"myAir {operation} failed: {messages}")
        wrapper = (body.get("data") or {}).get("getPatientWrapper")
        if wrapper is None:
            raise MyAirError(f"myAir {operation} returned no getPatientWrapper data")
        return wrapper

    async def get_devices(self) -> dict:
        wrapper = await self._query("getPatientWrapper", DEVICES_QUERY)
        return {
            "devices": [_normalize_device(d) for d in wrapper.get("fgDevices") or []],
            "masks": [m.get("maskCode") for m in wrapper.get("masks") or [] if m.get("maskCode")],
        }

    async def get_sleep_records(self, start: dt.date, end: dt.date) -> list[dict]:
        """Nightly records with start <= date <= end, newest first.

        myAir's startMonth/endMonth arguments only resolve to whole months, so
        the response is filtered down to the exact window here.
        """
        query = SLEEP_RECORDS_QUERY % (start.isoformat(), end.isoformat())
        wrapper = await self._query("GetPatientSleepRecords", query)
        items = (wrapper.get("sleepRecords") or {}).get("items") or []
        records = [
            _normalize_sleep_record(i)
            for i in items
            if i.get("startDate") and start.isoformat() <= i["startDate"] <= end.isoformat()
        ]
        return sorted(records, key=lambda r: r["date"], reverse=True)
