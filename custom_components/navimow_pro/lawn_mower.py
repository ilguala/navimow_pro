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
    STATE_PAUSED,
    STATE_PAUSED_RETURNING,
    encode_partition_ids,
    model_lacks,
    mow_setup,
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
        """Carry on with an unfinished job, otherwise mow the zones chosen.

        The zone select stores the choice (``coordinator.selected_zone_ids``,
        empty = all zones); this button starts it, in "restart" mode -- the popup
        "Mow now" card is where a continue-vs-restart choice lives.

        Two cases continue instead. A paused mower resumes. And a mower back in
        its dock with the job half done -- sent home by rain, or by the end of
        its window -- carries on when no zones are picked: that is what the app's
        Mow now does, and Start used to throw the job away and begin again from
        0 % (#26). Picked zones are a new job and start as one.

        Both paused codes resume: pausing a mow gives 0211, pausing a return
        gives 0221, and matching only 0211 meant a mower paused on its way to the
        dock -- issue #14's mower, stopped a metre from a closed gate -- answered
        Start by starting a whole new mow.

        Tested on the state CODE and not on ``activity``, which looks equivalent
        and is not: the coordinator overwrites activity with ERROR whenever a
        fault is reported or still held, so an activity test cannot match in the
        ~90 s after an owner frees the mower from an obstacle -- precisely when
        they reach for Start. That would restart the whole lawn from zero.
        """
        client = self.coordinator.client
        sn = self._sn
        if self.data.get("state_code") in (STATE_PAUSED, STATE_PAUSED_RETURNING):
            await self.coordinator.async_send(client.resume, sn)
            return
        progress = self.data.get("mowing_progress")
        if (
            self.data.get("docked")
            and isinstance(progress, int)
            and 0 < progress < 100
            and not self.coordinator.selected_zone_ids
        ):
            await self.coordinator.async_send(client.resume, sn)
            return

        zones = self.data.get("zones") or []
        if not zones:
            raise HomeAssistantError(
                "No mowing zones are known for this mower. Configure them in the "
                "integration Options (id:name,...) so a start command can be sent."
            )
        available_ids = [z["id"] for z in zones]
        sel = [i for i in (self.coordinator.selected_zone_ids or []) if i in available_ids]
        region_ids = sel or available_ids
        partition_ids = encode_partition_ids(region_ids)
        # A picked zone is a preference to honour; "all zones" is not, so let the
        # robot choose its own route there -- as it must on a model that ignores
        # the custom order altogether (see FAMILY_LACKS).
        ordered = bool(sel) and not model_lacks(self.data.get("model"), "ordered_mow")
        await self.coordinator.async_send(
            client.mow_zones,
            sn,
            partition_ids,
            mow_setup(reset=True, ordered=ordered),
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
