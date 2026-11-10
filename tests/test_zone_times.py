"""When each zone was last mowed and last completed, kept across restarts."""
import asyncio
from types import SimpleNamespace

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN
from custom_components.navimow_pro.coordinator import _parse_coverage, _unix, _zone_times

NOW = 2_000_000.0


def zone(zid=4, fin=50.0, pct=50, end=None, name="Front"):
    return {"id": zid, "name": name, "finished": fin, "pct": pct, "end": end}


def test_first_sight_takes_the_end_time():
    times = _zone_times({}, [zone(pct=100, fin=100.0, end=1_900_000)], NOW)
    assert times[4]["mowed"] == 1_900_000 and times[4]["completed"] == 1_900_000
    assert times[4]["name"] == "Front"


def test_first_sight_without_an_end_time_knows_nothing_yet():
    times = _zone_times({}, [zone(pct=100, fin=100.0)], NOW)
    assert times[4]["mowed"] is None and times[4]["completed"] is None


def test_growing_area_is_a_mow_now():
    before = _zone_times({}, [zone(fin=50.0)], NOW - 60)
    times = _zone_times(before, [zone(fin=52.0)], NOW)
    assert times[4]["mowed"] == NOW and times[4]["completed"] is None


def test_jitter_is_not_a_mow():
    before = _zone_times({}, [zone(fin=50.0)], NOW - 60)
    assert _zone_times(before, [zone(fin=50.05)], NOW)[4]["mowed"] is None


def test_reaching_100_without_an_end_time_completes_now():
    before = _zone_times({}, [zone(fin=98.0, pct=98)], NOW - 60)
    times = _zone_times(before, [zone(fin=100.0, pct=100)], NOW)
    assert times[4]["completed"] == NOW and times[4]["mowed"] == NOW


def test_a_later_end_time_moves_both_on():
    before = {4: {"name": "Front", "mowed": 1_000.0, "completed": 1_000.0, "finished": 100.0, "pct": 100}}
    times = _zone_times(before, [zone(fin=100.0, pct=100, end=1_500)], NOW)
    assert times[4]["mowed"] == 1_500 and times[4]["completed"] == 1_500


def test_a_zone_started_over_keeps_its_dates():
    before = {4: {"name": "Front", "mowed": 1_500.0, "completed": 1_500.0, "finished": 100.0, "pct": 100}}
    times = _zone_times(before, [zone(fin=0.0, pct=0)], NOW)
    assert times[4]["mowed"] == 1_500 and times[4]["completed"] == 1_500
    assert times[4]["pct"] == 0 and times[4]["finished"] == 0.0


def test_a_partial_session_end_is_a_mow_not_a_completion():
    times = _zone_times({}, [zone(fin=40.0, pct=40, end=1_800_000)], NOW)
    assert times[4]["mowed"] == 1_800_000 and times[4]["completed"] is None


def test_an_end_time_in_the_future_is_capped_at_now():
    assert _zone_times({}, [zone(end=NOW + 500)], NOW)[4]["mowed"] == NOW


def test_milliseconds_and_nothing():
    assert _unix(1_794_153_475_000) == 1_794_153_475
    assert _unix(1_794_153_475) == 1_794_153_475
    assert _unix(0) is None and _unix(None) is None and _unix("x") is None


def test_zones_no_longer_reported_are_dropped():
    before = _zone_times({}, [zone(4, end=1_000), zone(5, end=1_000, name="Back")], NOW)
    assert set(_zone_times(before, [zone(4, end=1_000)], NOW)) == {4}


def test_coverage_carries_the_end_time():
    cov = _parse_coverage([{"partitionId": 4, "area": 10, "finishedArea": 5,
                            "partitionPercentage": 50, "endTime": 1_234}], {4: "Front"})
    assert cov["zones"][0]["end"] == 1_234.0


# ---------------------------------------------------------------- coordinator


def make():
    entry = SimpleNamespace(entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"})
    return coord_mod.NavimowCoordinator(SimpleNamespace(), entry)


def test_no_coverage_answer_keeps_the_dates():
    co = make()
    co._update_zone_times({"zones": [zone(pct=100, fin=100.0, end=1_000)]})
    assert co._trail_dirty
    co._trail_dirty = False
    snap = co._update_zone_times(None)
    assert snap == {4: {"name": "Front", "mowed": 1_000.0, "completed": 1_000.0}}
    assert not co._trail_dirty


def test_the_dates_survive_a_restart():
    co = make()
    co._update_zone_times({"zones": [zone(pct=100, fin=100.0, end=1_000)]})
    saved = co._trail_store_data()

    class Store:
        async def async_load(self):
            return saved

    again = make()
    again._trail_store = Store()
    asyncio.run(again.async_load_trail())
    assert again._update_zone_times(None) == {4: {"name": "Front", "mowed": 1_000.0, "completed": 1_000.0}}
