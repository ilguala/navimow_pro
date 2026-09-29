"""The SVG map: where zone labels go, and what the rendered picture holds.

Label placement is where the map has regressed before (#12: on a nine-zone map
the pills were wider than the strips they named and sat on top of each other
and of the mower), so it is pinned both on its own and through a full render.
"""
import itertools
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro.camera import (
    _LABEL_MAX,
    _LABEL_MIN,
    _LEGEND_W,
    _VIEW,
    NavimowMapCamera,
    _box_inside,
    _boxes_overlap,
    _label_box,
    _place_labels,
)

SVG = "{http://www.w3.org/2000/svg}"
PILL_FILL = "#eceff1"


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def label(poly, *texts):
    """A _place_labels entry for a screen-space polygon, at its box centre."""
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, list(texts), w, w * h, poly)


def render(data):
    camera = object.__new__(NavimowMapCamera)
    camera.coordinator = SimpleNamespace(data=data)
    return camera, camera.camera_image().decode()


def pills(svg):
    """The (x0, y0, x1, y1) boxes of the zone label pills in a rendered SVG."""
    root = ET.fromstring(svg)
    return [
        (
            float(r.get("x")),
            float(r.get("y")),
            float(r.get("x")) + float(r.get("width")),
            float(r.get("y")) + float(r.get("height")),
        )
        for r in root.iter(f"{SVG}rect")
        if r.get("fill") == PILL_FILL
    ]


def zone(zid, name, x0, y0, x1, y1):
    return {"id": zid, "name": name, "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


# ---------------------------------------------------------------- placement


def test_label_at_the_centre_of_a_roomy_zone():
    poly = square(100, 100, 400, 300)
    [(x, y, text, size)] = _place_labels([label(poly, "Front lawn · 83%", "83%")], [], _VIEW)
    assert (x, y) == (250, 200)
    assert text == "Front lawn · 83%"
    assert size == _LABEL_MAX


def test_long_label_falls_back_to_the_percentage():
    poly = square(100, 100, 150, 300)  # 50 px wide: room for "83%" only
    [(_, _, text, size)] = _place_labels([label(poly, "Front lawn · 83%", "83%")], [], _VIEW)
    assert text == "83%"
    assert _LABEL_MIN <= size <= _LABEL_MAX


def test_no_label_where_nothing_fits():
    poly = square(100, 100, 115, 110)
    assert _place_labels([label(poly, "Front lawn · 83%", "83%")], [], _VIEW) == []


def test_label_moves_off_a_reserved_box():
    poly = square(100, 100, 400, 300)
    mower = (230, 185, 270, 215)  # right on the centroid
    [(x, y, text, size)] = _place_labels([label(poly, "Front lawn")], [mower], _VIEW)
    box = _label_box(x, y, text, size)
    assert not _boxes_overlap(box, mower)
    assert _box_inside(box, poly)


def test_labels_stay_inside_their_own_zone_and_apart():
    # Six narrow side-by-side strips, the #12 layout in screen space.
    strips = [square(100 + i * 60, 100, 150 + i * 60, 500) for i in range(6)]
    placed = _place_labels(
        [label(p, f"Zone {i} · 50%", "50%") for i, p in enumerate(strips)], [], _VIEW
    )
    assert len(placed) == 6
    boxes = [_label_box(x, y, text, size) for x, y, text, size in placed]
    for (x, y, *_), box in zip(placed, boxes):
        own = next(p for p in strips if p[0][0] <= x <= p[1][0])
        assert _box_inside(box, own)
    for a, b in itertools.combinations(boxes, 2):
        assert not _boxes_overlap(a, b)


# ---------------------------------------------------------------- the rendered map


def many_zone_map():
    """Nine long, thin zones side by side, a dock and the mower among them."""
    zones = [zone(i + 1, f"Zone {i + 1}", i * 2.2, 0, i * 2.2 + 2, 30) for i in range(9)]
    return {
        "state": "Mowing",
        "battery": 80,
        "map": {"zones": zones, "station": {"x": 1, "y": 1}},
        "position": {"x": 9, "y": 15, "heading": 0},
        "coverage": {
            "overall_pct": 50,
            "zones": [{"id": z["id"], "pct": 50} for z in zones],
        },
        "trail": [[1, 1], [1, 10], [3, 10]],
    }


def test_render_is_valid_svg():
    _, svg = render(many_zone_map())
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG}svg"
    assert root.get("viewBox") == f"0 0 {_VIEW} {_VIEW}"


def test_many_zones_labels_do_not_collide():
    _, svg = render(many_zone_map())
    boxes = pills(svg)
    assert boxes, "expected at least some zone labels"
    for a, b in itertools.combinations(boxes, 2):
        assert not _boxes_overlap(a, b, gap=0)
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 and x1 <= _VIEW and 0 <= y0 and y1 <= _VIEW
        # Clear of the legend (top left) and the status line (bottom).
        assert not _boxes_overlap((x0, y0, x1, y1), (8, 8, 8 + _LEGEND_W, 96), gap=0)
        assert y1 <= _VIEW - 30


def test_zone_names_are_escaped():
    data = {"map": {"zones": [zone(1, '<b>&"x"', 0, 0, 30, 30)]}, "state": "<i>"}
    _, svg = render(data)
    ET.fromstring(svg)  # would fail on raw markup
    assert "<b>" not in svg and "<i>" not in svg


def test_a_stray_position_does_not_shrink_the_lawn():
    data = many_zone_map()
    _, before = render(data)
    data["position"] = {"x": 5000, "y": -5000, "heading": 0}
    _, after = render(data)
    polygons = lambda svg: [  # noqa: E731
        p.get("points") for p in ET.fromstring(svg).iter(f"{SVG}polygon")
    ]
    assert polygons(before) == polygons(after)


def test_trail_is_clipped_to_the_zones():
    _, svg = render(many_zone_map())
    assert 'clip-path="url(#zones)"' in svg


def test_trail_without_zones_is_drawn_unclipped():
    data = {"trail": [[0, 0], [1, 1], [2, 1]], "position": {"x": 2, "y": 1}}
    _, svg = render(data)
    assert "<polyline" in svg
    assert "clip-path" not in svg


def test_legend_lists_only_what_is_on_the_map():
    _, bare = render({"map": {"zones": [zone(1, "A", 0, 0, 10, 10)]}})
    assert "Mower" in bare
    assert "Dock" not in bare and "Mowed" not in bare and "Off-limit" not in bare
    _, full = render(
        {
            "map": {
                "zones": [zone(1, "A", 0, 0, 10, 10)],
                "station": {"x": 1, "y": 1},
                "obstacles": [[[2, 2], [3, 2], [3, 3]]],
                "vision_off": [[[5, 5], [6, 5], [6, 6]]],
            },
            "trail": [[1, 1], [2, 2]],
        }
    )
    for name in ("Dock", "Mowed", "Off-limit area", "VisionFence off"):
        assert name in full


@pytest.mark.parametrize(
    "data, text",
    [({}, "no map data"), ({"state": "Docked"}, "Docked")],
)
def test_placeholder_without_anything_to_draw(data, text):
    _, svg = render(data)
    ET.fromstring(svg)
    assert text in svg
    assert "<polygon" not in svg


def test_broken_geometry_renders_a_placeholder_not_an_exception():
    data = {"map": {"zones": [{"id": 1, "polygon": [[0, 0], [1, "x"], [2, 2]]}]}}
    _, svg = render(data)
    assert "map unavailable" in svg
