"""Segway/Ninebot passport authentication (standalone, no phone/hooks).

Faithful port of the proven reference (scratchpad/LOGIN_BIND_TEST.py and
navimow_auth/scripts/passport_auth.py).

    sign = SHA256_hex_lower( sorted "k=v&..." join ) over the map
           {app_version, clientKey, os, os_language, os_version, timestamp, url}
           PLUS the request params.
    - url = path only (e.g. "/v3/user/login")
    - clientKey goes in the SIGN but NOT in the headers.
    - headers carry clientId=mowerbot_app_prod.

Synchronous (urllib) on purpose: run off the event loop via an executor.
Never logs tokens or passwords.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..const import (
    ALL_PASSPORT_HOSTS,
    DEFAULT_REGION,
    canonical_region,
    passport_hosts,
)

_LOGGER = logging.getLogger(__name__)

# Kept for backwards compatibility with older entries / callers that pass no host.
DEFAULT_HOST = "api-passport-fra.willand.com"

# App identity (self-consistent; the server validates the sign against these).
CLIENT_ID = "mowerbot_app_prod"
CLIENT_KEY = "830247f0-da96-5c21-8cf0-ca09299795f9"  # app-wide, goes in sign only
APP_VERSION = "402000003"
OS_NAME = "Android"
OS_VERSION = "13"
OS_LANGUAGE = "en"
DEVICE = "ANDROID"

_RESULT_OK = "90000"
# resultCodes that mean the access token is expired / must be refreshed.
RESULT_TOKEN_EXPIRED = {"90015", "90016"}
# The account is not on THIS regional server (each region has its own directory).
RESULT_ACCOUNT_NOT_EXISTS = "00002"


class PassportError(Exception):
    """A passport call returned a non-success resultCode."""

    def __init__(self, code, desc: str) -> None:
        super().__init__(f"{code}: {desc}")
        self.code = str(code)
        self.desc = desc


class PassportAuthError(PassportError):
    """Bad credentials or expired session -- requires user re-auth."""


@dataclass
class Tokens:
    """Passport session tokens."""

    access_token: str
    refresh_token: str
    uuid: str = ""
    region: str = "fra"

    def redacted(self) -> dict:
        """A log-safe view (no token values)."""
        return {
            "access_token": bool(self.access_token),
            "refresh_token": bool(self.refresh_token),
            "uuid_present": bool(self.uuid),
            "region": self.region,
        }


def _sign(m: dict) -> str:
    return hashlib.sha256(
        "&".join(f"{k}={m[k]}" for k in sorted(m)).encode("utf-8")
    ).hexdigest()


def _signed_headers(url: str, req_params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    sign_map = {
        "app_version": APP_VERSION,
        "clientKey": CLIENT_KEY,
        "os": OS_NAME,
        "os_language": OS_LANGUAGE,
        "os_version": OS_VERSION,
        "timestamp": ts,
        "url": url,
    }
    sign_map.update(req_params)
    return {
        "app_version": APP_VERSION,
        "clientId": CLIENT_ID,
        "os": OS_NAME,
        "os_language": OS_LANGUAGE,
        "os_version": OS_VERSION,
        "timestamp": ts,
        "sign": _sign(sign_map),
        "content-type": "application/json",
        "user-agent": "Segway_Mowerbot/4.02.0 (android)",
    }


def _request(
    host: str, path: str, params: dict, *, method: str, timeout: int = 20
) -> dict:
    """Signed passport call against a specific regional host.

    The sign covers the request params, which for a GET are the query string and
    for a POST the JSON body -- nothing else (verified live: adding any extra key
    yields resultCode 90031 "sign invalid").
    """
    headers = _signed_headers(path, params)
    url = f"https://{host}{path}"
    body = None
    if method == "POST":
        body = json.dumps(params).encode()
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:  # noqa: PERF203
        try:
            return json.loads(err.read())
        except Exception as inner:  # pragma: no cover - defensive
            raise PassportError(err.code, "HTTP error") from inner


def _post(path: str, params: dict, host: str = DEFAULT_HOST, timeout: int = 20) -> dict:
    return _request(host, path, params, method="POST", timeout=timeout)


def lookup_region(email: str, hosts: tuple[str, ...] | None = None) -> str | None:
    """Which regional server owns this account? ``None`` if nobody claims it.

    ``GET /v3/region`` needs only the e-mail -- no password -- so the password is
    never offered to a server that does not own the account. Each region keeps its
    own directory: the wrong one answers ``00002 account not exists``, so the owner
    is found by asking each in turn (a closed set of four).
    """
    for host in hosts or ALL_PASSPORT_HOSTS:
        params = {"account": email, "device": DEVICE}
        try:
            j = _request(host, "/v3/region", params, method="GET", timeout=15)
        except (PassportError, OSError) as err:
            _LOGGER.debug("region lookup on %s failed: %s", host, err)
            continue
        code = str(j.get("resultCode"))
        if code == _RESULT_OK:
            region = canonical_region((j.get("data") or {}).get("region"))
            _LOGGER.debug("region lookup: %s owns this account -> %s", host, region)
            return region
        if code != RESULT_ACCOUNT_NOT_EXISTS:
            _LOGGER.debug("region lookup on %s: %s %s", host, code, j.get("resultDesc"))
    return None


def _extract_tokens(data: dict) -> Tokens:
    # Region left EMPTY when absent -- the caller knows which server answered and
    # must not have "fra" silently invented for it.
    raw_region = data.get("region")
    return Tokens(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        uuid=str(data.get("uuid") or ""),
        region=canonical_region(raw_region) if raw_region else "",
    )


def login(username: str, password: str, region: str | None = None) -> Tokens:
    """POST /v3/user/login -> Tokens. Raises PassportAuthError on bad creds.

    ``region`` selects the regional server; when omitted it is resolved from the
    account first (see :func:`lookup_region`). The region's hosts are tried in
    order so a single dead endpoint does not break login.
    """
    if not region:
        region = lookup_region(username) or DEFAULT_REGION
    hosts = passport_hosts(region)
    params = {"username": username, "password": password, "device": DEVICE}
    last: PassportAuthError | None = None
    for host in hosts:
        j = _post("/v3/user/login", params, host)
        code = str(j.get("resultCode"))
        if code == _RESULT_OK:
            data = j.get("data") or {}
            tokens = _extract_tokens(data)
            # Trust the region we actually authenticated against: some backends
            # echo a stale/absent value.
            tokens.region = canonical_region(tokens.region) or canonical_region(region)
            _LOGGER.debug("passport login ok on %s: %s", host, tokens.redacted())
            return tokens
        last = PassportAuthError(code, str(j.get("resultDesc", "")))
        # Only "not here" is worth retrying elsewhere; bad credentials are final.
        if code != RESULT_ACCOUNT_NOT_EXISTS:
            raise last
    raise last or PassportAuthError("unknown", "login failed")


def refresh(tokens: Tokens, region: str | None = None) -> Tokens:
    """POST /v3/user/refresh -> new Tokens (perpetual refresh)."""
    params = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "device": DEVICE,
    }
    host = passport_hosts(region or tokens.region)[0]
    j = _post("/v3/user/refresh", params, host)
    code = str(j.get("resultCode"))
    if code != _RESULT_OK:
        raise PassportAuthError(code, str(j.get("resultDesc", "")))
    data = j.get("data") or {}
    new = _extract_tokens(data)
    # Some backends omit uuid/region on refresh -> keep the previous values.
    if not new.uuid:
        new.uuid = tokens.uuid
    if not new.region:
        new.region = canonical_region(region or tokens.region)
    _LOGGER.debug("passport refresh ok: %s", new.redacted())
    return new
