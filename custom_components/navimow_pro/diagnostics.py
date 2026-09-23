"""Diagnostics for Navimow (Private).

Adds the "Download diagnostics" button to the integration page. The point is to
answer "why is X empty on my model?" without asking a non-technical owner to dig
through logs: the dump carries what their mower actually reports, so an
unfamiliar model can be supported from a single file.

It contains the RAW cloud payloads as well as the parsed snapshot, because the
interesting question is usually whether a field is missing, empty, or simply
named differently on that firmware.

Credentials and identifiers are redacted -- by key name, by what the value looks
like, and by hunting the secret strings themselves wherever they are echoed under
an innocent key -- and oversized blobs (map geometry, trails) are summarised
rather than dumped, so the file stays readable and safe to attach to a public
issue.
"""
from __future__ import annotations

import json
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


# Hiding by key name is only half the job: the same data also travels INSIDE
# string values, under keys that look perfectly innocent -- a token in a URL, an
# address in a free-text "desc", a whole JSON document stuffed into one string.
# The key rule cannot see any of that.
#
# Each pattern is deliberately narrow. These dumps exist to show what a mower
# reports, so a rule that blanks anything long and unfamiliar destroys the thing
# we are trying to read.
_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AUTH", re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "UUID",
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    ("MAC", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("IPV4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")),
)

# A firmware version reads exactly like an IPv4 address ("1.12.3.40"), and the
# firmware version is one of the fields these reports exist to compare across
# models. Under a key that names a version, leave dotted numbers alone.
_VERSION_WORDS = {"version", "ver", "firmware", "fw", "build", "sw", "hw", "revision"}

# Shortest value worth hunting for as an echo. A 4-digit PIN is already hidden by
# its own key; going looking for "1234" in every other string would corrupt half
# the payload to protect a value that is already gone.
_MIN_ECHO = 6


def _is_version_key(key: Any) -> bool:
    return any(w.lower() in _VERSION_WORDS for w in _WORD_SPLIT.split(str(key)) if w)


def _collect_secrets(value: Any, out: set[str], depth: int = 0) -> None:
    """Gather the actual secret STRINGS, so they can be hunted down elsewhere.

    The serial is hidden under ``vehicle_sn`` and then echoed in the clear inside
    a URL, a topic name or a message body three payloads away. Redacting it in one
    place and publishing it in another is not redaction.
    """
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, val in value.items():
            if _is_sensitive(key):
                if isinstance(val, (str, int)) and len(str(val)) >= _MIN_ECHO:
                    out.add(str(val))
            else:
                _collect_secrets(val, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_secrets(item, out, depth + 1)


def _scrub_text(text: str, secrets: frozenset[str], *, skip_ip: bool = False) -> str:
    """Remove sensitive content found inside one string value."""
    for label, pattern in _VALUE_PATTERNS:
        if skip_ip and label == "IPV4":
            continue
        text = pattern.sub(f"**{label}_REDACTED**", text)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "**REDACTED**")
    return text


def _redact(
    value: Any, secrets: frozenset[str] = frozenset(), key: Any = None, depth: int = 0
) -> Any:
    """Blank every sensitive key, and scrub what hides inside the values."""
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {
            k: (
                "**REDACTED**"
                if _is_sensitive(k)
                else _redact(v, secrets, k, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, secrets, key, depth + 1) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        # A JSON document parked in a string field is still a payload, and its
        # keys deserve the same treatment as any other.
        if stripped[:1] in "{[" and depth < 10:
            try:
                inner = json.loads(stripped)
            except (ValueError, TypeError):
                pass
            else:
                if isinstance(inner, (dict, list)):
                    return json.dumps(
                        _redact(inner, secrets, key, depth + 1), ensure_ascii=False
                    )
        return _scrub_text(value, secrets, skip_ip=_is_version_key(key))
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

    # One pass to learn the real secret strings, so the second pass can also
    # remove them where they are echoed under an innocent key.
    secrets: set[str] = set()
    _collect_secrets(dict(entry.data), secrets)
    if coordinator is not None:
        _collect_secrets(coordinator.raw_payloads, secrets)
        _collect_secrets(coordinator.data or {}, secrets)
    frozen = frozenset(secrets)

    data: dict[str, Any] = {
        "entry": {
            "data": _redact(dict(entry.data), frozen),
            "options": _redact(dict(entry.options), frozen),
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
    # Names and counts only, so it survives redaction and stays readable.
    data["map_inventory"] = coordinator.map_inventory or "map not decoded"
    data["raw"] = _redact(_trim(raw), frozen)
    data["snapshot"] = _redact(_trim(coordinator.data or {}), frozen)
    return data
