"""Switch platform for Navimow (Private): cloud settings toggles.

Two write families, both via ``/vehicle/set/save-set-data``:

* legacy switches (nightMow/rain/sound/power-saving) use the *plain* form with a
  zero-padded boolean string ('01'/'00');
* "modern" MowerSettingBean toggles (child lock, lift alarm, cyclic mowing, and
  the frost/snow/storm/high-temp mow delays) use ``operation_type:"iot_set"``
  with a per-key value encoding (some keys a JSON number ``1``/``0``, others a
  string ``"1"``/``"0"``). The plain form is acked but NOT applied for these.

Both write shapes and the per-key encodings were captured live from the app.
The modern toggles are feature-detected (created only when the robot actually
reports the key) so a different model only sees what it has.
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
    # "modern" settings: write via save-set-data + operation_type:iot_set.
    iot: bool = False
    # iot value encoding: True => JSON number (1/0), False => string ('1'/'0').
    numeric: bool = False
    # Device command (cmdCode s:mower on /vehicle/set/send) fired alongside the
    # cloud write so the robot actually applies it (the cloud copy alone reverts).
    # robot_key defaults to write_key; override where they differ (e.g.
    # tractionControl -> tcsSwitch). robot_numeric: True => JSON number 1/0,
    # False => string "1"/"0" (robot encoding, may differ from the cloud one).
    robot_key: str | None = None
    robot_numeric: bool = True
    # Registry enabled-by-default; None follows ``proven``.
    enabled_default: bool | None = None
    # Write-only setting (robot never reports it back): use assumed_state and
    # track the last commanded value optimistically.
    assumed: bool = False
    # For write-only switches: settings key (of a readable "sibling" feature)
    # whose presence gates creation, since the switch's own key can't be read.
    gate_key: str | None = None


SWITCHES: tuple[NavimowSwitchDescription, ...] = (
    NavimowSwitchDescription(
        key="night_mow",
        translation_key="night_mow",
        icon="mdi:weather-night",
        value_fn=lambda s: s.get("night_mow"),
        write_key="nightMowSwitch",
        proven=True,
        iot=True,
        numeric=True,  # cloud number 1/0 (captured live; app also fires s:mower)
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
        proven=True,
        iot=True,  # cloud string "1"/"0" (captured live; app also fires s:mower)
    ),
    NavimowSwitchDescription(
        key="power_saving",
        translation_key="power_saving",
        icon="mdi:leaf",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("power_saving"),
        write_key="lowPowerSet",
        proven=True,
        iot=True,
        numeric=True,  # cloud number 1/0 (captured live; app also fires s:mower)
    ),
    # --- "modern" MowerSettingBean toggles -----------------------------------
    # Write via save-set-data + operation_type:iot_set (the plain form is acked
    # but NOT applied for these). Per-key value encoding captured live; a restore
    # batch was verified applied on the owner account. Feature-detected (created
    # only when the robot reports the key) but enabled by default once created.
    NavimowSwitchDescription(
        key="child_lock",
        translation_key="child_lock",
        icon="mdi:account-lock",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("child_lock"),
        write_key="childLock",
        iot=True,  # string "1"/"0"
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="lift_alarm",
        translation_key="lift_alarm",
        icon="mdi:alarm-light",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("lift_alarm"),
        write_key="liftSwitch",
        iot=True,  # string "1"/"0"
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="mowing_cycle",
        translation_key="mowing_cycle",
        icon="mdi:sync",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("mowing_cycle"),
        write_key="mowingCycle",
        iot=True,  # cloud string "1"/"0"
        robot_numeric=False,  # robot also string "1"/"0"
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="frost_delay",
        translation_key="frost_delay",
        icon="mdi:snowflake-alert",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("frost_delay"),
        write_key="frostSwitch",
        iot=True,
        numeric=True,  # JSON number 1/0
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="snow_delay",
        translation_key="snow_delay",
        icon="mdi:snowflake",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("snow_delay"),
        write_key="snowSwitch",
        iot=True,
        numeric=True,  # JSON number 1/0
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="storm_delay",
        translation_key="storm_delay",
        icon="mdi:weather-windy",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("storm_delay"),
        write_key="stormSwitch",
        iot=True,
        numeric=True,  # JSON number 1/0
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="high_temp_delay",
        translation_key="high_temp_delay",
        icon="mdi:thermometer-high",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("high_temp_delay"),
        write_key="highTempSwitch",
        iot=True,
        numeric=True,  # JSON number 1/0
        enabled_default=True,
    ),
    # --- vision / advanced toggles (captured live 2026-07-24, one-at-a-time) --
    # save-set-data + iot_set, numeric 1/0. slam/cpt/traction are read-back;
    # animalProtection and lightSwitch are NOT reported by the robot -> assumed
    # state, and gated on a readable sibling so other models don't get a phantom.
    NavimowSwitchDescription(
        key="efls",  # EFLS = camera-assisted positioning (slamSwitch)
        translation_key="efls",
        icon="mdi:cctv",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("efls"),
        write_key="slamSwitch",
        iot=True,
        numeric=True,
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="obstacle_avoidance",  # VisionFence obstacle avoidance (cptSwitch)
        translation_key="obstacle_avoidance",
        icon="mdi:eye-off-outline",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("obstacle_avoid"),
        write_key="cptSwitch",
        iot=True,
        numeric=True,
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="traction_control",
        translation_key="traction_control",
        icon="mdi:car-traction-control",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("traction"),
        write_key="tractionControl",  # cloud key
        robot_key="tcsSwitch",  # robot key differs (captured live)
        iot=True,
        numeric=True,
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="animal_protection",  # VisionFence animal-friendly (write-only)
        translation_key="animal_protection",
        icon="mdi:paw",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: None,
        write_key="animalProtection",
        iot=True,
        numeric=True,
        enabled_default=True,
        assumed=True,
        gate_key="obstacle_avoid",  # part of VisionFence
    ),
    NavimowSwitchDescription(
        key="night_light",  # night light on/off (write-only)
        translation_key="night_light",
        icon="mdi:lightbulb-night-outline",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: None,
        write_key="lightSwitch",
        iot=True,
        numeric=True,
        enabled_default=True,
        assumed=True,
        gate_key="night_light_level",
    ),
    # --- rain / weather-forecast zone (captured live 2026-07-24) --------------
    # Distinct from the physical rain sensor above. All robot+cloud, number 1/0.
    NavimowSwitchDescription(
        key="weather_rain",  # weatherSwitch = master weather-forecast rain detection
        translation_key="weather_rain",
        icon="mdi:weather-rainy",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("weather_switch"),
        write_key="weatherSwitch",
        iot=True,
        numeric=True,
        enabled_default=True,
    ),
    NavimowSwitchDescription(
        key="rain_delay_mode",  # delayedPileSwitch: on=delay(1) / off=continue(0)
        translation_key="rain_delay_mode",
        icon="mdi:timer-sand",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.get("rain_behavior"),
        write_key="delayedPileSwitch",
        iot=True,
        numeric=True,
        enabled_default=True,
    ),
)


def _present(desc: NavimowSwitchDescription, settings: dict) -> bool:
    """Whether to create the switch for this robot."""
    if desc.proven:
        return True
    if desc.gate_key is not None:  # write-only: gate on a readable sibling
        return settings.get(desc.gate_key) is not None
    return desc.value_fn(settings) is not None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    settings = (coordinator.data or {}).get("settings") or {}
    entities = [
        NavimowSwitch(coordinator, desc) for desc in SWITCHES if _present(desc, settings)
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
        # Enabled-by-default: explicit override wins, else follow ``proven``
        # (best-effort/non-proven legacy switches stay opt-in).
        self._attr_entity_registry_enabled_default = (
            description.enabled_default
            if description.enabled_default is not None
            else description.proven
        )
        # Write-only settings have no read-back: show as assumed-state and
        # remember the last command optimistically.
        self._attr_assumed_state = description.assumed
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.assumed:
            return self._optimistic
        return self.entity_description.value_fn(self.data.get("settings") or {})

    async def _write(self, on: bool) -> None:
        desc = self.entity_description
        if desc.iot:
            # 1) device command first -- makes the robot actually apply it (the
            #    cloud copy alone is reverted by the robot). While mowing this is
            #    refused and aborts before the cloud write, same as the app.
            robot_val: Any = (1 if on else 0) if desc.robot_numeric else ("1" if on else "0")
            await self.coordinator.async_send(
                self.coordinator.client.send_setting_device,
                self._sn,
                {desc.robot_key or desc.write_key: robot_val},
            )
            # 2) cloud persist (save-set-data + iot_set)
            await self.coordinator.async_send(
                self.coordinator.client.set_iot_bool,
                self._sn,
                self.coordinator.vehicle_type,
                desc.write_key,
                on,
                desc.numeric,
            )
        else:
            await self.coordinator.async_send(
                self.coordinator.client.set_bool_setting,
                self._sn,
                desc.write_key,
                on,
            )
        if desc.assumed:
            self._optimistic = on
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)
