"""Event platform for Navimow (Private).

One event entity per mower: mowing started, returned to dock, mowing finished
and error. What counts as each is decided in event_detector.py.
"""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity
from .event_detector import EVENT_TYPES, MowerEventDetector


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowMowerEvent(coordinator)])


class NavimowMowerEvent(NavimowEntity, EventEntity):
    """Mowing started / returned to dock / mowing finished / error."""

    _attr_translation_key = "mower_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator, "mower_event")
        self._detector = MowerEventDetector()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._detector.update(self.coordinator.data or {})

    @callback
    def _handle_coordinator_update(self) -> None:
        events = self._detector.update(self.coordinator.data or {})
        if not events:
            super()._handle_coordinator_update()
            return
        # One state write per event, so an update that both returns the mower
        # and finishes the job reaches automations as two events, not one.
        for event_type, attrs in events:
            self._trigger_event(event_type, attrs)
            self.async_write_ha_state()
