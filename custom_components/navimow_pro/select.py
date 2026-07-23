"""Select platform for Navimow (Private): choose which zone the mow button uses.

Selecting an option only STORES the choice (on the coordinator); it does NOT
start mowing. Pressing the lawn_mower "mow" button then mows the stored zone (or
all zones). This is the classic select-then-mow flow.

The available zone list is best-effort: it comes from the integration Options
(``id:name,...``), else auto-discovery from the map endpoints, else the region
currently selected on the mower. If none can be determined, the entity is shown
but unavailable (see README).
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)

ALL_ZONES = "All zones"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowZoneSelect(coordinator)])


class NavimowZoneSelect(NavimowEntity, SelectEntity):
    """Pick a zone (or all) to start mowing."""

    _attr_translation_key = "zone"
    _attr_icon = "mdi:select-marker"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator, "zone")

    def _zones(self) -> list[dict]:
        return self.data.get("zones") or []

    @property
    def options(self) -> list[str]:
        zones = self._zones()
        if not zones:
            return []
        return [z["name"] for z in zones] + [ALL_ZONES]

    @property
    def current_option(self) -> str | None:
        """The user's stored choice (what the mow button will use), not the
        zone currently being mowed."""
        zones = self._zones()
        if not zones:
            return None
        sel = self.coordinator.selected_zone_ids or []
        if not sel:
            return ALL_ZONES
        if set(sel) == {z["id"] for z in zones}:
            return ALL_ZONES
        if len(sel) == 1:
            for z in zones:
                if z["id"] == sel[0]:
                    return z["name"]
        return None

    @property
    def available(self) -> bool:
        return super().available and bool(self._zones())

    async def async_select_option(self, option: str) -> None:
        """Store the zone choice for the mow button. Does NOT start mowing."""
        zones = self._zones()
        if option == ALL_ZONES:
            region_ids: list[int] = []  # empty = all zones
        else:
            match = next((z for z in zones if z["name"] == option), None)
            if match is None:
                _LOGGER.warning("Unknown zone option %s", option)
                return
            region_ids = [match["id"]]
        self.coordinator.selected_zone_ids = region_ids
        self.async_write_ha_state()
