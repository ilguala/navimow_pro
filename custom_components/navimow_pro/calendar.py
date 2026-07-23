"""Calendar platform for Navimow (Private).

Exposes the mower's weekly mowing schedule as a native HA calendar: each
scheduled period becomes a (recurring-weekly) event with the target zone in the
summary. The entity state is the NEXT upcoming mow, so you get "next cut + zone"
for free, plus automations on calendar events.

Read-only (Phase A). Fully defensive: no schedule -> no events, never crashes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)

# How far ahead the entity state looks for the "next" scheduled mow.
_HORIZON_DAYS = 14


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowScheduleCalendar(coordinator)])


class NavimowScheduleCalendar(NavimowEntity, CalendarEntity):
    """The weekly mowing schedule as calendar events."""

    _attr_translation_key = "schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator, "schedule")

    def _build_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Expand the weekly schedule into concrete events within [start, end]."""
        schedule = (self.data or {}).get("schedule") or []
        if not schedule:
            return []
        events: list[CalendarEvent] = []
        cur = dt_util.start_of_local_day(start)
        # +1 day guard so an event starting late on `end`'s day is still caught.
        limit = end + timedelta(days=1)
        while cur <= limit:
            our_day = (cur.weekday() + 1) % 7 + 1  # py Mon=0..Sun=6 -> Navimow 1=Sun..7=Sat
            for entry in schedule:
                if not entry.get("enabled") or entry.get("day") != our_day:
                    continue
                for period in entry.get("periods") or []:
                    smin, emin = period.get("start_min"), period.get("end_min")
                    if smin is None or emin is None or emin <= smin:
                        continue
                    ev_start = cur + timedelta(minutes=int(smin))
                    ev_end = cur + timedelta(minutes=int(emin))
                    if ev_end <= start or ev_start >= end:
                        continue
                    zones = ", ".join(period.get("zone_names") or ["All zones"])
                    events.append(
                        CalendarEvent(
                            start=ev_start,
                            end=ev_end,
                            summary=f"Mowing – {zones}",
                            description="Navimow scheduled mowing",
                        )
                    )
            cur += timedelta(days=1)
        return sorted(events, key=lambda ev: ev.start)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        try:
            return self._build_events(start_date, end_date)
        except Exception:  # noqa: BLE001 - a calendar must never crash the platform
            _LOGGER.debug("Failed to build Navimow calendar events", exc_info=True)
            return []

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next upcoming scheduled mow."""
        try:
            now = dt_util.now()
            for ev in self._build_events(
                now - timedelta(hours=1), now + timedelta(days=_HORIZON_DAYS)
            ):
                if ev.end > now:
                    return ev
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to compute next Navimow event", exc_info=True)
        return None
