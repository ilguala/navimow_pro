"""Number platform for Navimow (Private): percentage settings.

Two percentage settings -- the return-to-dock battery threshold and the
charge ceiling. Asymmetric encoding:

* READ:  the set-list reports them as a DECIMAL percentage (10 / 100).
* WRITE: like every setting, sent on BOTH channels, encoded differently -- the
  device command (cmdCode s:mower on /vehicle/set/send) takes a **hex string**
  ('14'=20), the cloud persist (iot_set) a **decimal number** (20).

Written robot-first then cloud, like the app (the cloud copy alone is reverted
by the robot). Feature-detected: created only when the robot reports the key.
Ranges fall back to conservative defaults when the mower does not state its
own; the robot
rejects an out-of-range value harmlessly.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CUT_HEIGHT_CONFIRM_S, DOMAIN, model_lacks
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NavimowNumberDescription(NumberEntityDescription):
    """A numeric setting (same key on both write channels).

    ``value_fn`` returns the raw *wire* integer from settings; the entity shows
    ``wire / scale`` and writes ``wire = displayed * scale``. The device
    (s:mower) command always encodes the wire as a hex string; the cloud
    (iot_set) uses hex too when ``cloud_hex`` else a bare decimal number.
    """

    value_fn: Callable[[dict], int | None]
    write_key: str
    scale: int = 1
    cloud_hex: bool = False
    # The two percentages take a hex string on the device channel. Cutting
    # height is reported as a decimal string ('85'), so it is written the way
    # it is read.
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
        # Decided in one place with the read-only sensor (const.cut_height_control),
        # so a mower gets exactly one of the two.
        if desc.key == "cut_height":
            return data.get("cut_height_control") == "slider"
        return not model_lacks(data.get("model"), desc.key)

    def _ruled_out(desc: NavimowNumberDescription) -> bool:
        """Excluded on the mower's own evidence, not for want of data.

        Only these are pruned from the registry. A setting missing from one
        snapshot may be back in the next; a family that has no charge limit, or
        a cutting height that became a read-only sensor, will not.
        """
        if desc.key == "cut_height":
            return data.get("cut_height_control") == "sensor"
        return model_lacks(data.get("model"), desc.key)

    registry = er.async_get(hass)
    keep = []
    for desc in NUMBERS:
        if _supported(desc):
            keep.append(NavimowNumber(coordinator, desc))
        elif settings and _ruled_out(desc):
            # e.g. the charge-limit slider X3 owners had until now, which the
            # Navimow app does not offer them, or the cutting-height slider an i1
            # got in 0.6.5 -- otherwise left behind as dead entities.
            stale = registry.async_get_entity_id("number", DOMAIN, f"{coordinator.sn}_{desc.key}")
            if stale:
                registry.async_remove(stale)
    async_add_entities(keep)


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
        # Cutting-height read-back: what was asked and not yet seen applied, a
        # target that failed (so a late success can still clear the warning),
        # and the scheduled confirmation.
        self._pending: int | None = None
        self._failed: int | None = None
        self._unsub_confirm = None

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
        #    the percentages -- per the per-key encoding.
        cloud_val = f"{wire:02X}" if desc.cloud_hex else wire
        await self.coordinator.async_send(
            self.coordinator.client.save_setting_iot,
            self._sn,
            self.coordinator.vehicle_type,
            {key: cloud_val},
        )
        if desc.key == "cut_height":
            self._expect(wire)

    # ------------------------------------------------- cutting-height read-back
    def _expect(self, target: int) -> None:
        """Remember what was asked, and come back to check it was applied.

        The slider is offered on the strength of the mower listing the heights
        it accepts, or of isCutterHeight -- two signals, neither proof (#12).
        Reading the value back is the proof, and with no mower to test on it is
        the only way a model that quietly ignores the write gets noticed at all.
        """
        self._cancel_confirm()
        self._pending = target
        self._unsub_confirm = async_call_later(
            self.hass, CUT_HEIGHT_CONFIRM_S, self._async_confirm
        )

    def _cancel_confirm(self) -> None:
        if self._unsub_confirm is not None:
            self._unsub_confirm()
            self._unsub_confirm = None

    async def _async_confirm(self, _now: datetime) -> None:
        """Half a minute on: read the settings afresh, then decide.

        The coordinator notifies this entity as part of that refresh, so if the
        value arrived, _handle_coordinator_update has already cleared _pending by
        the time the await returns. Anything still pending was not applied.
        """
        self._unsub_confirm = None
        if self._pending is None:
            return
        await self.coordinator.async_refresh_settings()
        if self._pending is None:
            return
        target, self._pending = self._pending, None
        self._failed = target
        current = self.entity_description.value_fn(self.data.get("settings") or {})
        _LOGGER.warning(
            "Cutting height not applied: asked for %s mm, the mower still reports %s mm",
            target,
            current,
        )
        persistent_notification.async_create(
            self.hass,
            f"{self.data.get('name') or 'The mower'} did not apply the cutting height: "
            f"asked for {target} mm, it still reports {current} mm.\n\n"
            "This model may not accept the height from Home Assistant. It would help "
            "to say so, with a diagnostics download, at "
            "https://github.com/ilguala/navimow_pro/issues",
            title="Navimow: cutting height not applied",
            notification_id=self._note_id,
        )

    @property
    def _note_id(self) -> str:
        return f"{DOMAIN}_{self._sn}_cut_height"

    @callback
    def _handle_coordinator_update(self) -> None:
        current = self.entity_description.value_fn(self.data.get("settings") or {})
        if current is not None:
            if self._pending is not None and int(current) == self._pending:
                self._pending = None
                self._cancel_confirm()
            if self._failed is not None and int(current) == self._failed:
                # Arrived late after all: take the warning back.
                self._failed = None
                persistent_notification.async_dismiss(self.hass, self._note_id)
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_confirm()
        await super().async_will_remove_from_hass()
