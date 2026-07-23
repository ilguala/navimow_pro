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
import urllib.request
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

HOST = "https://api-passport-fra.willand.com"

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


def _post(path: str, params: dict, timeout: int = 20) -> dict:
    body = json.dumps(params).encode()
    req = urllib.request.Request(
        HOST + path, data=body, headers=_signed_headers(path, params), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:  # noqa: PERF203
        try:
            return json.loads(err.read())
        except Exception as inner:  # pragma: no cover - defensive
            raise PassportError(err.code, "HTTP error") from inner


def _extract_tokens(data: dict) -> Tokens:
    return Tokens(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        uuid=str(data.get("uuid") or ""),
        region=str(data.get("region") or "fra"),
    )


def login(username: str, password: str) -> Tokens:
    """POST /v3/user/login -> Tokens. Raises PassportAuthError on bad creds."""
    params = {"username": username, "password": password, "device": DEVICE}
    j = _post("/v3/user/login", params)
    code = str(j.get("resultCode"))
    if code != _RESULT_OK:
        # Wrong email/password and similar user-facing failures.
        raise PassportAuthError(code, str(j.get("resultDesc", "")))
    data = j.get("data") or {}
    tokens = _extract_tokens(data)
    _LOGGER.debug("passport login ok: %s", tokens.redacted())
    return tokens


def refresh(tokens: Tokens) -> Tokens:
    """POST /v3/user/refresh -> new Tokens (perpetual refresh)."""
    params = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "device": DEVICE,
    }
    j = _post("/v3/user/refresh", params)
    code = str(j.get("resultCode"))
    if code != _RESULT_OK:
        raise PassportAuthError(code, str(j.get("resultDesc", "")))
    data = j.get("data") or {}
    new = _extract_tokens(data)
    # Some backends omit uuid/region on refresh -> keep the previous values.
    if not new.uuid:
        new.uuid = tokens.uuid
    if not new.region or new.region == "fra":
        new.region = tokens.region or new.region
    _LOGGER.debug("passport refresh ok: %s", new.redacted())
    return new
