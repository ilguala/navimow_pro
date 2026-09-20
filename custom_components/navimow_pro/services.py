"""Services for Navimow (Private).

- ``navimow_pro.set_schedule`` writes one weekday's plan (enabled + one or more
  time periods, each optionally restricted to zones) via the
  save-set-data format.
- ``navimow_pro.resume`` resumes the job the mower already has, without
  choosing zones and without a reset flag -- the one call that cannot throw away
  progress. The Start button does this too when the mower is paused, but only a
  service can be reached from an automation.
- ``navimow_pro.mow`` starts mowing now: chosen zones and a ``reset`` flag
  (True = riparti da zero / clear progress, False = continua). Listing zones
  explicitly also fixes the ORDER they are mowed in (like the app's "Personalizza
  la sequenza di falciatura"); omitting them means all zones with the robot
  choosing its own route.

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
    encode_partition_ids,
    mow_setup,
)

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_MOW = "mow"
SERVICE_RESUME = "resume"

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


RESUME_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})


def _check_zones(coordinator, zone_ids: list[int]) -> None:
    """Refuse zone ids this mower's map does not contain.

    Checked against the DECODED MAP only, never against ``snapshot["zones"]``.
    That list is a three-tier fallback: real map geometry, then the hand-typed
    Options id:name string, then -- last resort -- just the partitions of the job
    currently selected. The lower two tiers are non-empty but partial by
    construction, so trusting them would refuse zone ids that genuinely exist and
    turn our failure to read the map into the user's error. ``snapshot["map"]``
    is None until the geometry really decoded, which is the honest test.
    """
    known = {
        z["id"]
        for z in ((coordinator.data or {}).get("map") or {}).get("zones") or []
        if z.get("id") is not None
    }
    if not known:
        return
    unknown = sorted({int(z) for z in zone_ids} - known)
    if unknown:
        raise ServiceValidationError(
            f"Unknown zone id(s): {', '.join(str(u) for u in unknown)}. "
            f"This mower reports {', '.join(str(k) for k in sorted(known))}."
        )


def _check_periods(periods: list[dict]) -> None:
    """A day's periods must each be forward in time, and must not overlap.

    The mower accepts an overlapping or reversed plan without complaint and then
    behaves in a way nobody can predict from what they typed, so the refusal has
    to happen here.
    """
    for period in periods:
        if period["end_min"] <= period["start_min"]:
            raise ServiceValidationError(
                f"A period ending at {_min_to_hhmm(period['end_min'])} cannot start "
                f"at {_min_to_hhmm(period['start_min'])}."
            )
    ordered = sorted(periods, key=lambda p: p["start_min"])
    for first, second in zip(ordered, ordered[1:]):
        if second["start_min"] < first["end_min"]:
            raise ServiceValidationError(
                f"Periods overlap: {_min_to_hhmm(first['start_min'])}-"
                f"{_min_to_hhmm(first['end_min'])} and "
                f"{_min_to_hhmm(second['start_min'])}-"
                f"{_min_to_hhmm(second['end_min'])}."
            )


def _min_to_hhmm(minutes: int) -> str:
    """Minutes from midnight back to 'HH:MM', for error messages."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hhmm_to_min(value: str) -> int:
    """``'HH:MM'`` -> minutes from midnight, on the mower's 15-minute grid.

    The weekly plan travels as 15-minute slot indices (``start_min // 15`` in
    :mod:`.api.client`), so a time off the grid is silently rounded down -- and a
    short period can collapse to zero length, since 09:47 and 09:52 are both
    slot 39. Refuse it here instead. The scheduler card already snaps to the
    grid, so this only ever fires for a service call written by hand.
    """
    parts = str(value).strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        raise ServiceValidationError(f"Invalid time '{value}' (use HH:MM)") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ServiceValidationError(f"Invalid time '{value}' (use HH:MM)")
    if m % 15:
        raise ServiceValidationError(
            f"Invalid time '{value}': the mower keeps the weekly plan in "
            "15-minute slots, so minutes must be 00, 15, 30 or 45"
        )
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
            zone_ids = [int(z) for z in p.get("zones") or []]
            _check_zones(coordinator, zone_ids)
            periods.append(
                {
                    "start_min": start_min,
                    "end_min": end_min,
                    "zone_ids": zone_ids,
                }
            )
        # Only when the day is being switched ON. A plan created in the Segway
        # app can legitimately overlap, and refusing to validate it would leave
        # the owner unable to turn that day OFF -- the card posts the stored
        # periods back verbatim even for a disabled day.
        if enabled:
            _check_periods(periods)
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
        # An explicit list means "mow these, in this order"; omitting it means
        # "all zones, no preference" -> let the robot route itself (see mow_setup).
        ordered = bool(zones)
        _check_zones(coordinator, zones)
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
        partition_setup = mow_setup(reset=call.data["reset"], ordered=ordered)
        try:
            await coordinator.async_send_confirmed(
                coordinator.client.mow_zones,
                coordinator.sn,
                partition_ids,
                partition_setup,
            )
        except Exception as err:  # noqa: BLE001 - surface a clean error to the UI
            raise HomeAssistantError(f"Navimow mow failed: {err}") from err

    async def _resume(call: ServiceCall) -> None:
        # Deliberately unconditional. The mower is the authority on whether it
        # has something to resume, and refusing here on a state code we polled up
        # to two minutes ago would invent a failure the machine never reported.
        coordinator = _resolve_coordinator(call)
        try:
            await coordinator.async_send_confirmed(coordinator.client.resume, coordinator.sn)
        except Exception as err:  # noqa: BLE001 - surface a clean error to the UI
            raise HomeAssistantError(f"Navimow resume failed: {err}") from err

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_MOW, _mow, schema=MOW_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RESUME, _resume, schema=RESUME_SCHEMA)
