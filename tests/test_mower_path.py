"""The mower's own swept path (beta): decoding it, when it is read, how it is drawn.

The answer's shape follows what vahesoo's NaviMower decodes from the same call:
base64 of a zstd frame of JSON, one row per zone with points {x, y, kr, pid, pt}.
"""
import base64
import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest
import zstandard

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.camera import _TRAIL_OPACITY, NavimowMapCamera
from custom_components.navimow_pro.const import (
    CONF_DEVICE_ID,
    CONF_VEHICLE_SN,
    STATE_DOCKED_CHARGING,
    STATE_MOWING,
    STATE_RETURNING,
    TRAIL_MAX_POINTS,
)
from custom_components.navimow_pro.coordinator import (
    _decode_path_data,
    _parse_path_row,
    _path_runs,
)


def row(zone, points, pct=40, start=1000):
    return {"partitionId": zone, "partitionPercentage": pct, "startTime": start,
            "endTime": start + 99, "points": points}


def pt(x, y, kr="01"):
    return {"x": x, "y": y, "kr": kr, "pid": "1", "pt": "04"}


def encode(rows):
    raw = json.dumps(rows, separators=(",", ":")).encode()
    return base64.b64encode(zstandard.ZstdCompressor().compress(raw)).decode()


# ---------------------------------------------------------------- decoding


def test_the_compressed_answer_decodes():
    rows = [row(4, [pt(1, 2), pt(1.25, 2)])]
    assert _decode_path_data(encode(rows)) == rows


def test_nothing_and_garbage_are_told_apart():
    assert _decode_path_data(None) == []
    assert _decode_path_data("bm90IHpzdGQ=") is None  # base64 of "not zstd"
    assert _decode_path_data([row(1, [])]) == [row(1, [])]


def test_kr_becomes_the_blade_flag():
    parsed = _parse_path_row(row(4, [pt(1, 2, "01"), pt(2, 2, "00"), pt(3, 2, "")]))
    assert parsed["points"] == [[1.0, 2.0, True], [2.0, 2.0, False], [3.0, 2.0, None]]
    assert parsed["blade_off"] == 1 and parsed["zone"] == 4 and parsed["start"] == 1000


def test_a_row_without_a_zone_is_dropped():
    assert _parse_path_row(row(0, [pt(1, 1)])) is None


def test_thinning_keeps_where_the_blade_changes():
    many = [pt(i * 0.01, 0, "00" if i == 12345 else "01") for i in range(3 * TRAIL_MAX_POINTS)]
    parsed = _parse_path_row(row(1, many))
    assert len(parsed["points"]) <= TRAIL_MAX_POINTS + 50
    assert parsed["blade_off"] == 1


# ---------------------------------------------------------------- stretches


POINTS = [[0, 0, True], [1, 0, True], [2, 0, False], [3, 0, True], [4, 0, True]]


def test_a_stopped_blade_breaks_the_stretch():
    assert _path_runs(POINTS, True, []) == [[[0, 0], [1, 0]], [[3, 0], [4, 0]]]


def test_without_blade_gaps_it_is_one_stretch():
    assert _path_runs(POINTS, False, []) == [[[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]]


def test_the_live_tail_joins_the_path():
    runs = _path_runs(POINTS, True, [[5, 0], [6, 0]])
    assert runs[-1] == [[4, 0], [5, 0], [6, 0]]


def test_a_tail_after_a_stopped_blade_is_not_joined():
    runs = _path_runs([[0, 0, True], [1, 0, False]], True, [[2, 0]])
    assert runs[-1] == [[2, 0]]


# ---------------------------------------------------------------- when


class Client:
    def __init__(self):
        self.state = STATE_DOCKED_CHARGING
        self.zone = 4
        self.cov = {4: (40, 1000), 7: (0, 900)}
        self.asked = []
        self.fail = None

    def index2(self, sn):
        return {"vehicle_state": self.state, "soc": 80}

    def location(self, sn, vtype):
        return {"map_work_position": "%08x%08x%08x%08x%08x" % (8, 6, 0, self.zone, 4000),
                "posture_x": 1.0, "posture_y": 1.0}

    def path_info_time(self, sn):
        return [{"partitionId": z, "partitionPercentage": p, "startTime": s, "area": 10,
                 "finishedArea": p / 10} for z, (p, s) in self.cov.items()]

    def path_data(self, sn, zones):
        self.asked.append(list(zones))
        if self.fail:
            raise self.fail
        return encode([row(z, [pt(1, 1), pt(1.5, 1)], start=self.cov[z][1]) for z in zones])

    def __getattr__(self, name):
        return lambda *a, **k: {}


@pytest.fixture
def mower(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        coord_mod, "dt_util",
        SimpleNamespace(now=lambda: dt.datetime.now(dt.timezone.utc), as_local=lambda t: t),
    )

    def build(options=None):
        entry = SimpleNamespace(
            entry_id="e1", options=options or {}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
        )
        co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
        co.client = Client()

        def poll(state=None, after=12.0):
            clock["t"] += after
            if state:
                co.client.state = state
            return co._fetch_blocking()

        co.poll = poll
        return co

    return build


def test_read_once_after_a_restart_for_zones_with_progress(mower):
    co = mower()
    snap = co.poll()
    assert co.client.asked == [[4]]  # zone 7 is at 0 %
    assert snap["path_status"]["state"] == "ok"
    assert 4 in snap["path_zones"]
    co.poll()
    assert co.client.asked == [[4]]


def test_while_mowing_once_a_minute_for_the_zone_being_cut(mower):
    co = mower()
    co.poll()
    co.poll(STATE_MOWING, after=30)
    assert len(co.client.asked) == 1  # too soon after the first read
    co.poll(after=40)
    assert co.client.asked[-1] == [4]
    co.poll(after=10)
    assert len(co.client.asked) == 2


def test_once_more_when_the_mower_docks(mower):
    co = mower()
    co.poll(STATE_MOWING)
    co.poll(STATE_RETURNING, after=70)
    n = len(co.client.asked)
    co.poll(STATE_DOCKED_CHARGING)
    assert len(co.client.asked) == n + 1
    co.poll()
    assert len(co.client.asked) == n + 1


def test_a_zone_started_over_loses_its_path(mower):
    co = mower()
    co.poll()
    co.client.cov[4] = (0, 2000)
    snap = co.poll()
    assert 4 not in snap["path_zones"]


def test_a_new_cycle_on_a_zone_loses_the_old_path(mower):
    co = mower()
    co.poll()
    co.client.cov[4] = (5, 2000)
    snap = co.poll()
    assert 4 not in snap["path_zones"]


def test_a_failure_keeps_what_was_read(mower):
    co = mower()
    co.poll()
    co.client.fail = coord_mod.NavimowError(500, "busy")
    snap = co.poll(STATE_MOWING, after=70)
    assert 4 in snap["path_zones"]
    assert snap["path_status"]["state"] == "error 500"


def test_turned_off_it_is_never_asked(mower):
    co = mower({"mower_path": False})
    snap = co.poll()
    assert co.client.asked == []
    assert snap["path_zones"] == {} and snap["path_status"] == {"state": "off"}


def test_blade_gaps_can_be_turned_off(mower):
    assert mower({"blade_gaps": False}).blade_gaps is False
    assert mower().blade_gaps is True


# ---------------------------------------------------------------- the map


def zone(zid, x0, x1):
    return {"id": zid, "name": f"Zone {zid}", "polygon": [[x0, 0], [x1, 0], [x1, 10], [x0, 10]],
            "boundary_flags": [1, 1, 1, 1]}


def render(data):
    camera = object.__new__(NavimowMapCamera)
    camera.coordinator = SimpleNamespace(data=data)
    return camera.camera_image().decode()


def lines(svg):
    group = re.search(r'<g opacity="%s"[^>]*>(.*?)</g>' % _TRAIL_OPACITY, svg).group(1)
    return re.findall(r'<polyline points="([^"]+)"', group)


def test_the_mowers_path_replaces_the_rebuilt_trail_for_its_zone():
    own = [[1 + i * 0.5, 2.0] for i in range(10)]
    data = {
        "state": "Mowing", "battery": 80, "position": {"x": 25, "y": 5, "heading": 0},
        "coverage": {"zones": [{"id": 1, "pct": 40}, {"id": 2, "pct": 40}]},
        "map": {"zones": [zone(1, 0, 10), zone(2, 20, 30)]},
        "trail_zones": {1: own, 2: [[21 + i * 0.5, 8.0] for i in range(10)]},
        "trail": own,
        "path_zones": {2: [[[21, 2], [22, 2], [23, 2]], [[25, 2], [26, 2], [27, 2]]]},
    }
    drawn = lines(render(data))
    assert len(drawn) == 3  # zone 1's own trail + zone 2's two stretches
    with_path = render(dict(data, path_zones={}))
    assert len(lines(with_path)) == 2  # without it, both zones' own trails
