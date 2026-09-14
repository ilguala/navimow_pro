"""Device tracker platform for Navimow (Private).

Puts the mower on Home Assistant's own map, and makes it usable in zone and
proximity triggers -- "tell me if he leaves the property" becomes one automation
instead of a template over the position sensors.

Nothing extra is fetched for this: the coordinates are the ones the cloud already
reports on every poll, which until now the integration parsed and threw away.
"""
from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowDeviceTracker(coordinator)])


class NavimowDeviceTracker(NavimowEntity, TrackerEntity):
    """The mower's geographic position, as a marker on the HA map."""

    # Carries the device's own name, so the map reads "Navimow" rather than
    # "Navimow Location".
    _attr_name = None

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator, "location")

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def force_update(self) -> bool:
        """The position is polled, not pushed.

        TrackerEntity defaults this to ``not should_poll`` -- True here -- because
        a push tracker must record every report it receives. This one is written
        on every coordinator refresh whether or not anything moved, so leaving it
        True would write a recorder row and reset last_changed every 3 s while
        cutting, for a mower sitting still. Must be a property: TrackerEntity
        declares its own, which shadows the ``_attr_force_update`` fallback.
        """
        return False

    @property
    def _coords(self) -> tuple[float, float] | None:
        """Latitude/longitude, or ``None`` when the cloud has nothing usable.

        A mower that has never had a fix reports 0/0, and 0/0 is a real place in
        the Gulf of Guinea: published as-is it would drop the marker off the coast
        of Africa and fire every "has he left the garden?" automation at once.
        Out-of-range values are refused for the same reason -- a wrong position is
        worse than no position, because only one of the two looks like an answer.

        Deliberately not gated on ``available``: a poll that happens to carry no
        location should leave the entity reading unknown, not flap the whole
        entity in and out of existence.
        """
        lat = self.data.get("latitude")
        lon = self.data.get("longitude")
        if lat is None or lon is None:
            return None
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            return None
        if lat == 0.0 and lon == 0.0:
            return None
        return lat, lon

    @property
    def latitude(self) -> float | None:
        coords = self._coords
        return coords[0] if coords else None

    @property
    def longitude(self) -> float | None:
        coords = self._coords
        return coords[1] if coords else None
