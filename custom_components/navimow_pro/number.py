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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength
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
    # The two percentages were CAPTURED taking a hex string on the device
    # channel. Cutting height was not, and the mower reports it as a decimal
    # string ('85'), so matching the read format is the better-founded guess.
    robot_hex: bool = True


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
        key="cut_height",
        translation_key="cut_height",
        icon="mdi:arrow-up-down",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        # Replaced at construction by the steps the mower itself reports.
        native_min_value=50,
        native_max_value=100,
        native_step=5,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: s.get("cut_height"),
        write_key="height",
        robot_hex=False,
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
    data = coordinator.data or {}
    settings = data.get("settings") or {}

    def _supported(desc: NavimowNumberDescription) -> bool:
        if desc.value_fn(settings) is None:
            return False
        # Plenty of mowers report a height while having only a manual knob;
        # isCutterHeight is the machine saying it has a motor for it.
        if desc.key == "cut_height":
            return bool(data.get("cut_height_supported"))
        return True

    async_add_entities(
        NavimowNumber(coordinator, desc) for desc in NUMBERS if _supported(desc)
    )


class NavimowNumber(NavimowEntity, NumberEntity):
    """A percentage cloud setting (return-to-dock battery, charge ceiling)."""

    entity_description: NavimowNumberDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        # Prefer what the mower publishes about itself over the descriptor's
        # defaults: the accepted range differs per model and the machine knows
        # it. Static bounds stay as the fallback for models that stay silent.
        bounds = ((coordinator.data or {}).get("number_limits") or {}).get(description.key)
        if bounds:
            self._attr_native_min_value = float(bounds["min"])
            self._attr_native_max_value = float(bounds["max"])
        options = [
            int(o)
            for o in ((coordinator.data or {}).get("cut_height_options") or [])
            if str(o).strip().lstrip("-").isdigit()
        ]
        if description.key == "cut_height" and len(options) >= 2:
            options.sort()
            self._attr_native_min_value = float(options[0])
            self._attr_native_max_value = float(options[-1])
            steps = {b - a for a, b in zip(options, options[1:])}
            self._attr_native_step = float(min(steps)) if steps else 1.0
        self._allowed = options if description.key == "cut_height" else []

    @property
    def native_value(self) -> float | None:
        val = self.entity_description.value_fn(self.data.get("settings") or {})
        return None if val is None else float(val) / self.entity_description.scale

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        wire = int(round(value)) * desc.scale
        # Never send a height the machine did not offer: snap to the nearest one
        # it listed rather than trusting the slider's arithmetic.
        if self._allowed:
            wire = min(self._allowed, key=lambda o: abs(o - wire))
        key = desc.write_key
        # 1) device command first -- robot value is a hex string ('14'=20,
        #    '0C'=12), so the robot applies it (the cloud copy alone is reverted).
        #    Refused while mowing, aborting before the cloud write, like the app.
        await self.coordinator.async_send(
            self.coordinator.client.send_setting_device,
            self._sn,
            {key: f"{wire:02X}" if desc.robot_hex else str(wire)},
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
