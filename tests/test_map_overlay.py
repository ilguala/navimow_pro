"""Legend and status as camera attributes, and the clean-map option."""
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from custom_components.navimow_pro.camera import (
    NavimowMapCamera,
    _map_legend,
    _map_mowed,
    _map_status,
)

SVG = "{http://www.w3.org/2000/svg}"


def data(**over):
    base = {
        "state": "Docked", "battery": 90,
        "coverage": {"overall_pct": 100, "zones": [{"id": 1, "pct": 100}]},
        "map": {"zones": [{"id": 1, "name": "Z", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
                "obstacles": [[[4, 4], [6, 4], [6, 6]]], "vision_off": [[[1, 1], [2, 1], [2, 2]]],
                "station": {"x": 1, "y": 1}},
        "position": {"x": 5, "y": 5, "heading": 0},
    }
    base.update(over)
    return base


def test_status_line():
    assert _map_status(data()) == "Docked · 90% · 100% mowed"
    assert _map_status(data(coverage={})) == "Docked · 90%"


def test_mowed_detection():
    assert _map_mowed(data()) is True  # a 100% zone
    assert _map_mowed(data(coverage={"zones": [{"id": 1, "pct": 40}]})) is False
    assert _map_mowed(data(coverage={}, trail=[[0, 0]])) is True


def test_legend_rows_follow_the_map():
    rows = _map_legend(data())
    assert [r["label"] for r in rows] == ["Mower", "Mowed", "Dock", "Off-limit area", "VisionFence off"]
    bare = _map_legend(data(coverage={}, map={"zones": []}))
    assert [r["label"] for r in bare] == ["Mower"]


def _cam(d, clean=False):
    cam = object.__new__(NavimowMapCamera)
    cam.coordinator = SimpleNamespace(data=d)
    cam._clean_map = clean
    cam._mower_scale = 1.0
    return cam


def test_attributes_carry_legend_and_status():
    attrs = _cam(data()).extra_state_attributes
    assert attrs["status"] == "Docked · 90% · 100% mowed"
    assert attrs["legend"][0] == {"color": "#ff6d00", "label": "Mower"}


def test_clean_map_leaves_the_legend_and_status_off_the_picture():
    full = _cam(data(), clean=False).camera_image().decode()
    clean = _cam(data(), clean=True).camera_image().decode()
    # the baked status text and the legend swatches are gone when clean
    assert "100% mowed" in full and "100% mowed" not in clean
    assert full.count("<rect") > clean.count("<rect")
    # the map itself is still drawn either way
    assert ET.fromstring(clean).find(f".//{SVG}polygon") is not None
