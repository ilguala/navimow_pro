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
    _PATH_SUMMARY_VALUES,
    _decode_path_data,
    _parse_path_row,
    _path_runs,
    _point_drawn,
)


def row(zone, points, pct=40, start=1000):
    return {"partitionId": zone, "partitionPercentage": pct, "startTime": start,
            "endTime": start + 99, "points": points}


def pt(x, y, kr="01", kind="04"):
    return {"x": x, "y": y, "kr": kr, "pid": "1", "pt": kind}


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


@pytest.mark.parametrize(
    ("kr", "kind", "drawn"),
    [
        ("00", "00", True),  # types 00 and 01 are drawn whatever the blade
        ("00", "01", True),
        ("01", "02", True),  # any other type with the blade turning
        ("01", "04", True),
        ("00", "02", False),  # ... and not with it stopped
        ("00", "04", False),
        ("01", "06", False),  # 06 always breaks the line
        ("00", "06", False),
        ("", "06", False),
        ("01", "", True),
        ("00", "", False),
        ("", "04", False),
        ("1", "04", False),  # exact strings, as the app compares them
        ("", "", None),  # no flags at all: drawn, as before they were known
    ],
)
def test_the_apps_rule_for_path_points(kr, kind, drawn):
    assert _point_drawn(kr, kind) is drawn


def test_the_flags_become_the_drawn_flag():
    parsed = _parse_path_row(row(4, [
        pt(1, 2, "01"), pt(2, 2, "00"), pt(3, 2, "00", "00"), pt(4, 2, "01", "06"),
        {"x": 5, "y": 2},
    ]))
    assert [p[2] for p in parsed["points"]] == [True, False, True, False, None]
    assert parsed["blade_off"] == 2 and parsed["left_out"] == 2
    assert parsed["zone"] == 4 and parsed["start"] == 1000


def test_vahesoos_six_point_sequence():
    # pt/kr: 00/00, 02/00, 02/00, 01/00, 06/01, 02/01 -> the app draws [0], [3], [5].
    flags = [("00", "00"), ("00", "02"), ("00", "02"), ("00", "01"), ("01", "06"), ("01", "02")]
    parsed = _parse_path_row(row(1, [pt(i, 0, kr, kind) for i, (kr, kind) in enumerate(flags)]))
    assert _path_runs(parsed["points"], True, [])["path"] == [[[0, 0]], [[3, 0]], [[5, 0]]]


def test_a_row_without_a_zone_is_dropped():
    assert _parse_path_row(row(0, [pt(1, 1)])) is None


def test_thinning_keeps_where_the_blade_changes():
    many = [pt(i * 0.01, 0, "00" if i == 12345 else "01") for i in range(3 * TRAIL_MAX_POINTS)]
    parsed = _parse_path_row(row(1, many))
    assert len(parsed["points"]) <= TRAIL_MAX_POINTS + 50
    assert parsed["blade_off"] == 1 and parsed["left_out"] == 1


# ---------------------------------------------------------------- summary


def test_the_summary_tells_what_the_path_is_made_of():
    points = [
        dict(pt(1.25, 2.75), pid=1, pt="00", timestamp=500),
        dict(pt(1.5, 2.75), pid=1, pt="04", timestamp=0),
        dict(pt(1.75, 2.75), pid=2, pt="04", timestamp=900),
        dict(pt(2.25, 2.75), pid=2, pt="04", timestamp=700),
    ]
    summary = _parse_path_row(dict(row(4, points), extra=[1]))["summary"]
    assert summary["row"] == {"partitionId": 4, "partitionPercentage": 40,
                              "startTime": 1000, "endTime": 1099}
    assert summary["row_lists"] == ["extra"]
    assert summary["points"] == 4
    assert summary["point_fields"] == ["kr", "pid", "pt", "timestamp", "x", "y"]
    assert summary["pid"] == {"1": 2, "2": 2} and summary["pid_changes"] == 1
    assert summary["pt"] == {"04": 3, "00": 1}
    assert summary["kr_pt"] == {"01/04": 3, "01/00": 1}
    assert summary["spacing_m"] == {"median": 0.25, "max": 0.5, "over_2": 0}
    assert summary["pid_times"] == {"1": [500.0, 500.0], "2": [700.0, 900.0]}
    # Not a single coordinate.
    assert not any(c in str(summary) for c in ("1.25", "1.75", "2.25", "2.75"))


def test_the_summary_without_times_and_with_many_values():
    points = [dict(pt(i, 0), pid=i) for i in range(_PATH_SUMMARY_VALUES + 5)]
    summary = _parse_path_row(row(1, points))["summary"]
    assert "pid_times" not in summary
    assert len(summary["pid"]) == _PATH_SUMMARY_VALUES + 1
    assert summary["pid"]["<more>"] == 5
    assert summary["pid_changes"] == _PATH_SUMMARY_VALUES + 4
    assert summary["spacing_m"] == {"median": 1.0, "max": 1.0, "over_2": 0}


def test_the_summary_counts_long_steps():
    summary = _parse_path_row(row(1, [pt(0, 0), pt(0, 1), pt(0, 5), pt(0, 13)]))["summary"]
    assert summary["spacing_m"] == {"median": 4.0, "max": 8.0, "over_2": 2}
    assert "spacing_m" not in _parse_path_row(row(1, [pt(0, 0)]))["summary"]


# ---------------------------------------------------------------- stretches


POINTS = [[0, 0, True], [1, 0, True], [2, 0, False], [3, 0, True], [4, 0, True]]


def test_a_point_left_out_breaks_the_stretch():
    assert _path_runs(POINTS, True, []) == {"path": [[[0, 0], [1, 0]], [[3, 0], [4, 0]]], "tail": []}


def test_without_blade_gaps_it_is_one_stretch():
    assert _path_runs(POINTS, False, [])["path"] == [[[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]]


def test_the_live_tail_joins_the_path():
    runs = _path_runs(POINTS, True, [[5, 0], [6, 0]])
    assert runs["tail"] == [[4, 0], [5, 0], [6, 0]]


def test_a_tail_after_a_point_left_out_is_not_joined():
    runs = _path_runs([[0, 0, True], [1, 0, False]], True, [[2, 0]])
    assert runs["tail"] == [[2, 0]]


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
    assert snap["path_status"]["zones"][4]["summary"]["pid"] == {"1": 2}
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
        "path_zones": {2: {"path": [[[21, 2], [22, 2], [23, 2]], [[25, 2], [26, 2], [27, 2]]],
                           "tail": []}},
    }
    drawn = lines(render(data))
    assert len(drawn) == 3  # zone 1's own trail + zone 2's two stretches
    with_path = render(dict(data, path_zones={}))
    assert len(lines(with_path)) == 2  # without it, both zones' own trails


def stripes(x0, y0):
    """Long passes with points only where the mower turns, 4 m apart."""
    return [[x0 + (4 if i % 4 in (1, 2) else 0), y0 + 0.3 * (i // 2)] for i in range(12)]


def test_the_mowers_path_is_not_split_where_its_points_lie_apart():
    data = {
        "state": "Docked", "battery": 80,
        "coverage": {"zones": [{"id": 1, "pct": 60}, {"id": 2, "pct": 60}]},
        "map": {"zones": [zone(1, 0, 10), zone(2, 20, 30)]},
        "trail_zones": {1: stripes(2, 2)},
        "path_zones": {2: {"path": [stripes(22, 2)], "tail": [[22, 6], [26, 6], [26.5, 6]]}},
    }
    drawn = [len(run.split()) for run in lines(render(data))]
    # Our own samples (zone 1) break at every 4 m jump, leaving five short
    # pieces; zone 2's path is one line through all twelve points; its tail,
    # our samples again, breaks too.
    assert drawn == [2, 2, 2, 2, 2, 12, 2]
