"""Services for Navimow (Private).

- ``navimow_pro.set_schedule`` writes one weekday's plan (enabled + one or more
  time periods, each optionally restricted to zones) via the proven
  save-set-data format.
- ``navimow_pro.mow`` starts mowing now: chosen zones (empty = all) and a
  ``reset`` flag (True = riparti da zero / clear progress, False = continua).

These back the graphical cards (and automations).
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    MOW_SETUP_CONTINUE,
    MOW_SETUP_RESTART,
    encode_partition_ids,
)

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_MOW = "mow"

# Navimow weekday numbering is 1=Sun .. 7=Sat.
_WEEKDAY_TO_NUM = {
    "sunday": 1,
    "monday": 2,
    "tuesday": 3,
    "wednesday": 4,
    "thursday": 5,
    "friday": 6,
    "saturday": 7,
}

_PERIOD_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,  # "HH:MM"
        vol.Required("end"): cv.string,  # "HH:MM"
        vol.Optional("zones", default=list): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("day"): vol.In(list(_WEEKDAY_TO_NUM)),
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("periods", default=list): vol.All(cv.ensure_list, [_PERIOD_SCHEMA]),
    }
)

MOW_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        # Region ids to mow; empty = all available zones.
        vol.Optional("zones", default=list): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        # True = riparti da zero (clear progress); False = continua.
        vol.Optional("reset", default=True): cv.boolean,
    }
)


def _hhmm_to_min(value: str) -> int:
    parts = str(value).strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ServiceValidationError(f"Invalid time '{value}' (use HH:MM)")
    return h * 60 + m


def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_MOW):
        return

    def _resolve_coordinator(call: ServiceCall):
        store = hass.data.get(DOMAIN) or {}
        coords = list(store.values())
        device_id = call.data.get("device_id")
        if device_id:
            device = dr.async_get(hass).async_get(device_id)
            if device:
                for entry_id in device.config_entries:
                    if entry_id in store:
                        return store[entry_id]
            raise ServiceValidationError("device_id is not a Navimow (Private) mower")
        if len(coords) == 1:
            return coords[0]
        raise ServiceValidationError(
            "Multiple Navimow mowers configured: pass device_id to choose one"
        )

    async def _set_schedule(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(call)
        day_num = _WEEKDAY_TO_NUM[call.data["day"]]
        enabled = call.data["enabled"]
        periods = []
        for p in call.data.get("periods", []):
            start_min = _hhmm_to_min(p["start"])
            end_min = _hhmm_to_min(p["end"])
            # An end of "00:00" means end-of-day (24:00 = slot 96), never 0.
            if end_min == 0:
                end_min = 1440
            periods.append(
                {
                    "start_min": start_min,
                    "end_min": end_min,
                    "zone_ids": list(p.get("zones") or []),
                }
            )
        try:
            await coordinator.async_send(
                coordinator.client.set_day_schedule,
                coordinator.sn,
                coordinator.vehicle_type,
                day_num,
                enabled,
                periods,
            )
        except Exception as err:  # noqa: BLE001 - surface a clean error to the UI
            raise HomeAssistantError(f"Navimow set_schedule failed: {err}") from err

    async def _mow(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(call)
        zones = [int(z) for z in call.data.get("zones") or []]
        if not zones:
            # All available zones (from the current snapshot).
            zones = [
                z["id"]
                for z in (coordinator.data or {}).get("zones") or []
                if z.get("id") is not None
            ]
        if not zones:
            raise ServiceValidationError(
                "No mowing zones known yet — wait for the mower to report its map, "
                "or pass explicit zone ids."
            )
        partition_ids = encode_partition_ids(zones)
        # partitionSetup carries the restart/continue mode (proven live).
        partition_setup = MOW_SETUP_RESTART if call.data["reset"] else MOW_SETUP_CONTINUE
        try:
            await coordinator.async_send(
                coordinator.client.mow_zones,
                coordinator.sn,
                partition_ids,
                partition_setup,
            )
        except Exception as err:  # noqa: BLE001 - surface a clean error to the UI
            raise HomeAssistantError(f"Navimow mow failed: {err}") from err

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_MOW, _mow, schema=MOW_SCHEMA)
