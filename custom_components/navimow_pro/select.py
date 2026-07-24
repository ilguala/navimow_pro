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
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)

ALL_ZONES = "All zones"


@dataclass(frozen=True, kw_only=True)
class NavimowSelectDescription(SelectEntityDescription):
    """A multi-value MowerSettingBean setting (numeric on the wire)."""

    value_fn: Callable[[dict], int | None]
    write_key: str
    # option string -> numeric value written (also defines the option list).
    value_map: dict[str, int]
    # device (s:mower) value encoding: True => JSON number, False => zero-padded
    # 2-char string ("00"/"01"). Cloud (iot_set) is always the bare number.
    robot_numeric: bool = False


# Settings selects (written via save-set-data + iot_set, bare integer value).
# Feature-detected: created only when the robot reports the key.
SETTING_SELECTS: tuple[NavimowSelectDescription, ...] = (
    NavimowSelectDescription(
        key="night_light_level",
        translation_key="night_light_level",
        icon="mdi:brightness-6",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("night_light_level"),
        write_key="nightLightLevel",
        value_map={"dim": 0, "very_dim": 1},  # Tenue / Molto tenue
        robot_numeric=False,  # robot takes "00"/"01"
    ),
    NavimowSelectDescription(
        key="weather_sensitivity",  # rain-forecast sensitivity
        translation_key="weather_sensitivity",
        icon="mdi:weather-partly-rainy",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("weather_sensitivity"),
        write_key="weatherSensitivity",
        value_map={"drizzle": 0, "light": 1, "moderate": 2},
        robot_numeric=True,  # robot takes the bare number
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    settings = (coordinator.data or {}).get("settings") or {}
    entities: list[SelectEntity] = [NavimowZoneSelect(coordinator)]
    entities += [
        NavimowSettingSelect(coordinator, desc)
        for desc in SETTING_SELECTS
        if desc.value_fn(settings) is not None
    ]
    async_add_entities(entities)


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


class NavimowSettingSelect(NavimowEntity, SelectEntity):
    """A multi-value cloud setting (e.g. night-light brightness)."""

    entity_description: NavimowSelectDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowSelectDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.value_map)
        self._rev = {v: k for k, v in description.value_map.items()}

    @property
    def current_option(self) -> str | None:
        val = self.entity_description.value_fn(self.data.get("settings") or {})
        return self._rev.get(val)

    async def async_select_option(self, option: str) -> None:
        num = self.entity_description.value_map[option]
        key = self.entity_description.write_key
        # 1) device command first -- makes the robot apply it (the cloud copy
        #    alone reverts). Robot value: bare number, or a zero-padded 2-char
        #    string per the captured per-key encoding. Refused while mowing,
        #    aborts before the cloud write.
        robot_val = num if self.entity_description.robot_numeric else f"{num:02d}"
        await self.coordinator.async_send(
            self.coordinator.client.send_setting_device,
            self._sn,
            {key: robot_val},
        )
        # 2) cloud persist (save-set-data + iot_set, bare integer)
        await self.coordinator.async_send(
            self.coordinator.client.save_setting_iot,
            self._sn,
            self.coordinator.vehicle_type,
            {key: num},
        )
