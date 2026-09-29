"""The mowed trail: kept zone by zone, and cleared only when a zone starts over.

#15: one trail for the whole lawn, under one cap, lost the start of a job that
ran over several days, and a rain hold's resume could be taken for a new job.
"""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.camera import _TRAIL_COLOR, _TRAIL_OPACITY, NavimowMapCamera
from custom_components.navimow_pro.const import (
    CONF_DEVICE_ID,
    CONF_VEHICLE_SN,
    STATE_DOCKED_CHARGING,
    STATE_MOWING,
    TRAIL_MIN_STEP_M,
)

DOCK, MOW = STATE_DOCKED_CHARGING, STATE_MOWING
STEP = TRAIL_MIN_STEP_M + 0.5
CAP = 40


class _Store:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data


class Mower:
    """A real coordinator, fed one poll at a time."""

    def __init__(self, monkeypatch, pct=None):
        monkeypatch.setattr(coord_mod, "TRAIL_MAX_POINTS", CAP)
        entry = SimpleNamespace(
            entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
        )
        self.co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
        self.pct = dict(pct or {})
        self.x = 0.0

    def poll(self, state, zone=None, moving=True, shapes=None):
        if moving:
            self.x += STEP
        cov = {"zones": [{"id": z, "pct": p} for z, p in self.pct.items()]} if self.pct else None
        return self.co._update_trail(
            {"x": self.x, "y": 0.0}, state, zone, self.pct.get(zone), cov, shapes
        )

    def mow(self, n, zone, pct=None):
        if pct is not None:
            self.pct[zone] = pct
        for _ in range(n):
            self.poll(MOW, zone)

    def n(self, zone):
        return len(self.co._trail_zones.get(zone) or [])


def test_a_zone_keeps_its_trail_while_others_are_cut(monkeypatch):
    m = Mower(monkeypatch, {1: 0, 4: 0})
    m.poll(DOCK, moving=False)
    m.mow(CAP, zone=1, pct=60)
    m.poll(DOCK, moving=False)
    m.mow(3 * CAP, zone=4, pct=70)
    assert m.n(1) == CAP
    assert m.n(4) == CAP


def test_resuming_into_a_zone_at_zero_is_not_a_new_job(monkeypatch):
    m = Mower(monkeypatch, {1: 0, 2: 0})
    m.poll(DOCK, moving=False)
    m.mow(10, zone=1, pct=100)
    m.poll(DOCK, moving=False)
    m.mow(4, zone=2, pct=0)
    assert m.n(1) == 10 and m.n(2) == 4


def test_only_the_zone_started_over_loses_its_trail(monkeypatch):
    m = Mower(monkeypatch, {1: 0, 4: 0})
    m.poll(DOCK, moving=False)
    m.mow(10, zone=1, pct=100)
    m.mow(10, zone=4, pct=100)
    m.poll(DOCK, moving=False)
    m.pct[1] = 0
    m.poll(MOW, zone=1)
    assert m.n(1) == 1 and m.n(4) == 10


def test_a_late_reset_keeps_the_points_of_the_new_job(monkeypatch):
    m = Mower(monkeypatch, {1: 0})
    m.poll(DOCK, moving=False)
    m.mow(10, zone=1, pct=100)
    m.poll(DOCK, moving=False)
    m.mow(3, zone=1)  # the cloud still says 100
    m.mow(1, zone=1, pct=0)
    assert m.n(1) == 4


def test_a_reset_seen_in_the_dock_clears_the_zone(monkeypatch):
    m = Mower(monkeypatch, {1: 0})
    m.poll(DOCK, moving=False)
    m.mow(10, zone=1, pct=100)
    m.poll(DOCK, moving=False)
    m.pct[1] = 0
    m.poll(DOCK, moving=False)
    assert m.n(1) == 0


@pytest.mark.parametrize(("after", "cleared"), [(70, False), (30, True), (0, True)])
def test_only_a_real_fall_is_a_restart(monkeypatch, after, cleared):
    m = Mower(monkeypatch, {4: 0})
    m.poll(DOCK, moving=False)
    m.mow(10, zone=4, pct=73)
    m.poll(DOCK, moving=False)
    m.pct[4] = after
    m.poll(DOCK, moving=False)
    assert (m.n(4) == 0) is cleared


def test_a_trip_to_a_zone_is_kept_with_that_zone(monkeypatch):
    m = Mower(monkeypatch, {4: 10})
    m.poll(DOCK, moving=False)
    m.mow(3, zone=4)
    m.mow(2, zone=None)  # a moment with no zone reported
    assert m.n(4) == 5 and m.n(0) == 0


def test_without_coverage_a_new_job_is_seen_leaving_the_dock(monkeypatch):
    m = Mower(monkeypatch)

    def cut(progress):
        m.x += STEP
        m.co._update_trail({"x": m.x, "y": 0.0}, MOW, None, progress)

    m.poll(DOCK, moving=False)
    for _ in range(10):
        cut(30.0)
    m.poll(DOCK, moving=False)
    cut(0.0)
    assert m.n(0) == 1


def test_the_store_round_trips(monkeypatch):
    a = Mower(monkeypatch)
    a.co._trail_zones = {1: [[1.0, 1.0], [2.0, 2.0]], 4: [[3.0, 3.0]]}
    a.co._zone_pct = {1: 100, 4: 55}
    saved = json.loads(json.dumps(a.co._trail_store_data()))
    b = Mower(monkeypatch)
    b.co._trail_store = _Store(saved)
    asyncio.run(b.co.async_load_trail())
    assert b.co._trail_zones == a.co._trail_zones
    assert b.co._zone_pct == a.co._zone_pct


def test_an_old_single_trail_is_shared_among_the_zones(monkeypatch):
    m = Mower(monkeypatch)
    m.co._trail_store = _Store(
        {"sn": "SN1", "trail": [[1, 1], [2, 2], [21, 1], [50, 50], [25, 5]]}
    )
    asyncio.run(m.co.async_load_trail())
    shapes = [
        {"id": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        {"id": 2, "polygon": [[20, 0], [30, 0], [30, 10], [20, 10]]},
    ]
    m.poll(DOCK, moving=False, shapes=shapes)
    assert m.co._trail_zones[1] == [[1.0, 1.0], [2.0, 2.0]]
    assert m.co._trail_zones[2] == [[21.0, 1.0], [25.0, 5.0]]
    assert m.co._trail_zones[0] == [[50.0, 50.0]]


# ---------------------------------------------------------------- the map


def zone(zid, x0, x1):
    return {
        "id": zid,
        "name": f"Zone {zid}",
        "polygon": [[x0, 0], [x1, 0], [x1, 10], [x0, 10]],
        "boundary_flags": [1, 1, 1, 1],
    }


def map_data(**over):
    line1 = [[1 + i * 0.5, 2.0] for i in range(15)]
    line2 = [[21 + i * 0.5, 2.0] for i in range(15)]
    data = {
        "state": "Mowing",
        "battery": 80,
        "position": {"x": 25, "y": 5, "heading": 0},
        "coverage": {"zones": [{"id": 1, "pct": 100}, {"id": 2, "pct": 40}]},
        "trail_zones": {1: line1, 2: line2},
        "trail": line1 + line2,
        "map": {"zones": [zone(1, 0, 10), zone(2, 20, 30)], "station": {"x": 15, "y": 12}},
    }
    data.update(over)
    return data


def render(data):
    camera = object.__new__(NavimowMapCamera)
    camera.coordinator = SimpleNamespace(data=data)
    return camera.camera_image().decode()


def trail_group(svg):
    return re.search(r'<g opacity="%s"[^>]*>(.*?)</g>' % _TRAIL_OPACITY, svg).group(1)


def mower_at(svg):
    m = re.search(r'<g transform="translate\(([\d.]+),([\d.]+)\) rotate', svg)
    return float(m.group(1)), float(m.group(2))


def test_a_finished_zone_is_painted_mowed_and_its_trail_left_out():
    group = trail_group(render(map_data()))
    assert group.count(f'fill="{_TRAIL_COLOR}"') == 1
    assert group.count("<polyline") == 1  # zone 2's trail only


def test_a_docked_mower_is_drawn_on_the_dock():
    docked = render(map_data(docked=True, position={"x": 25, "y": 5, "heading": 0}))
    at_dock = render(map_data(position={"x": 15, "y": 12, "heading": 0}))
    assert mower_at(docked) == mower_at(at_dock)


def test_labels_stay_put_when_the_mower_drives_past():
    pills = lambda svg: re.findall(r'<rect x="([\d.]+)" y="([\d.]+)"[^>]*fill="#eceff1"', svg)  # noqa: E731
    a = render(map_data(position={"x": 25, "y": 5, "heading": 0}))
    b = render(map_data(position={"x": 26, "y": 6, "heading": 0}))
    assert pills(a) and pills(a) == pills(b)
    last_pill = max(m.start() for m in re.finditer(r'<rect [^>]*fill="#eceff1"', b))
    mower = re.search(r'<g transform="translate\([\d.]+,[\d.]+\) rotate', b).start()
    assert mower > last_pill  # drawn over the labels
