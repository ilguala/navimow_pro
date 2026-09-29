"""The map's stream to the pop-up is never silent long enough to be cut off.

Home Assistant sends a new picture only when it changes. A docked mower's map
may not change for minutes, and a reverse proxy closes a silent stream after
about a minute -- the pop-up then went black (#15).
"""
import asyncio

from custom_components.navimow_pro import camera as camera_mod
from custom_components.navimow_pro.camera import _KEEPALIVE_S, NavimowMapCamera

STILL = b"<svg/>"


def frames_over(monkeypatch, seconds, step=2.0):
    """What the stream's picture callback returns, polled every ``step`` s."""
    clock = {"t": 0.0}
    monkeypatch.setattr(camera_mod.time, "monotonic", lambda: clock["t"])
    seen = []

    async def fake_stream(request, image_cb, content_type, interval):
        while clock["t"] <= seconds:
            seen.append(await image_cb())
            clock["t"] += step

    monkeypatch.setattr(camera_mod, "async_get_still_stream", fake_stream)
    if not hasattr(NavimowMapCamera, "frame_interval"):  # Home Assistant's placeholder
        monkeypatch.setattr(NavimowMapCamera, "frame_interval", 2.0, raising=False)
    camera = object.__new__(NavimowMapCamera)
    camera.content_type = "image/svg+xml"

    async def still(width=None, height=None):
        return STILL

    camera.async_camera_image = still
    asyncio.run(camera.handle_async_mjpeg_stream(None))
    return seen


def test_an_unchanged_map_still_goes_out_again_within_a_minute(monkeypatch):
    seen = frames_over(monkeypatch, 70)
    distinct = [f for i, f in enumerate(seen) if i == 0 or f != seen[i - 1]]
    assert len(distinct) >= 70 // _KEEPALIVE_S
    assert all(f.startswith(STILL) for f in seen)


def test_the_picture_itself_is_unchanged(monkeypatch):
    for frame in frames_over(monkeypatch, 20):
        assert frame[len(STILL):].startswith(b"<!-- ") and frame.endswith(b" -->")


def test_the_map_is_not_redrawn_twice_a_second():
    assert NavimowMapCamera._attr_frame_interval >= 2
