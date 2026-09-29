"""Decoding the map and the fields drawn on it, from the cloud's raw payloads.

The geometry follows the layout the decoder documents: sub_maps with BOUNDARY
and CHARGING_PILE elements, obstacles, vision_off_areas and tunnels, points as
[x, y, attr, seq]. The values are made up.
"""
import base64
import json

import pytest
import zstandard

from custom_components.navimow_pro.const import (
    decode_partition_id_list,
    encode_partition_ids,
)
from custom_components.navimow_pro.coordinator import (
    _extract_geometry,
    _parse_coverage,
    _parse_map_detail,
    _parse_map_detail_plain,
    _parse_work_position,
)

GEOM = {
    "area": 412.5,
    "sub_maps": [
        {
            "id": 1,
            "name": "Front lawn",
            "area": 250.0,
            "elements": [
                {
                    "type": "BOUNDARY",
                    "points": [[0, 0, 1, 0], [20, 0, 2, 1], [20, 10, 1, 2], [0, 10, 1, 3]],
                },
                {"type": "CHARGING_PILE", "position": [1.5, 2.5], "direction": 1.57},
            ],
        },
        {
            "id": 5,
            "area": 162.5,
            "elements": [{"type": "BOUNDARY", "points": [[30, 0], [40, 0], [40, 10]]}],
        },
        {"elements": [{"type": "UNKNOWN_THING"}]},  # no id, no boundary: dropped
    ],
    "obstacles": [{"points": [[2, 2], [4, 2], [4, 4]]}, {"points": []}],
    "vision_off_areas": [{"points": [[10, 1], [12, 1], [12, 3]]}],
    "tunnels": [
        {"type": 1, "tunnel_type": "TUNNEL", "connection": [1, 5], "points": [[20, 5, 0], [30, 5, 0]]},
        {"type": 1, "tunnel_type": "TUNNEL", "connection": [1, 5], "points": [[25, 5]]},
    ],
}


def _compressed(geom, *, content_size=True):
    """What map-detail-compress returns: base64(zstd(JSON with map_detail as a string))."""
    outer = json.dumps({"map_detail": json.dumps(geom)}).encode()
    frame = zstandard.ZstdCompressor(write_content_size=content_size).compress(outer)
    return base64.b64encode(frame).decode()


# ---------------------------------------------------------------- geometry


def test_zones():
    zones = _extract_geometry(GEOM)["zones"]
    assert [z["id"] for z in zones] == [1, 5]
    front, back = zones
    assert front["name"] == "Front lawn"
    assert front["area"] == 250.0
    assert front["polygon"] == [[0, 0], [20, 0], [20, 10], [0, 10]]
    assert front["boundary_flags"] == [1, 2, 1, 1]
    # A zone without a name is named after its id; points without an
    # attribute carry None, which the perimeter draws dashed.
    assert back["name"] == "Zone 5"
    assert back["boundary_flags"] == [None, None, None]


def test_station_obstacles_vision_off():
    geom = _extract_geometry(GEOM)
    assert geom["station"] == {"x": 1.5, "y": 2.5, "direction": 1.57}
    assert geom["obstacles"] == [[[2, 2], [4, 2], [4, 4]]]  # empty one dropped
    assert geom["vision_off"] == [[[10, 1], [12, 1], [12, 3]]]
    assert geom["area"] == 412.5


def test_tunnels_need_two_points():
    tunnels = _extract_geometry(GEOM)["tunnels"]
    assert tunnels == [
        {"points": [[20, 5], [30, 5]], "zones": [1, 5], "kind": "1/TUNNEL"}
    ]
    # ...but every tunnel is counted, drawable or not.
    assert _extract_geometry(GEOM)["inventory"]["tunnel_kinds"] == {"1/TUNNEL": 2}


def test_inventory_carries_no_values():
    """The inventory goes into diagnostics: names and counts, never data."""
    geom = json.loads(json.dumps(GEOM))
    geom["sub_maps"][0]["name"] = "Private garden name"
    geom["tunnels"][0]["tunnel_type"] = "free text with spaces"
    inventory = json.dumps(_extract_geometry(geom)["inventory"])
    assert "Private garden name" not in inventory
    assert "free text" not in inventory
    for value in ("412.5", "1.57", "2.5"):
        assert value not in inventory


def test_malformed_parts_are_skipped():
    geom = {
        "sub_maps": [
            "not a dict",
            {"id": 2, "elements": ["nope", {"type": "BOUNDARY", "points": [[1, "x"], [2, 2], "bad"]}]},
        ],
        "obstacles": ["nope"],
        "tunnels": [None],
    }
    result = _extract_geometry(geom)
    assert result["zones"][0]["polygon"] == [[2.0, 2.0]]
    assert result["obstacles"] == []
    assert result["tunnels"] == []
    assert result["station"] is None


# ---------------------------------------------------------------- payloads


@pytest.mark.parametrize("content_size", [True, False], ids=["sized", "headerless"])
def test_compressed_map_detail(content_size):
    # The cloud sends frames WITHOUT the content size; a one-shot decompress
    # cannot handle those, which is what _zstd_decompress streams around.
    geom = _parse_map_detail(_compressed(GEOM, content_size=content_size))
    assert geom == _extract_geometry(GEOM)


@pytest.mark.parametrize(
    "blob",
    [None, "", 42, "not base64 !!", base64.b64encode(b"not zstd").decode()],
)
def test_compressed_map_detail_rejects_garbage(blob):
    assert _parse_map_detail(blob) is None


def test_compressed_map_detail_rejects_wrong_json():
    frame = zstandard.ZstdCompressor().compress(b'{"map_detail": "[1, 2]"}')
    assert _parse_map_detail(base64.b64encode(frame).decode()) is None


def test_plain_map_detail():
    expected = _extract_geometry(GEOM)
    assert _parse_map_detail_plain({"map_detail": json.dumps(GEOM)}) == expected
    assert _parse_map_detail_plain({"map_detail": GEOM}) == expected
    assert _parse_map_detail_plain(json.dumps({"map_detail": json.dumps(GEOM)})) == expected


@pytest.mark.parametrize(
    "data",
    [None, "{broken", {"map_detail": "{broken"}, {"map_detail": "{}"}, {"map_detail": "  "}, []],
)
def test_plain_map_detail_rejects_garbage(data):
    assert _parse_map_detail_plain(data) is None


# ---------------------------------------------------------------- fields drawn on the map


def test_partition_ids():
    assert decode_partition_id_list("00010005") == [1, 5]
    assert decode_partition_id_list("0000000a") == [10]  # 0 is padding
    assert decode_partition_id_list("") == []
    assert decode_partition_id_list("zz") == []
    # Written little-endian, read big-endian: the two are not inverses.
    assert encode_partition_ids([1, 5]) == "01000500"


def test_work_position():
    # The app's own log: currentMowBoundary=5, currentMowProgress=396.
    blob = "".join(f"{w:08x}" for w in (8, 6, 0, 5, 396))
    assert _parse_work_position(blob) == (5, 3.96)


@pytest.mark.parametrize(
    "words",
    [(8, 6, 0, 0, 396), (8, 6, 0, 0xFFFFFFFF, 396)],
    ids=["zero", "all-ones"],
)
def test_work_position_not_in_a_zone(words):
    assert _parse_work_position("".join(f"{w:08x}" for w in words)) == (None, None)


def test_work_position_unknown_progress():
    blob = "".join(f"{w:08x}" for w in (8, 6, 0, 5, 0xFFFFFFFF))
    assert _parse_work_position(blob) == (5, None)


@pytest.mark.parametrize("raw", [None, "", "0011", "x" * 40])
def test_work_position_garbage(raw):
    assert _parse_work_position(raw) == (None, None)


def test_coverage():
    raw = [
        {"partitionId": 1, "area": 100, "finishedArea": 80, "partitionPercentage": 80},
        {"partitionId": 5, "area": 50, "finishedArea": 10, "partitionPercentage": 20},
    ]
    cov = _parse_coverage(raw, {1: "Front lawn"})
    assert cov["overall_pct"] == 60  # by area, not the mean of 80 and 20
    assert cov["total_area"] == 150
    assert cov["finished_area"] == 90
    assert [(z["id"], z["name"], z["pct"]) for z in cov["zones"]] == [
        (1, "Front lawn", 80),
        (5, "Zone 5", 20),
    ]


@pytest.mark.parametrize("raw", [None, [], "x", ["not a dict"]])
def test_coverage_nothing_to_report(raw):
    assert _parse_coverage(raw, {}) is None


def test_coverage_without_area():
    cov = _parse_coverage([{"partitionId": 1, "partitionPercentage": 40}], {})
    assert cov["overall_pct"] is None
    assert cov["zones"][0]["pct"] == 40
