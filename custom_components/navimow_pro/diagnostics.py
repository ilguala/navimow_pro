"""Diagnostics for Navimow (Private).

Adds the "Download diagnostics" button to the integration page. The point is to
answer "why is X empty on my model?" without asking a non-technical owner to dig
through logs: the dump carries what their mower actually reports, so an
unfamiliar model can be supported from a single file.

It contains the RAW cloud payloads as well as the parsed snapshot, because the
interesting question is usually whether a field is missing, empty, or simply
named differently on that firmware.

Credentials and identifiers are redacted, and oversized blobs (map geometry,
trails) are summarised rather than dumped, so the file stays readable and safe to
attach to a public issue.
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import NavimowCoordinator

# Anything that identifies the account, the session or the machine. Kept
# deliberately wide: these dumps are meant to be pasted into public issues.
# Exact key names to hide. Kept alongside the word-based rule below because a
# couple of them ("sn", "device_id") are too short or too generic to match on.
TO_REDACT = {
    "access_token",
    "auth_uid",
    "device_id",
    "email",
    "latitude",
    "longitude",
    "password",
    "refresh_token",
    "serial",
    "sn",
    "token",
    "uid",
    "username",
    "uuid",
    "vehicle_sn",
}

# Matching on exact names alone let real data through: the cloud also sends
# ``last_latitude`` / ``last_longitude`` (a user's home, in the clear), plus
# ``pin_code``, ``iccid`` and ``antiTheftPoint`` -- none of which were listed,
# and all of which reached a public issue tracker in files we had called safe to
# attach. We do not control this payload, so any rule keyed to names we have
# already seen will keep losing to the next variant.
#
# The key is split into WORDS (``last_latitude`` -> last, latitude;
# ``antiTheftPoint`` -> anti, theft, point) and hidden if any word is sensitive.
# Words, not substrings: "mapping" contains "pin" and "map_id" contains "id",
# and blanking those would gut the very payload these dumps exist to show.
_SENSITIVE_WORDS = {
    "latitude", "longitude", "lat", "lng", "lon", "gps", "coord", "coords",
    "token", "serial", "sn", "uid", "uuid", "imei", "iccid", "msisdn", "iccids",
    "email", "mail", "username", "password", "passwd", "pwd", "pin", "secret",
    "theft",
}

_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _is_sensitive(key: Any) -> bool:
    """Whether a payload key names something that must not be published."""
    text = str(key)
    if text in TO_REDACT:
        return True
    return any(w.lower() in _SENSITIVE_WORDS for w in _WORD_SPLIT.split(text) if w)


def _redact(value: Any) -> Any:
    """Recursively blank every sensitive key, whatever its nesting."""
    if isinstance(value, dict):
        return {
            k: ("**REDACTED**" if _is_sensitive(k) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


_MAX_STR = 300  # characters kept of any single string value
_MAX_LIST = 8  # items kept of a list of structures (map shapes, trail points)
# A list of plain numbers is cheap to print and is usually the interesting
# part -- abbreviating mowingHeightList to 8 of 11 hid the top of a mower's
# cutting range from the very report sent to reveal it.
_MAX_NUMBER_LIST = 64


def _trim(value: Any, depth: int = 0) -> Any:
    """Shorten blobs and long lists, keeping the shape visible.

    A raw payload carries the compressed map and thousands of trail points; those
    would bury the very thing we want to see (which fields exist and what they
    hold), so they are summarised instead of dumped.
    """
    if depth > 8:
        return "<...>"
    if isinstance(value, str):
        if len(value) > _MAX_STR:
            return f"{value[:_MAX_STR]}... <{len(value)} chars total>"
        return value
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        numeric = all(v is None or isinstance(v, (int, float)) for v in value)
        cap = _MAX_NUMBER_LIST if numeric else _MAX_LIST
        if len(value) > cap:
            head = [_trim(v, depth + 1) for v in value[:cap]]
            return [*head, f"<{len(value) - cap} more of {len(value)} items>"]
        return [_trim(v, depth + 1) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Everything needed to work out why a field is empty on a given model."""
    coordinator: NavimowCoordinator | None = (hass.data.get(DOMAIN) or {}).get(
        entry.entry_id
    )

    data: dict[str, Any] = {
        "entry": {
            "data": _redact(dict(entry.data)),
            "options": dict(entry.options),
        }
    }

    if coordinator is None:
        data["note"] = "integration not loaded; only the stored entry is available"
        return data

    # Which endpoints answered at all -- often the whole story on an older model.
    raw = coordinator.raw_payloads
    data["endpoints"] = {
        key: ("empty" if not value else type(value).__name__) for key, value in raw.items()
    }
    data["raw"] = _redact(_trim(raw))
    data["snapshot"] = _redact(_trim(coordinator.data or {}))
    return data
