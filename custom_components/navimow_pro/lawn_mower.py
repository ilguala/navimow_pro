"""Lawn mower platform for Navimow (Private)."""
from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTIVITY_ERROR,
    ACTIVITY_MOWING,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
    DOMAIN,
    MOW_SETUP_RESTART,
    STATE_PAUSED,
    encode_partition_ids,
)
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)

_ACTIVITY_MAP = {
    ACTIVITY_MOWING: LawnMowerActivity.MOWING,
    ACTIVITY_PAUSED: LawnMowerActivity.PAUSED,
    ACTIVITY_RETURNING: LawnMowerActivity.RETURNING,
    ACTIVITY_ERROR: LawnMowerActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowLawnMower(coordinator)])


class NavimowLawnMower(NavimowEntity, LawnMowerEntity):
    """The mower as a HA lawn_mower entity."""

    _attr_name = None  # main feature of the device -> use the device name
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator, "mower")

    @property
    def activity(self) -> LawnMowerActivity:
        return _ACTIVITY_MAP.get(self.data.get("activity"), LawnMowerActivity.DOCKED)

    async def async_start_mowing(self) -> None:
        """Resume a paused job, otherwise mow the zone chosen in the zone select.

        The zone select stores the choice (``coordinator.selected_zone_ids``,
        empty = all zones); this button starts it. Always "restart" mode -- the
        popup "Mow now" card is where a continue-vs-restart choice lives.
        """
        client = self.coordinator.client
        sn = self._sn
        if self.data.get("state_code") == STATE_PAUSED:
            await self.coordinator.async_send(client.resume, sn)
            return

        zones = self.data.get("zones") or []
        if not zones:
            raise HomeAssistantError(
                "No mowing zones are known for this mower. Configure them in the "
                "integration Options (id:name,...) so a start command can be sent."
            )
        available_ids = [z["id"] for z in zones]
        sel = self.coordinator.selected_zone_ids or []
        region_ids = [i for i in sel if i in available_ids] or available_ids
        partition_ids = encode_partition_ids(region_ids)
        await self.coordinator.async_send(
            client.mow_zones, sn, partition_ids, MOW_SETUP_RESTART
        )

    async def async_pause(self) -> None:
        await self.coordinator.async_send(self.coordinator.client.pause, self._sn)

    async def async_dock(self) -> None:
        await self.coordinator.async_send(self.coordinator.client.dock, self._sn)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.data
        return {
            "state_code": data.get("state_code"),
            "state": data.get("state"),
            "current_zone": data.get("current_zone"),
        }
