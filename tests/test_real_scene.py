"""The real-scene overlay: parsing the package and drawing it."""
import base64
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.camera import NavimowMapCamera
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN
from custom_components.navimow_pro.coordinator import NavimowError, _fetch_terrain

SVG = "{http://www.w3.org/2000/svg}"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-image-bytes"


def terrain_zip(bounds=None, with_image=True, with_json=True):
    meta = {"minX": -11.3, "maxX": 26.9, "minY": -8.1, "maxY": 25.7, "pixel_per_meter": 30}
    if bounds:
        meta.update(bounds)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_json:
            zf.writestr("upload/merge2d_20261110.json", json.dumps(meta))
        if with_image:
            zf.writestr("upload/merge2d_20261110.webp", WEBP)
            zf.writestr("upload/adapt_20261110.webp", b"other")
    return buf.getvalue()


# ---------------------------------------------------------------- parsing


def test_the_package_gives_the_image_and_its_bounds(monkeypatch):
    blob = terrain_zip()
    monkeypatch.setattr(coord_mod.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(blob).__enter__ and _ctx(blob))
    out = _fetch_terrain("http://x/terrain.zip")
    assert out["bounds"] == {"minX": -11.3, "maxX": 26.9, "minY": -8.1, "maxY": 25.7}
    assert out["image"] == "data:image/webp;base64," + base64.b64encode(WEBP).decode()


class _ctx:
    def __init__(self, blob):
        self.blob = blob
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self, n=-1):
        return self.blob


@pytest.mark.parametrize("blob", [
    terrain_zip(with_image=False),
    terrain_zip(with_json=False),
    terrain_zip(bounds={"minX": 5.0, "maxX": 5.0}),  # empty span
    b"not a zip",
])
def test_a_broken_package_is_no_terrain(monkeypatch, blob):
    monkeypatch.setattr(coord_mod.urllib.request, "urlopen", lambda *a, **k: _ctx(blob))
    assert _fetch_terrain("http://x") is None


def test_a_download_error_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise OSError("no net")
    monkeypatch.setattr(coord_mod.urllib.request, "urlopen", boom)
    assert _fetch_terrain("http://x") is None


# ---------------------------------------------------------------- when


class Client:
    def __init__(self):
        self.calls = 0
        self.answer = {"url": "http://x/t.zip", "version": 111}

    def index2(self, sn):
        return {"vehicle_state": "0101", "soc": 50}

    def iot_file(self, sn, file_type=2):
        self.calls += 1
        return self.answer

    def __getattr__(self, name):
        return lambda *a, **k: {}


@pytest.fixture
def mower(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(coord_mod, "_fetch_terrain",
                        lambda url: {"bounds": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}, "image": "data:img"})

    def build(opts):
        entry = SimpleNamespace(entry_id="e1", options=opts,
                                data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"})
        co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
        co.client = Client()
        co.clock = clock
        return co

    return build


def test_off_by_default_it_is_never_asked(mower):
    co = mower({})
    co._maybe_fetch_terrain()
    assert co.client.calls == 0


def test_on_it_reads_once_then_only_as_the_version_changes(mower):
    co = mower({"real_scene": True})
    co._maybe_fetch_terrain()
    assert co.client.calls == 1 and co._terrain["image"] == "data:img"
    co.clock["t"] += 10_000  # past the refresh window
    co._maybe_fetch_terrain()
    assert co.client.calls == 2  # asked again
    # same version -> keeps what it has, no re-parse flag change
    assert co._terrain_version == 111


def test_a_mower_without_a_terrain_shows_nothing(mower):
    co = mower({"real_scene": True})
    co.client.answer = {}  # no such view: no URL
    co._maybe_fetch_terrain()
    assert co._terrain is None


def test_an_api_error_does_not_raise(mower):
    co = mower({"real_scene": True})
    def boom(sn, file_type=2):
        raise NavimowError(500, "busy")
    co.client.iot_file = boom
    co._maybe_fetch_terrain()  # must not raise
    assert co._terrain is None


# ---------------------------------------------------------------- drawing


def render(data):
    camera = object.__new__(NavimowMapCamera)
    camera.coordinator = SimpleNamespace(data=data)
    return camera.camera_image().decode()


def zone(zid, x0, y0, x1, y1):
    return {"id": zid, "name": f"Zone {zid}", "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


IMG = "data:image/webp;base64,AAAA"


def test_the_real_scene_is_drawn_whole_under_the_map():
    data = {
        "map": {"zones": [zone(1, 0, 0, 10, 10)]},
        "position": {"x": 5, "y": 5},
        "real_scene": {"bounds": {"minX": -2, "maxX": 12, "minY": -2, "maxY": 12}, "image": IMG},
    }
    svg = render(data)
    root = ET.fromstring(svg)
    imgs = list(root.iter(f"{SVG}image"))
    assert len(imgs) == 1
    assert imgs[0].get("href") == IMG
    # not clipped to the zones: the scan reaches past them
    assert imgs[0].get("clip-path") is None
    # the image sits under the zones: before the green zone fill in document order,
    # and with the photo on the zone tint drops to a light veil
    assert svg.index("<image") < svg.index('fill-opacity="0.08"')


def test_the_mowed_veil_is_lighter_over_the_real_scene():
    cov = {"zones": [{"id": 1, "pct": 100}]}
    base = {"map": {"zones": [zone(1, 0, 0, 10, 10)]}, "position": {"x": 5, "y": 5},
            "coverage": cov}
    without = render(base)
    withimg = render(dict(base, real_scene={
        "bounds": {"minX": 0, "maxX": 10, "minY": 0, "maxY": 10}, "image": IMG}))
    assert '<g opacity="0.4"' in without  # the normal mowed layer
    assert '<g opacity="0.2"' in withimg  # lighter when the photo is under it


def test_no_real_scene_no_image():
    svg = render({"map": {"zones": [zone(1, 0, 0, 10, 10)]}, "position": {"x": 5, "y": 5}})
    assert "<image" not in svg


def test_a_real_scene_is_drawn_even_without_zones():
    # The scan is its own footprint; it does not need zone geometry to show.
    data = {"position": {"x": 5, "y": 5}, "trail": [[0, 0], [1, 1]],
            "real_scene": {"bounds": {"minX": 0, "maxX": 4, "minY": 0, "maxY": 4}, "image": IMG}}
    assert "<image" in render(data)


def test_the_mower_shrinks_when_the_view_widens_past_the_lawn():
    import re
    zones = {"map": {"zones": [zone(1, 0, 0, 10, 10)]}, "position": {"x": 5, "y": 5, "heading": 0},
             "coverage": {"zones": [{"id": 1, "pct": 40}]}}
    tight = render(zones)
    wide = render(dict(zones, real_scene={
        "bounds": {"minX": -20, "maxX": 30, "minY": -20, "maxY": 30}, "image": IMG}))

    def mscale(svg):
        return float(re.search(r'rotate\([-\d.]+\) scale\(([\d.]+)\)', svg).group(1))

    assert mscale(tight) > mscale(wide)  # the wider real-scene view draws a smaller mower
    assert mscale(wide) >= 0.5           # but not below the floor
