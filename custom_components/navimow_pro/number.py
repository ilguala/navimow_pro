"""Number platform for Navimow (Private): percentage settings.

Two MowerSettingBean percentages -- the return-to-dock battery threshold and the
charge ceiling. Asymmetric encoding (captured live):

* READ:  the set-list reports them as a DECIMAL percentage (10 / 100).
* WRITE: like every setting, sent on BOTH channels, encoded differently -- the
  device command (cmdCode s:mower on /vehicle/set/send) takes a **hex string**
  ('14'=20), the cloud persist (iot_set) a **decimal number** (20).

Written robot-first then cloud, like the app (the cloud copy alone is reverted
by the robot). Feature-detected: created only when the robot reports the key.
Ranges are best-effort (the app's exact min/max wasn't captured); the robot
rejects an out-of-range value harmlessly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity


@dataclass(frozen=True, kw_only=True)
class NavimowNumberDescription(NumberEntityDescription):
    """A numeric MowerSettingBean value (same key on both write channels).

    ``value_fn`` returns the raw *wire* integer from settings; the entity shows
    ``wire / scale`` and writes ``wire = displayed * scale``. The device
    (s:mower) command always encodes the wire as a hex string; the cloud
    (iot_set) uses hex too when ``cloud_hex`` else a bare decimal number.
    """

    value_fn: Callable[[dict], int | None]
    write_key: str
    scale: int = 1
    cloud_hex: bool = False


NUMBERS: tuple[NavimowNumberDescription, ...] = (
    NavimowNumberDescription(
        key="return_battery_level",
        translation_key="return_battery_level",
        icon="mdi:battery-arrow-down",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=5,
        native_max_value=50,
        native_step=5,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: s.get("return_battery_level"),
        write_key="returnBatteryLevel",
    ),
    NavimowNumberDescription(
        key="charging_limit",
        translation_key="charging_limit",
        icon="mdi:battery-charging-high",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=50,
        native_max_value=100,
        native_step=5,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: s.get("charging_limit"),
        write_key="chargingLimit",
    ),
    NavimowNumberDescription(
        key="rain_delay_time",  # delayedPileSet: rain-delay duration, hours
        translation_key="rain_delay_time",
        icon="mdi:timer-pause",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement="h",
        native_min_value=1,
        native_max_value=12,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda s: s.get("rain_delay_wire"),
        write_key="delayedPileSet",
        scale=4,  # wire = hours * 4 (15-min units); 3h -> 12 -> "0C"
        cloud_hex=True,  # cloud takes the hex string too (unlike the % settings)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    settings = (coordinator.data or {}).get("settings") or {}
    async_add_entities(
        NavimowNumber(coordinator, desc)
        for desc in NUMBERS
        if desc.value_fn(settings) is not None
    )


class NavimowNumber(NavimowEntity, NumberEntity):
    """A percentage cloud setting (return-to-dock battery, charge ceiling)."""

    entity_description: NavimowNumberDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        val = self.entity_description.value_fn(self.data.get("settings") or {})
        return None if val is None else float(val) / self.entity_description.scale

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        wire = int(round(value)) * desc.scale
        key = desc.write_key
        # 1) device command first -- robot value is a hex string ('14'=20,
        #    '0C'=12), so the robot applies it (the cloud copy alone is reverted).
        #    Refused while mowing, aborting before the cloud write, like the app.
        await self.coordinator.async_send(
            self.coordinator.client.send_setting_device,
            self._sn,
            {key: f"{wire:02X}"},
        )
        # 2) cloud persist (iot_set): hex string for some keys, bare decimal for
        #    the percentages -- per the captured per-key encoding.
        cloud_val = f"{wire:02X}" if desc.cloud_hex else wire
        await self.coordinator.async_send(
            self.coordinator.client.save_setting_iot,
            self._sn,
            self.coordinator.vehicle_type,
            {key: cloud_val},
        )
