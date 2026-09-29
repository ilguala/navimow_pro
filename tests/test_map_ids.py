"""Which map to download: get-location's ids, or map-list's when those are unusable.

#22: a first-generation H mower (seen on an H1500 and an H3000) reports its map
ids as 0. Taken for real ids, they loaded no map, while position and trail
worked.
"""
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN

MAP_LIST = [
    {"map_id": "0", "map_base_id": "0", "edittime": "1"},
    {"map_id": "2", "map_base_id": "9909624", "edittime": "1790104307"},
]


class Client:
    def __init__(self):
        self.asked = []

    def map_detail_plain(self, sn, map_id, map_base_id):
        self.asked.append((map_id, map_base_id))
        return None

    def map_detail(self, sn, map_id, map_base_id):
        return None


def asked_for(location, map_list=MAP_LIST):
    entry = SimpleNamespace(
        entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
    )
    co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
    co.client = Client()
    co._maybe_fetch_map({"location": location, "map_list": map_list})
    return co.client.asked


@pytest.mark.parametrize("zero", [0, "0", "", None])
def test_zero_or_missing_ids_fall_back_to_the_map_list(zero):
    assert asked_for({"map_id": zero, "map_base_id": zero}) == [("2", "9909624")]


def test_one_bad_id_is_enough_to_fall_back():
    assert asked_for({"map_id": "2", "map_base_id": 0}) == [("2", "9909624")]


def test_good_ids_from_get_location_are_used_as_they_are():
    assert asked_for({"map_id": "1", "map_base_id": 25085064}) == [("1", "25085064")]


def test_nothing_usable_anywhere_fetches_nothing():
    assert asked_for({"map_id": 0, "map_base_id": 0}, map_list=MAP_LIST[:1]) == []
    assert asked_for({"map_id": 0, "map_base_id": 0}, map_list=None) == []
