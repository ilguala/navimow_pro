"""Camera platform for Navimow (Private).

Renders the real lawn as a lightweight SVG map from the decoded map geometry the
coordinator caches (zones/obstacles/no-mow areas + dock) plus the mower's live
posture from get-location. The HA frontend renders the SVG directly.

The visual style deliberately mirrors the official Navimow app: soft pale-green
zone fills with a thin dashed green perimeter, rounded "pill" zone labels, and
the mowed area as a single flat translucent green layer (drawn with group
opacity so overlapping passes never compound into darker patches).

It is fully defensive: with no geometry it falls back to a position-only dot, and
with nothing at all to a status placeholder -- a camera must never crash the
platform.
"""
from __future__ import annotations

import html
import logging
import math

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SWATH_WIDTH_M, TRAIL_BREAK_M
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity

_LOGGER = logging.getLogger(__name__)

# Square viewBox; world geometry is letterboxed into it preserving aspect ratio.
_VIEW = 600

# App-like palette: one soft green for every zone (the app does not colour zones
# differently -- they are distinguished by their pill labels), a slightly richer
# green for the dashed perimeter and the mowed layer.
_ZONE_FILL = "#81c784"      # pale grass green
_ZONE_STROKE = "#43a047"    # perimeter / border green

# The perimeter is drawn segment-by-segment from each boundary point's attribute
# (map-detail 3rd element): the common "virtual" boundary renders DASHED (most of
# the perimeter), the rarer ride-on/straddle edge -- a physical edge the mower
# rides on, e.g. the top edge of Roberto's Zone 2 -- renders SOLID. This is the
# attribute value that renders SOLID; flip to 1 if the live app shows it inverted.
_BORDER_SOLID_ATTR = 2

_TRAIL_COLOR = "#43a047"    # mowed layer (opaque, flattened via group opacity)
_TRAIL_OPACITY = 0.40       # applied to the WHOLE trail group -> no compounding
_MOWER_ORANGE = "#ff6d00"
_OBSTACLE_FILL = "#616161"
_NOMOW_FILL = "#bdbdbd"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NavimowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NavimowMapCamera(coordinator)])


class NavimowMapCamera(NavimowEntity, Camera):
    """An SVG map of the lawn: zones, obstacles, no-mow areas, dock, mower."""

    _attr_translation_key = "map"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        NavimowEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        # Set after Camera.__init__ so it is not shadowed by the base default.
        self.content_type = "image/svg+xml"

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return self._render().encode("utf-8")
        except Exception:  # noqa: BLE001 - a camera must never crash the platform
            _LOGGER.debug("Failed to render Navimow map", exc_info=True)
            return self._placeholder("map unavailable").encode("utf-8")

    # ------------------------------------------------------------------ render
    def _render(self) -> str:
        data = self.data
        mp = data.get("map") or {}
        zones = mp.get("zones") or []
        obstacles = mp.get("obstacles") or []
        vision_off = mp.get("vision_off") or []
        station = mp.get("station") or None

        pos = data.get("position") or {}
        px, py = pos.get("x"), pos.get("y")
        heading = pos.get("heading")

        # Per-zone coverage % (from get-path-info-time) and the reconstructed
        # mowed trail ([[x,y],...] accumulated while cutting).
        cov = data.get("coverage") or {}
        cov_by_id = {
            z.get("id"): z.get("pct")
            for z in cov.get("zones") or []
            if z.get("id") is not None
        }
        trail = data.get("trail") or []

        # Bounding box: derive it from the STABLE geometry (zones/obstacles/
        # no-mow/dock) first, then include only the dynamic points (mower +
        # trail) that fall within a margin of it. This stops a single bogus
        # posture sample from permanently shrinking the real lawn into a corner.
        stable: list[list[float]] = []
        for z in zones:
            stable.extend(z.get("polygon") or [])
        for ob in obstacles:
            stable.extend(ob)
        for vo in vision_off:
            stable.extend(vo)
        if station and station.get("x") is not None and station.get("y") is not None:
            stable.append([station["x"], station["y"]])

        dynamic: list[list[float]] = list(trail)
        if px is not None and py is not None:
            dynamic.append([px, py])

        if stable:
            bxs = [p[0] for p in stable]
            bys = [p[1] for p in stable]
            bxmin, bxmax, bymin, bymax = min(bxs), max(bxs), min(bys), max(bys)
            margin = max(bxmax - bxmin, bymax - bymin) * 0.15 + 1.0
            pts = list(stable)
            for p in dynamic:
                if bxmin - margin <= p[0] <= bxmax + margin and bymin - margin <= p[1] <= bymax + margin:
                    pts.append(p)
        else:
            pts = dynamic  # no map geometry yet -> fall back to whatever we have

        if not pts:
            return self._placeholder(data.get("state") or "no map data")

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 0.1)
        span_y = max(max_y - min_y, 0.1)
        # ~5% padding on each side.
        pad_x, pad_y = span_x * 0.05, span_y * 0.05
        min_x, max_x = min_x - pad_x, max_x + pad_x
        min_y, max_y = min_y - pad_y, max_y + pad_y
        span_x, span_y = max_x - min_x, max_y - min_y

        scale = min(_VIEW / span_x, _VIEW / span_y)
        off_x = (_VIEW - span_x * scale) / 2.0
        off_y = (_VIEW - span_y * scale) / 2.0

        def sx(x: float) -> float:
            return off_x + (x - min_x) * scale

        def sy(y: float) -> float:
            # world y grows up, SVG grows down.
            return off_y + (max_y - y) * scale

        # Transparent field -> adapts to the HA card's light/dark theme. No frame:
        # the app map is borderless.
        parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_VIEW}" height="{_VIEW}" '
            f'viewBox="0 0 {_VIEW} {_VIEW}">'
        ]

        # Zones: soft uniform green fill (no border) + a per-segment perimeter
        # (dashed for virtual-boundary edges, solid for ride-on/straddle edges),
        # mirroring the app. Labels are collected and drawn LAST so they sit above
        # the mowed layer.
        zone_labels: list[tuple[float, float, str]] = []
        for z in zones:
            poly = z.get("polygon") or []
            if len(poly) < 3:
                continue
            pts_str = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in poly)
            parts.append(
                f'<polygon points="{pts_str}" fill="{_ZONE_FILL}" fill-opacity="0.22" '
                f'stroke="none"/>'
            )
            parts.append(self._perimeter(poly, z.get("boundary_flags") or [], sx, sy))
            cx = sum(sx(x) for x, _ in poly) / len(poly)
            cy = sum(sy(y) for _, y in poly) / len(poly)
            zname = str(z.get("name") or "")
            zpct = cov_by_id.get(z.get("id"))
            zlabel = f"{zname} · {zpct}%" if zpct is not None else zname
            if zlabel:
                zone_labels.append((cx, cy, zlabel))

        # Obstacles (dark gray fill).
        for ob in obstacles:
            if len(ob) < 3:
                continue
            pts_str = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in ob)
            parts.append(
                f'<polygon points="{pts_str}" fill="{_OBSTACLE_FILL}" fill-opacity="0.70" '
                f'stroke="#424242" stroke-width="1.5" stroke-linejoin="round"/>'
            )

        # Vision-off / no-mow areas (light gray, dashed).
        for vo in vision_off:
            if len(vo) < 3:
                continue
            pts_str = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in vo)
            parts.append(
                f'<polygon points="{pts_str}" fill="{_NOMOW_FILL}" fill-opacity="0.30" '
                f'stroke="#9e9e9e" stroke-width="1.5" stroke-dasharray="6 4" '
                f'stroke-linejoin="round"/>'
            )

        # Mowed trail: a single flat translucent green layer. Each pass is drawn
        # OPAQUE inside one <g opacity=...> group -> the group is flattened before
        # the opacity is applied, so overlapping passes never compound into darker
        # patches (this is what made the old per-line stroke-opacity look ugly).
        # Points are lightly smoothed per segment to remove GPS zig-zag.
        if len(trail) >= 2:
            stroke_w = min(max(SWATH_WIDTH_M * scale, 6.0), 26.0)
            break_sq = TRAIL_BREAK_M * TRAIL_BREAK_M
            segments: list[list[list[float]]] = [[]]
            prev: list[float] | None = None
            for x, y in trail:
                if prev is not None:
                    dx, dy = x - prev[0], y - prev[1]
                    if dx * dx + dy * dy > break_sq:
                        segments.append([])
                segments[-1].append([x, y])
                prev = [x, y]

            trail_parts: list[str] = []
            for seg in segments:
                seg = self._smooth(seg)
                if len(seg) >= 2:
                    pts_str = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in seg)
                    trail_parts.append(
                        f'<polyline points="{pts_str}" fill="none" '
                        f'stroke="{_TRAIL_COLOR}" stroke-width="{stroke_w:.1f}" '
                        f'stroke-linecap="round" stroke-linejoin="round"/>'
                    )
            if trail_parts:
                parts.append(
                    f'<g opacity="{_TRAIL_OPACITY}">{"".join(trail_parts)}</g>'
                )

        # Dock / charging station (small house marker).
        if station and station.get("x") is not None and station.get("y") is not None:
            parts.append(self._station(sx(station["x"]), sy(station["y"])))

        # Mower (robot icon oriented to its heading).
        if px is not None and py is not None:
            parts.append(self._mower(sx(px), sy(py), heading))

        # Zone labels (rounded pills) -- on top of the mowed layer so they read.
        for cx, cy, text in zone_labels:
            parts.append(self._pill_label(cx, cy, text, size=15))

        # Legend + status line.
        parts.append(
            self._legend(
                bool(obstacles), bool(vision_off), station is not None, len(trail) >= 2
            )
        )
        cov_pct = cov.get("overall_pct")
        cov_txt = f" · {cov_pct}% mowed" if cov_pct is not None else ""
        label = f"{data.get('state', '')} · {data.get('battery', '?')}%{cov_txt}"
        parts.append(self._label(12, _VIEW - 12, label, size=14, anchor="start"))

        parts.append("</svg>")
        return "".join(parts)

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _smooth(points: list[list[float]], window: int = 3) -> list[list[float]]:
        """Light moving-average smoothing of a point run (endpoints preserved).

        Removes the GPS zig-zag from the reconstructed trail so passes read as
        clean lines rather than a scribble. A tiny window keeps real turns.
        """
        n = len(points)
        if n < 3:
            return points
        half = window // 2
        out: list[list[float]] = []
        for i in range(n):
            lo = max(0, i - half)
            hi = min(n, i + half + 1)
            k = hi - lo
            sxx = sum(p[0] for p in points[lo:hi]) / k
            syy = sum(p[1] for p in points[lo:hi]) / k
            out.append([sxx, syy])
        out[0] = list(points[0])
        out[-1] = list(points[-1])
        return out

    @staticmethod
    def _perimeter(poly: list[list[float]], flags: list, sx, sy) -> str:
        """Draw the zone perimeter edge-by-edge, dashed vs solid per boundary flag.

        Each edge ``poly[i] -> poly[i+1]`` takes the attribute of its START point
        (``flags[i]``): ``_BORDER_SOLID_ATTR`` renders SOLID, everything else
        (including missing/short flags) renders DASHED -- so a map with no
        attribute data degrades to the common all-dashed look. Consecutive edges
        of the same style are merged into one polyline (continuous dashes, fewer
        nodes). ``sx``/``sy`` are the world->screen projectors.
        """
        n = len(poly)
        if n < 2:
            return ""
        scr = [(sx(x), sy(y)) for x, y in poly]
        # Edge index pairs; close the loop unless the polygon already repeats p0.
        pairs = [(i, i + 1) for i in range(n - 1)]
        if poly[0] != poly[-1]:
            pairs.append((n - 1, 0))

        def is_solid(i: int) -> bool:
            return i < len(flags) and flags[i] == _BORDER_SOLID_ATTR

        # Group consecutive same-style edges into runs (shared junction vertex).
        runs: list[tuple[bool, list[tuple[float, float]]]] = []
        for a, b in pairs:
            st = is_solid(a)
            if runs and runs[-1][0] == st:
                runs[-1][1].append(scr[b])
            else:
                runs.append((st, [scr[a], scr[b]]))

        # If the loop closes on a same-style edge, fold the last run into the
        # first so a dashed stretch crossing vertex 0 stays one continuous run
        # (no dash-phase reset / seam at that vertex).
        if (
            len(runs) > 1
            and runs[0][0] == runs[-1][0]
            and runs[-1][1][-1] == runs[0][1][0]
        ):
            last = runs.pop()
            runs[0] = (runs[0][0], last[1] + runs[0][1][1:])

        out: list[str] = []
        for solid, pts in runs:
            pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            dash = "" if solid else ' stroke-dasharray="7 4"'
            out.append(
                f'<polyline points="{pts_str}" fill="none" stroke="{_ZONE_STROKE}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )
        return "".join(out)

    @staticmethod
    def _label(x: float, y: float, text: str, *, size: int, anchor: str) -> str:
        """A text label with a white halo so it reads on any theme/fill."""
        safe = html.escape(str(text))
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'font-family="sans-serif" font-size="{size}" font-weight="600" '
            f'paint-order="stroke" stroke="#ffffff" stroke-width="3" '
            f'stroke-linejoin="round" fill="#212121">{safe}</text>'
        )

    @staticmethod
    def _pill_label(cx: float, cy: float, text: str, *, size: int) -> str:
        """A rounded light-gray "pill" label with dark text (app style).

        Always a light pill with dark text, so it stays readable on both light
        and dark HA themes and over any fill.
        """
        safe = html.escape(str(text))
        w = len(text) * size * 0.58 + 18.0
        h = size + 12.0
        x = cx - w / 2.0
        y = cy - h / 2.0
        return (
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{h / 2.0:.1f}" fill="#eceff1" fill-opacity="0.92" '
            f'stroke="#b0bec5" stroke-width="1"/>'
            f'<text x="{cx:.1f}" y="{cy + size * 0.35:.1f}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="{size}" font-weight="600" '
            f'fill="#37474f">{safe}</text>'
        )

    @staticmethod
    def _mower(cx: float, cy: float, heading: float | None) -> str:
        """A little robot-mower icon, rotated to face its heading.

        World heading is CCW from +x with y-up; SVG y is down, so the SVG
        rotation is the negated degrees.
        """
        deg = 0.0 if heading is None else -math.degrees(heading)
        return (
            f'<g transform="translate({cx:.1f},{cy:.1f}) rotate({deg:.1f})">'
            # body (dark, rounded) with a white outline so it pops on any fill
            f'<rect x="-15" y="-12" width="30" height="24" rx="8" fill="#263238" '
            f'stroke="#ffffff" stroke-width="2.5"/>'
            # two head "LEDs"
            f'<circle cx="-5" cy="-5.5" r="2.4" fill="#eceff1"/>'
            f'<circle cx="-5" cy="5.5" r="2.4" fill="#eceff1"/>'
            # orange front sensor = points in the heading direction
            f'<circle cx="8.5" cy="0" r="5.5" fill="{_MOWER_ORANGE}" stroke="#ffffff" stroke-width="1.5"/>'
            f"</g>"
        )

    @staticmethod
    def _station(cx: float, cy: float) -> str:
        """A charging-dock icon: a dark base plate with a green charge bolt."""
        bolt = (
            f"M {cx + 1:.1f},{cy - 6:.1f} L {cx - 4:.1f},{cy + 1:.1f} "
            f"L {cx:.1f},{cy + 1:.1f} L {cx - 1:.1f},{cy + 6:.1f} "
            f"L {cx + 4:.1f},{cy - 1:.1f} L {cx:.1f},{cy - 1:.1f} Z"
        )
        return (
            f'<rect x="{cx - 11:.1f}" y="{cy - 9:.1f}" width="22" height="18" rx="4" '
            f'fill="#37474f" stroke="#ffffff" stroke-width="2"/>'
            f'<path d="{bolt}" fill="#69f0ae" stroke="#1b5e20" stroke-width="0.6"/>'
        )

    def _legend(
        self, has_obstacle: bool, has_no_mow: bool, has_dock: bool, has_trail: bool
    ) -> str:
        """Compact, discreet legend of marker meanings (top-left overlay)."""
        rows: list[tuple[str, str]] = [(_MOWER_ORANGE, "Mower")]
        if has_trail:
            rows.append((_TRAIL_COLOR, "Mowed"))
        if has_dock:
            rows.append(("#455a64", "Dock"))
        if has_obstacle:
            rows.append((_OBSTACLE_FILL, "Obstacle"))
        if has_no_mow:
            rows.append((_NOMOW_FILL, "No-mow"))
        x0, y0, dy = 14, 22, 20
        h = dy * len(rows) + 8
        parts = [
            f'<rect x="8" y="8" width="120" height="{h}" rx="6" '
            f'fill="#000000" fill-opacity="0.05" stroke="#9e9e9e" stroke-opacity="0.25"/>'
        ]
        for i, (color, name) in enumerate(rows):
            cy = y0 + i * dy
            parts.append(
                f'<rect x="{x0}" y="{cy - 8:.0f}" width="12" height="12" rx="2" '
                f'fill="{color}" stroke="#ffffff" stroke-width="1"/>'
            )
            parts.append(self._label(x0 + 18, cy + 2, name, size=13, anchor="start"))
        return "".join(parts)

    def _placeholder(self, message: str) -> str:
        msg = html.escape(str(message))
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_VIEW}" height="{_VIEW}" '
            f'viewBox="0 0 {_VIEW} {_VIEW}">'
            f'<rect x="1" y="1" width="{_VIEW - 2}" height="{_VIEW - 2}" fill="none" '
            f'stroke="#9e9e9e" stroke-opacity="0.35" stroke-width="1" rx="8"/>'
            f'<text x="{_VIEW // 2}" y="{_VIEW // 2}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="18" paint-order="stroke" '
            f'stroke="#ffffff" stroke-width="3" fill="#777">{msg}</text>'
            f"</svg>"
        )
