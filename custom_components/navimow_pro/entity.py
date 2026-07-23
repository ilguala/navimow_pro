"""Shared base entity for Navimow (Private).

Ties every entity to the single mower device (keyed by vehicle_sn) and to the
coordinator's parsed snapshot.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import NavimowCoordinator


class NavimowEntity(CoordinatorEntity[NavimowCoordinator]):
    """Base class: all entities live under one HA device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavimowCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._sn = coordinator.sn
        self._attr_unique_id = f"{self._sn}_{key}"

    @property
    def data(self) -> dict:
        """The current parsed snapshot (may be empty before first refresh)."""
        return self.coordinator.data or {}

    @property
    def device_info(self) -> DeviceInfo:
        data = self.data
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            manufacturer=MANUFACTURER,
            name=data.get("name") or "Navimow",
            model=data.get("model") or None,
            serial_number=self._sn,
        )

    @property
    def available(self) -> bool:
        # Available while the coordinator is succeeding; per-entity platforms
        # may further gate on whether their specific field is present.
        return super().available and bool(self.coordinator.data)
