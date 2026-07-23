"""Switch platform for Navimow (Private): cloud settings toggles.

Writes go through ``/vehicle/set/save-set-data`` with a zero-padded boolean
string, exactly as proven for ``nightMowSwitch``. Only the night-mow switch is
individually proven; the others use the same encoding and camelCase write keys
inferred from the MowerSettingBean model, so they are added only when their
value is actually present in set-list and are disabled by default (opt-in).
See README.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity


@dataclass(frozen=True, kw_only=True)
class NavimowSwitchDescription(SwitchEntityDescription):
    """Switch description mapping a snapshot key to a save-set-data write key."""

    value_fn: Callable[[dict], bool | None]
    write_key: str
    proven: bool = False


SWITCHES: tuple[NavimowSwitchDescription, ...] = (
    NavimowSwitchDescription(
        key="night_mow",
        translation_key="night_mow",
        icon="mdi:weather-night",
        value_fn=lambda s: s.get("night_mow"),
        write_key="nightMowSwitch",
        proven=True,
    ),
    NavimowSwitchDescription(
        key="rain_sensor",
        translation_key="rain_sensor",
        icon="mdi:weather-rainy",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("rain_sensor"),
        write_key="rainSensor",
        proven=True,  # write verified live (flip+restore)
    ),
    NavimowSwitchDescription(
        key="rain_detection",
        translation_key="rain_detection",
        icon="mdi:weather-pouring",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("rain_detection"),
        write_key="rainDetectionSwitch",
        proven=True,  # write verified live (flip+restore)
    ),
    NavimowSwitchDescription(
        key="sound",
        translation_key="sound",
        icon="mdi:volume-high",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("sound"),
        write_key="soundSwitch",
        proven=True,  # write verified live (flip+restore)
    ),
    NavimowSwitchDescription(
        key="power_saving",
        translation_key="power_saving",
        icon="mdi:leaf",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("power_saving"),
        write_key="lowPowerSet",
        proven=True,  # write verified live (flip+restore)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    settings = (coordinator.data or {}).get("settings") or {}
    entities = [
        NavimowSwitch(coordinator, desc)
        for desc in SWITCHES
        # Proven switch is always offered; others only when discoverable.
        if desc.proven or desc.value_fn(settings) is not None
    ]
    async_add_entities(entities)


class NavimowSwitch(NavimowEntity, SwitchEntity):
    """A boolean cloud setting."""

    entity_description: NavimowSwitchDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowSwitchDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        # Best-effort (non-proven) settings are opt-in.
        self._attr_entity_registry_enabled_default = description.proven

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.data.get("settings") or {})

    async def _write(self, on: bool) -> None:
        await self.coordinator.async_send(
            self.coordinator.client.set_bool_setting,
            self._sn,
            self.entity_description.write_key,
            on,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)
