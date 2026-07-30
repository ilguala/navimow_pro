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

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import NavimowCoordinator

# Anything that identifies the account, the session or the machine. Kept
# deliberately wide: these dumps are meant to be pasted into public issues.
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

_MAX_STR = 300  # characters kept of any single string value
_MAX_LIST = 8  # items kept of any single list


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
        if len(value) > _MAX_LIST:
            head = [_trim(v, depth + 1) for v in value[:_MAX_LIST]]
            return [*head, f"<{len(value) - _MAX_LIST} more of {len(value)} items>"]
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
            "data": async_redact_data(dict(entry.data), TO_REDACT),
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
    data["raw"] = async_redact_data(_trim(raw), TO_REDACT)
    data["snapshot"] = async_redact_data(_trim(coordinator.data or {}), TO_REDACT)
    return data
