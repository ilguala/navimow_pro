"""Synchronous client for the Segway Navimow private cloud (p:101).

All business identity (uid, access_token, device_id) travels *inside* the
encrypted payload, never in HTTP headers.

The whole class is synchronous (http.client + `cryptography`). Home Assistant
runs it off the event loop via hass.async_add_executor_job, so the CPU-bound
crypto and blocking IO never touch the loop. A threading.Lock serialises calls
(poll vs. control) that may run in different executor threads and share the
token state *and* the pooled connection.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import ssl
import sys
import threading
import time
from typing import Any

from ..const import (
    DEFAULT_REGION,
    canonical_region,
    encode_partition_ids,
    mower_hosts,
)
from . import crypto, passport
from .passport import PassportAuthError, PassportError, Tokens

_LOGGER = logging.getLogger(__name__)

_HEADERS = {
    "Content-Type": "text/html",
    "ninebot-version": "1",
    # Kept byte-identical to what urllib used to send. Nothing suggests the
    # cloud gates on the User-Agent (it has always accepted a Python one), but
    # this change is meant to alter the connection *lifecycle* and nothing else,
    # so the request on the wire stays exactly as it was.
    "User-Agent": "Python-urllib/%d.%d" % sys.version_info[:2],
}

_TIMEOUT = 30

# A pooled keep-alive socket can be dropped by the far end at any moment, and
# that only surfaces when we try to use it. These are the failures which mean
# *no byte of a response ever arrived*, so re-sending on a fresh connection is
# safe even for a write (urllib3 applies the same rule). A timeout is
# deliberately NOT in here: the server may be slow yet still processing, and
# re-sending a command in that case would be wrong.
_STALE_CONN_ERRORS = (
    http.client.BadStatusLine,  # includes RemoteDisconnected
    http.client.ImproperConnectionState,
    ConnectionResetError,
    BrokenPipeError,
)

# One shared, certificate-verifying TLS context. Building it reads the CA bundle
# from disk, so it is created lazily -- always from an executor thread, never on
# the event loop.
_SSL_CTX: ssl.SSLContext | None = None
_SSL_CTX_LOCK = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CTX  # noqa: PLW0603 - module-level singleton, guarded
    with _SSL_CTX_LOCK:
        if _SSL_CTX is None:
            _SSL_CTX = ssl.create_default_context()
        return _SSL_CTX


CODE_OK = 1
# Business codes that indicate an auth/session problem -> refresh + re-login.
AUTH_ERROR_CODES = {
    90015,
    90016,  # ACCESS_TOKEN_EXPIRES
    401900,  # token empty
    401901,
    401902,
    401903,  # token expired
    401905,  # incorrect user information
    1005,  # logged in from another device
}

# Everything the cloud numbers 4019xx has turned out to be about the session:
# token empty, expired, wrong user. Listing them one at a time has now stranded
# a user once -- 401903 was simply missing, so the session was never refreshed
# and the integration sat in a retry loop that reloading could not clear. Treat
# the whole family as auth, so the next gap in their numbering costs at most one
# needless refresh instead of a dead integration.
_AUTH_CODE_FAMILY = "4019"


def is_auth_error(code: Any) -> bool:
    """Whether a business code means "this session is no longer good"."""
    if code in AUTH_ERROR_CODES:
        return True
    text = str(code)
    return len(text) == 6 and text.startswith(_AUTH_CODE_FAMILY) and text.isdigit()


class NavimowError(Exception):
    """A p:101 business call returned a non-success code."""

    def __init__(self, code: Any, desc: str = "") -> None:
        super().__init__(f"{code}: {desc}")
        self.code = code
        self.desc = desc


class NavimowAuthError(NavimowError):
    """Auth/session problem (token expired, wrong uid, kicked, bad creds)."""


class NavimowCloudClient:
    """One account session against the Navimow private cloud.

    Parameters
    ----------
    device_id:
        Home Assistant's OWN device id (uuid4 hex, 32 chars), generated once at
        install and PERSISTED. It must stay stable -- changing it re-registers
        the session and can kick other sessions.
    tokens:
        Passport tokens. May be empty for a fresh config flow (then call
        ``authenticate`` first).
    uid:
        Mowerbot user id (from a prior user/user/login). Empty on first run.
    region / language:
        From passport login; language is needed by get-vehicle-config.
    """

    def __init__(
        self,
        device_id: str,
        *,
        tokens: Tokens | None = None,
        uid: str = "",
        region: str = DEFAULT_REGION,
        language: str = "en",
        host: str | None = None,
    ) -> None:
        self._device_id = device_id
        self._tokens = tokens or Tokens("", "")
        self._uid = uid
        self._region = canonical_region(region)
        self._language = language or "en"
        # The mower cloud is regional too. ``host`` is the one resolved at setup
        # time (persisted in the entry); without it fall back to the region's
        # first candidate.
        self._host = host or mower_hosts(self._region)[0]
        self._lock = threading.RLock()
        # Pooled HTTPS connection (see _post). ``_conn_host`` records the host it
        # was opened to, so a region/host change drops it instead of talking to
        # the wrong cloud.
        self._conn: http.client.HTTPSConnection | None = None
        self._conn_host = ""

    # ------------------------------------------------------------------ state
    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def tokens(self) -> Tokens:
        return self._tokens

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def region(self) -> str:
        return self._region

    @property
    def host(self) -> str:
        """The mower-cloud host in use (persist it so runtime is deterministic)."""
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        self._host = value

    def session_state(self) -> dict[str, str]:
        """Log-safe-to-persist snapshot of the mutable session state."""
        return {
            "access_token": self._tokens.access_token,
            "refresh_token": self._tokens.refresh_token,
            "uid": self._uid,
            "region": self._tokens.region or self._region,
            "host": self._host,
        }

    # ------------------------------------------------------------------ auth
    def authenticate(self, email: str, password: str, region: str | None = None) -> Tokens:
        """Passport login with email/password. Stores and returns tokens.

        ``region`` pins the regional server; omitted, it is resolved from the
        account. The mower host follows the resolved region unless one was pinned
        explicitly at construction.
        """
        self._tokens = passport.login(email, password, region)
        new_region = canonical_region(self._tokens.region or region or self._region)
        if new_region != self._region:
            self._region = new_region
            self._host = mower_hosts(new_region)[0]
        return self._tokens

    def refresh_session(self) -> Tokens:
        """Refresh passport tokens via the refresh_token (perpetual)."""
        self._tokens = passport.refresh(self._tokens, self._region)
        self._region = canonical_region(self._tokens.region or self._region)
        return self._tokens

    def _common_params(self, access_token: str = "", uid: str = "") -> dict:
        """The 8 'common' params the app sends on user/user/login."""
        return {
            "manufacturer": "samsung_samsung_SM-G930F",
            "systemVersion": passport.OS_VERSION,
            "platform": "and",
            "uid": uid,
            "device_id": self._device_id,
            "client_ver": passport.APP_VERSION,
            "language": self._language,
            "access_token": access_token,
        }

    @staticmethod
    def _add_checkcode(body: dict) -> dict:
        b = dict(body)
        b["serviceTime"] = int(time.time() * 1000)
        b["nonce"] = hashlib.sha256(os.urandom(16)).hexdigest()[:32]
        b["checkcode"] = (
            hashlib.md5(json.dumps(b, separators=(",", ":")).encode())
            .hexdigest()
            .upper()
        )
        return b

    def mower_login(self) -> str:
        """POST /user/user/login to register this device and obtain the uid.

        Tries without the checkcode signature first; falls back
        to the signed variant if the plain one is rejected. Stores and returns
        the uid.
        """
        field4 = {
            "uuid": self._tokens.uuid,
            "token": self._tokens.access_token,
            "refresh_token": self._tokens.refresh_token,
            "region": self._tokens.region or self._region,
        }
        body_a = {**field4, **self._common_params(access_token="")}
        result = self._raw("/user/user/login", body_a)
        uid = self._extract_uid(result)
        if not uid:
            body_b = self._add_checkcode({**field4, **self._common_params(access_token="")})
            result = self._raw("/user/user/login", body_b)
            uid = self._extract_uid(result)
        if not uid:
            code = result.get("code") if isinstance(result, dict) else None
            desc = str(result.get("desc", "")) if isinstance(result, dict) else str(result)
            raise NavimowAuthError(code, f"mower login returned no uid: {desc}")
        self._uid = str(uid)
        _LOGGER.debug("mower login ok (uid acquired)")
        return self._uid

    @staticmethod
    def _extract_uid(result: Any) -> str | None:
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            uid = result["data"].get("uid")
            return str(uid) if uid else None
        return None

    def _reauth(self) -> None:
        """Recover an expired/kicked session: refresh tokens, then re-login."""
        try:
            self.refresh_session()
        except PassportError as err:
            raise NavimowAuthError(err.code, f"passport refresh failed: {err.desc}") from err
        self.mower_login()

    # ------------------------------------------------------------- transport
    def _connection(self) -> tuple[http.client.HTTPSConnection, bool]:
        """The pooled HTTPS connection, and whether it was already open.

        One connection per client, kept alive across calls. Polling makes several
        requests back to back (and, while the mower cuts, does so every few
        seconds), so a fresh TCP+TLS handshake per request was the dominant cost
        of the poll loop -- and the reason the same hostname got resolved over
        and over (GitHub issue #6).
        """
        if self._conn is not None and self._conn_host == self._host:
            return self._conn, True
        self._close_connection()  # host changed (region re-resolved) -> drop it
        self._conn = http.client.HTTPSConnection(
            self._host, timeout=_TIMEOUT, context=_ssl_context()
        )
        self._conn_host = self._host
        return self._conn, False

    def _close_connection(self) -> None:
        """Drop the pooled connection. Never raises."""
        conn, self._conn, self._conn_host = self._conn, None, ""
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - tearing down must not fail a call
                pass

    def close(self) -> None:
        """Release the pooled connection (called when the config entry unloads)."""
        with self._lock:
            self._close_connection()

    def _post(self, path: str, envelope: dict) -> dict:
        data = json.dumps(envelope, separators=(",", ":")).encode()
        # Serialised: one connection cannot carry two requests at once, and a
        # poll and a control command can land in different executor threads.
        # This is the same RLock _call already holds, so it re-enters happily.
        with self._lock:
            for _ in range(2):
                conn, reused = self._connection()
                try:
                    conn.request("POST", path, body=data, headers=_HEADERS)
                    resp = conn.getresponse()
                    # Drain fully: an unread body makes the socket unusable.
                    body = resp.read()
                    if resp.will_close:
                        self._close_connection()
                except _STALE_CONN_ERRORS as err:
                    self._close_connection()
                    if reused:
                        continue  # dead keep-alive socket -> retry once, fresh
                    raise NavimowError("network", str(err)) from err
                except OSError as err:  # DNS, refused, timeout, TLS failure
                    self._close_connection()
                    raise NavimowError("network", str(err)) from err
                except http.client.HTTPException as err:  # truncated/garbled
                    self._close_connection()
                    raise NavimowError("network", str(err)) from err

                try:
                    return json.loads(body)
                except ValueError as err:
                    # The cloud answers *business* errors with a JSON body (and
                    # usually a 200), so a body that isn't JSON at all means a
                    # genuine HTTP-level failure.
                    raise NavimowError(resp.status, "HTTP error") from err
            # Only reachable if a reused connection died and the retry loop fell
            # through, which the `reused` guard above already prevents.
            raise NavimowError("network", "connection lost")

    def _raw(self, path: str, business: dict) -> dict:
        """Pack + POST + decode a single call, no auth handling."""
        return crypto.decode_response(self._post(path, crypto.pack(business)))

    def _auth_body(self, extra: dict | None) -> dict:
        # Send the FULL common-parameter set the app sends on every call
        # (uid, access_token, device_id, client_ver, platform, language,
        # systemVersion, manufacturer). auth-list returns an EMPTY list with
        # only the minimal 3 fields, but returns owned + shared vehicles with
        # the full set. (checkcode/serviceTime/nonce are NOT
        # required.) Other reads tolerate the minimal body, but sending the full
        # set matches the app and is what makes discovery work.
        body = self._common_params(access_token=self._tokens.access_token, uid=self._uid)
        if extra:
            body.update(extra)
        return body

    def call(
        self,
        path: str,
        extra: dict | None = None,
        *,
        auth: bool = True,
        retry_auth: bool = True,
    ) -> Any:
        """Encrypted call returning the ``data`` payload on success.

        Injects the auth identity (uid, access_token, device_id). On an
        auth/expiry code it refreshes tokens, re-runs mower_login and retries
        once. Raises NavimowError / NavimowAuthError otherwise.
        """
        with self._lock:
            if auth and not self._uid:
                # No session yet (e.g. after a restart with only tokens stored).
                self._reauth()
            body = self._auth_body(extra) if auth else dict(extra or {})
            result = self._raw(path, body)
            code = result.get("code") if isinstance(result, dict) else None
            if code == CODE_OK:
                return result.get("data")

            if retry_auth and auth and is_auth_error(code):
                _LOGGER.debug("auth code %s on %s -> re-auth + retry", code, path)
                self._reauth()
                body = self._auth_body(extra)
                result = self._raw(path, body)
                if isinstance(result, dict) and result.get("code") == CODE_OK:
                    return result.get("data")

            desc = str(result.get("desc", "")) if isinstance(result, dict) else str(result)
            if is_auth_error(code):
                raise NavimowAuthError(code, desc)
            raise NavimowError(code, desc)

    # ----------------------------------------------------------------- reads
    def auth_list(self) -> list[dict]:
        """Vehicles on this account. Learns uid from auth_uid if not set."""
        data = self.call("/vehicle/vehicle/auth-list", {})
        items = data if isinstance(data, list) else (data or {}).get("list") or []
        if items and not self._uid:
            uid = items[0].get("auth_uid")
            if uid:
                self._uid = str(uid)
        return items

    def index2(self, sn: str) -> dict:
        return self.call("/vehicle/vehicle/index2", {"vehicle_sn": sn}) or {}

    def device_info(self, sn: str) -> dict:
        return self.call("/vehicle/vehicle/get-device-info", {"vehicle_sn": sn}) or {}

    def set_list(self, sn: str) -> dict:
        return self.call("/vehicle/vehicle/set-list", {"vehicle_sn": sn}) or {}

    def vehicle_config(self, sn: str) -> dict:
        return self.call(
            "/vehicle/vehicle/get-vehicle-config",
            {"vehicle_sn": sn, "language": self._language},
        ) or {}

    def today_plan(self, sn: str, vehicle_type: int) -> dict:
        return self.call(
            "/vehicle/vehicle/get-today-plan",
            {"vehicle_sn": sn, "vehicle_type": vehicle_type},
        ) or {}

    def location(self, sn: str, vehicle_type: int) -> dict:
        return self.call(
            "/vehicle/vehicle/get-location",
            {"vehicle_sn": sn, "vehicle_type": vehicle_type},
        ) or {}

    def errors(self, sn: str, vehicle_type: int) -> dict:
        return self.call(
            "/vehicle/vehicle/get-hint-error-compress",
            {"vehicle_sn": sn, "vehicle_type": vehicle_type},
        ) or {}

    def maintenance(self, sn: str) -> dict:
        return self.call(
            "/vehicle/vehicle/get-component-maintenance", {"vehicle_sn": sn}
        ) or {}

    def map_list(self, sn: str) -> Any:
        return self.call("/map/index/map-list", {"vehicle_sn": sn})

    def path_info_time(self, sn: str) -> list:
        """Per-zone mowing coverage for the current/last session.

        Returns ``[{partitionId, area, finishedArea, partitionPercentage,
        startTime, endTime, endTimeAlias}, ...]`` -- the authoritative mowed
        amount per zone (works while docked too). This is the
        coverage source; the swept-path geometry (get-path-info-data-compress)
        is not reachable standalone, so the trail is reconstructed from position.
        """
        data = self.call("/vehicle/trail/get-path-info-time", {"vehicle_sn": sn})
        return data if isinstance(data, list) else []

    def map_detail_plain(self, sn: str, map_id: str, map_base_id: str) -> Any:
        """Uncompressed map detail: returns DeviceMapInfo with a `map_detail`
        JSON *string* (the full geometry) -- NO zstd needed. Preferred source."""
        return self.call(
            "/map/index/map-detail",
            {"vehicle_sn": sn, "map_id": map_id, "map_base_id": map_base_id},
        )

    def map_detail(self, sn: str, map_id: str, map_base_id: str) -> Any:
        """Compressed map detail: `data` is a base64 ZSTD blob (fallback only --
        needs a zstd decoder, which the HA host may lack)."""
        return self.call(
            "/map/index/map-detail-compress",
            {"vehicle_sn": sn, "map_id": map_id, "map_base_id": map_base_id},
        )

    def station_map(self, sn: str, map_id: str, map_base_id: str) -> dict:
        """Dock/approach path (get-station-map). ``data`` is a JSON *string*.

        Returns the parsed dict ``{points:[[x,y,type],...], start_from_pile}`` or
        an empty dict. NOTE: its coordinates are in a docking-local frame, not the
        map world frame used by map-detail.
        """
        data = self.call(
            "/map/index/get-station-map",
            {"vehicle_sn": sn, "map_id": map_id, "map_base_id": map_base_id},
        )
        if isinstance(data, str):
            try:
                return json.loads(data)
            except (ValueError, TypeError):
                return {}
        return data or {}

    # -------------------------------------------------------------- controls
    def mow_zones(self, sn: str, partition_ids_hex: str, partition_setup: int) -> dict:
        """Start mowing the selected zones (cmdCode s:mower). Returns cmd_num data."""
        return self.call(
            "/vehicle/set/send",
            {
                "vehicle_sn": sn,
                "cmdCode": "s:mower",
                "data": {"partitionSetup": partition_setup, "partitionIds": partition_ids_hex},
            },
        )

    def _behavior(self, sn: str, type_int: int) -> dict:
        # BOTH top-level string `type` AND nested int `data.type` are required.
        return self.call(
            "/vehicle/set/send",
            {
                "vehicle_sn": sn,
                "cmdCode": "c:behavior",
                "type": str(type_int),
                "data": {"type": type_int},
            },
        )

    def pause(self, sn: str) -> dict:
        return self._behavior(sn, 1)

    def dock(self, sn: str) -> dict:
        return self._behavior(sn, 2)

    def resume(self, sn: str) -> dict:
        return self._behavior(sn, 3)

    def command_status(self, sn: str, cmd_num: str) -> dict:
        """Poll a set/send or set/index command outcome."""
        return self.call(
            "/vehicle/set/response", {"vehicle_sn": sn, "cmd_num": cmd_num}
        ) or {}

    # -------------------------------------------------------------- settings
    def save_setting(self, sn: str, data: dict) -> Any:
        """Write a partial settings object via save-set-data (camelCase keys)."""
        return self.call("/vehicle/set/save-set-data", {"vehicle_sn": sn, "data": data})

    def set_bool_setting(self, sn: str, write_key: str, on: bool) -> Any:
        """Write a boolean setting as a zero-padded string ('01'/'00').

        The pattern nightMowSwitch uses; other keys follow the same encoding
        (best-effort -- see README).
        """
        return self.save_setting(sn, {write_key: "01" if on else "00"})

    def save_setting_iot(self, sn: str, vehicle_type: Any, data: dict) -> Any:
        """Write settings keys via save-set-data + operation_type 'iot_set'.

        Required for the "modern" settings (childLock/liftSwitch/mowingCycle/
        frostSwitch/snowSwitch/stormSwitch/highTempSwitch/returnBatteryLevel/
        chargingLimit): the plain :meth:`save_setting` form is acked (code:1) but
        NOT applied for these keys.
        """
        return self.call(
            "/vehicle/set/save-set-data",
            {
                "vehicle_sn": sn,
                "vehicle_type": str(vehicle_type),
                "data": data,
                "operation_type": "iot_set",
            },
        )

    def set_iot_bool(
        self, sn: str, vehicle_type: Any, write_key: str, on: bool, numeric: bool = False
    ) -> Any:
        """Boolean "modern" setting. Per-key encoding matches the app: some keys
        take a JSON number (1/0), others a string ('1'/'0')."""
        value: Any = (1 if on else 0) if numeric else ("1" if on else "0")
        return self.save_setting_iot(sn, vehicle_type, {write_key: value})

    def send_setting_device(self, sn: str, robot_data: dict) -> Any:
        """Push a settings change straight to the robot.

        Device command ``cmdCode="s:mower"`` on ``/vehicle/set/send`` (the
        control endpoint, same as mow/dock), ``data`` a JSON *string* -- exactly
        the form the app fires alongside every settings write. The cloud
        (``iot_set``) copy alone is acked but the robot does NOT apply it (it
        reverts to its onboard value), so this command is what actually takes
        effect. The per-key value encoding
        differs from the cloud form and lives in the switch/select descriptions.
        Refused (5001) while the mower is running, same as the app.
        """
        return self.call(
            "/vehicle/set/send",
            {
                "vehicle_sn": sn,
                "cmdCode": "s:mower",
                "data": json.dumps(robot_data, separators=(",", ":")),
            },
        )

    def set_night_mow(self, sn: str, on: bool) -> Any:
        return self.set_bool_setting(sn, "nightMowSwitch", on)

    @staticmethod
    def _partition_plan_hex(day: int, enabled: bool, periods: list[dict]) -> str:
        """Per-day plan encoded for the ``s:mower`` device command.

        Byte layout::

            01 <day> <open> <n_periods> [ <start> <end> <n_zones> <zone_id>… ]…

        Single bytes except the zone ids, which are **little-endian uint16** —
        the same encoding as :func:`encode_partition_ids` uses everywhere else.
        start/end are 15-minute slots from 00:00; the leading ``01`` means one day
        per command; ``n_zones = 0`` means all zones. An OFF day is ``01 <day> 00 00``.

        Writing a zone id as one byte instead of two shifted every following byte,
        which the mower then read as an extra bogus period (a 00:15-00:15 entry
        the app could not delete, since it was created inside the mower).
        """
        out = ["%02X" % (x & 0xFF) for x in (1, int(day), 1 if enabled else 0, len(periods))]
        for p in periods:
            ids = [int(z) for z in (p.get("partition_ids") or [])]
            out.append(
                "%02X%02X%02X" % (int(p["start_time"]) & 0xFF, int(p["end_time"]) & 0xFF, len(ids))
            )
            out.append(encode_partition_ids(ids).upper())
        return "".join(out)

    def set_day_schedule(
        self, sn: str, vehicle_type: Any, day: int, enabled: bool, periods: list[dict]
    ) -> Any:
        """Write one weekday's mowing plan.

        ``day`` is the Navimow weekday number (1=Sun .. 7=Sat). ``periods`` is a
        list of ``{start_min, end_min, zone_ids}`` (minutes from 00:00; empty
        zone_ids => all zones). Mirrors the app: first an immediate device
        command (``cmdCode="s:mower"`` on ``/vehicle/set/send`` -- the
        control endpoint the mow/dock commands use -- carrying the plan bytes
        from :meth:`_partition_plan_hex` as a JSON *string*), then the cloud
        persist (``save-set-data`` + ``operation_type="iot_set"`` with
        ``{partitionPlan<day-1>: {day, open, period:[...]}}``). Robot first:
        while the mower is running the device command is refused (same as the
        app), which correctly aborts before the cloud write. Returns the cloud
        result.
        """
        plan_periods = [
            {
                "start_time": int(p["start_min"]) // 15,
                "end_time": int(p["end_min"]) // 15,
                "partition_ids": [int(z) for z in (p.get("zone_ids") or [])],
            }
            for p in periods
        ]
        key = f"partitionPlan{int(day) - 1}"
        # 1) Immediate command to the robot -- /vehicle/set/send (the
        #    s:mower control endpoint), data as a JSON *string*, as the app sends.
        hex_plan = self._partition_plan_hex(day, enabled, plan_periods)
        self.call(
            "/vehicle/set/send",
            {
                "vehicle_sn": sn,
                "cmdCode": "s:mower",
                "data": json.dumps({key: hex_plan}, separators=(",", ":")),
            },
        )
        # 2) Persist to the cloud (iot_set) -- the copy the app reads back.
        entry = {"day": int(day), "open": 1 if enabled else 0, "period": plan_periods}
        return self.call(
            "/vehicle/set/save-set-data",
            {
                "vehicle_sn": sn,
                "vehicle_type": str(vehicle_type),
                "data": {key: entry},
                "operation_type": "iot_set",
            },
        )
