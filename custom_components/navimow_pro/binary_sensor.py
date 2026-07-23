"""Binary sensor platform for Navimow (Private)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity


@dataclass(frozen=True, kw_only=True)
class NavimowBinaryDescription(BinarySensorEntityDescription):
    """Binary sensor description with a value extractor over the snapshot."""

    value_fn: Callable[[dict], bool | None]


BINARY_SENSORS: tuple[NavimowBinaryDescription, ...] = (
    NavimowBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.get("error"),
    ),
    NavimowBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("online"),
    ),
    NavimowBinaryDescription(
        key="docked",
        translation_key="docked",
        icon="mdi:home-import-outline",
        value_fn=lambda d: d.get("docked"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NavimowBinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class NavimowBinarySensor(NavimowEntity, BinarySensorEntity):
    """A boolean derived from the mower snapshot."""

    entity_description: NavimowBinaryDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.data)
