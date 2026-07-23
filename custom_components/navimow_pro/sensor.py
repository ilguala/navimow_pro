"""Sensor platform for Navimow (Private)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity


@dataclass(frozen=True, kw_only=True)
class NavimowSensorDescription(SensorEntityDescription):
    """Sensor description with a value extractor over the snapshot dict."""

    value_fn: Callable[[dict], Any]
    attrs_fn: Callable[[dict], dict | None] | None = None


def _schedule_summary(schedule: list | None) -> str | None:
    """Short human summary of the weekly schedule for the sensor state."""
    if not schedule:
        return None
    days = [d.get("weekday") for d in schedule if d.get("enabled") and d.get("periods")]
    return ", ".join(d for d in days if d) if days else "Off"


SENSORS: tuple[NavimowSensorDescription, ...] = (
    NavimowSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.get("battery"),
    ),
    NavimowSensorDescription(
        key="state",
        translation_key="state",
        icon="mdi:robot-mower",
        value_fn=lambda d: d.get("state"),
    ),
    NavimowSensorDescription(
        key="state_code",
        translation_key="state_code",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("state_code"),
    ),
    NavimowSensorDescription(
        key="mowing_progress",
        translation_key="mowing_progress",
        icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("mowing_progress"),
    ),
    NavimowSensorDescription(
        key="current_zone",
        translation_key="current_zone",
        icon="mdi:select-marker",
        value_fn=lambda d: d.get("current_zone"),
    ),
    NavimowSensorDescription(
        key="coverage",
        translation_key="coverage",
        icon="mdi:grid",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # Overall mowed % of the current/last session (per-zone in attributes).
        value_fn=lambda d: (d.get("coverage") or {}).get("overall_pct"),
        attrs_fn=lambda d: (
            {
                "total_area": (d.get("coverage") or {}).get("total_area"),
                "finished_area": (d.get("coverage") or {}).get("finished_area"),
                "zones": [
                    {
                        "name": z.get("name"),
                        "percentage": z.get("pct"),
                        "finished_area": z.get("finished"),
                        "area": z.get("area"),
                    }
                    for z in (d.get("coverage") or {}).get("zones") or []
                ],
            }
            if d.get("coverage")
            else None
        ),
    ),
    NavimowSensorDescription(
        key="session_area",
        translation_key="session_area",
        icon="mdi:texture-box",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("session_area"),
    ),
    NavimowSensorDescription(
        key="weekly_area",
        translation_key="weekly_area",
        icon="mdi:calendar-week",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.get("weekly_area"),
    ),
    NavimowSensorDescription(
        key="total_area",
        translation_key="total_area",
        icon="mdi:ruler-square",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("total_area"),
    ),
    NavimowSensorDescription(
        key="next_mow",
        translation_key="next_mow",
        icon="mdi:clock-start",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("next_mow"),
    ),
    NavimowSensorDescription(
        key="schedule",
        translation_key="schedule",
        icon="mdi:calendar-clock",
        value_fn=lambda d: _schedule_summary(d.get("schedule")),
        # `days` = parsed weekly plan; `zones` = available zones (id/name) so the
        # graphical scheduler card can offer a per-period zone picker.
        attrs_fn=lambda d: {
            "days": d.get("schedule"),
            "zones": [
                {"id": z.get("id"), "name": z.get("name")}
                for z in (d.get("zones") or [])
                if z.get("id") is not None
            ],
        },
    ),
    NavimowSensorDescription(
        key="error_text",
        translation_key="error_text",
        icon="mdi:alert-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("error_text"),
    ),
    NavimowSensorDescription(
        key="signal_wifi",
        translation_key="signal_wifi",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("signal_wifi"),
    ),
    NavimowSensorDescription(
        key="blades_life",
        translation_key="blades_life",
        icon="mdi:saw-blade",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("maintenance") or {}).get("blades_pct"),
        attrs_fn=lambda d: {
            "reminder_interval_hours": (d.get("maintenance") or {}).get("blades_set_hours"),
            "runtime_minutes": (d.get("maintenance") or {}).get("blades_used_min"),
        },
    ),
    NavimowSensorDescription(
        key="chassis_life",
        translation_key="chassis_life",
        icon="mdi:car-wrench",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("maintenance") or {}).get("chassis_pct"),
        attrs_fn=lambda d: {
            "reminder_interval_hours": (d.get("maintenance") or {}).get("chassis_set_hours"),
            "runtime_minutes": (d.get("maintenance") or {}).get("chassis_used_min"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NavimowSensor(coordinator, desc) for desc in SENSORS)


class NavimowSensor(NavimowEntity, SensorEntity):
    """A single value from the mower snapshot."""

    entity_description: NavimowSensorDescription

    def __init__(
        self, coordinator: NavimowCoordinator, description: NavimowSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.data)
