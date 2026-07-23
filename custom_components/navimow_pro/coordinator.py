"""DataUpdateCoordinator for Navimow (Private).

Polls the private cloud on a fixed interval and produces a single, defensively
parsed snapshot dict consumed by every entity. All blocking work (crypto + IO)
runs in one executor job per cycle so the event loop is never blocked.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import NavimowAuthError, NavimowCloudClient, NavimowError, Tokens
from .const import (
    ACTIVE_STATES,
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_LANGUAGE,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_UID,
    CONF_VEHICLE_SN,
    CONF_VEHICLE_TYPE,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL,
    DOCKED_STATES,
    DOMAIN,
    FAST_SCAN_INTERVAL,
    MOW_SCAN_INTERVAL,
    OPT_ZONES,
    SLOW_REFRESH_EVERY,
    STATE_MOWING,
    TRAIL_MAX_POINTS,
    TRAIL_MIN_STEP_M,
    VEHICLE_STATE_LABELS,
    VEHICLE_STATE_TO_ACTIVITY,
    decode_partition_id_list,
    encode_partition_ids,
    partition_setup_mask,
)

_LOGGER = logging.getLogger(__name__)

# Slow-changing endpoints refreshed only every SLOW_REFRESH_EVERY cycles.
_SLOW_KEYS = ("set_list", "maintenance", "today_plan", "map_list")

# Persist the reconstructed mowed trail so it survives HA restarts/updates.
# The store is written at most once per _TRAIL_SAVE_DELAY seconds and only when
# the trail actually changed (keeps SD-card writes low on HAOS).
_TRAIL_STORE_VERSION = 1
_TRAIL_SAVE_DELAY = 30


def trail_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Per-entry Store that persists the reconstructed mowed trail.

    Shared with ``async_remove_entry`` in ``__init__`` so the key/version stay in
    sync (the store file must be deleted when the integration is removed).
    """
    return Store(hass, _TRAIL_STORE_VERSION, f"{DOMAIN}_trail_{entry_id}")


def _find(obj: Any, *keys: str) -> Any:
    """Recursively return the first value found for any of ``keys`` (DFS)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                return v
        for v in obj.values():
            found = _find(v, *keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, *keys)
            if found is not None:
                return found
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """Interpret the mower's many truthy encodings ('01', 1, '1', True)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in ("1", "01", "true", "on", "yes"):
        return True
    if s in ("0", "00", "false", "off", "no", ""):
        return False
    return None


def _parse_zone_options(raw: str | None) -> list[dict]:
    """Parse the 'id:name,id:name' options string into zone dicts."""
    zones: list[dict] = []
    if not raw:
        return zones
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            rid_s, name = chunk.split(":", 1)
        else:
            rid_s, name = chunk, f"Zone {chunk}"
        rid = _as_int(rid_s)
        if rid is None:
            continue
        zones.append({"id": rid, "name": name.strip() or f"Zone {rid}"})
    return zones


# --------------------------------------------------------------------- map
# Weekday labels (plan.day is 1=Mon..7=Sun).
# Navimow weekday numbering is 1=Sun .. 7=Sat (verified live: day 3 = Tue, 6 = Fri).
_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _zstd_decompress(raw: bytes) -> bytes | None:
    """Decompress a ZSTD frame that carries no content-size header.

    The map blob is a raw ZSTD frame without the (optional) decompressed-size in
    its header, so a one-shot ``decompress()`` cannot pre-size its output and
    fails. Both paths below stream the output instead:

    * Python 3.14+ stdlib ``compression.zstd`` (the HA host), and
    * the ``zstandard`` PyPI package via ``stream_reader`` (fallback).

    Returns the decompressed bytes, or ``None`` if no decoder is available /
    the frame is unreadable (the integration then degrades to "no map").
    """
    # 1) Python 3.14+ standard library `compression.zstd` (no external dep).
    try:
        import compression.zstd as _cz  # type: ignore[import-not-found]
    except Exception as err:  # noqa: BLE001 - module not built into this Python.
        _LOGGER.warning("map: stdlib compression.zstd unavailable (%s)", err)
    else:
        try:
            return _cz.decompress(raw)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("map: compression.zstd.decompress failed", exc_info=True)
            try:  # headerless frame -> stream via the decompressor object.
                return _cz.ZstdDecompressor().decompress(raw)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("map: compression.zstd streaming failed", exc_info=True)
    # 2) zstandard PyPI package (fallback; stream_reader handles headerless frames).
    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    except Exception:  # noqa: BLE001 - no usable decoder available.
        _LOGGER.warning("map: no zstd decoder available (map geometry disabled)")
        return None


def _points_xy(points: Any) -> list[list[float]]:
    """Extract [x, y] pairs from a list of [x, y, flag, seq, ...] points."""
    out: list[list[float]] = []
    if not isinstance(points, list):
        return out
    for p in points:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = _as_float(p[0]), _as_float(p[1])
            if x is not None and y is not None:
                out.append([x, y])
    return out


def _boundary_points(points: Any) -> tuple[list[list[float]], list[int | None]]:
    """Split a BOUNDARY point list into an [x, y] polygon and per-point flags.

    Each raw point is ``[x, y, attr, seq, ...]``; ``attr`` (3rd element) is the
    boundary segment attribute that drives the perimeter line style (dashed vs
    solid). Returns ``(polygon, flags)`` with ``flags[i]`` aligned to
    ``polygon[i]`` (``None`` when a point carries no attribute).
    """
    poly: list[list[float]] = []
    flags: list[int | None] = []
    if not isinstance(points, list):
        return poly, flags
    for p in points:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = _as_float(p[0]), _as_float(p[1])
            if x is None or y is None:
                continue
            poly.append([x, y])
            flags.append(_as_int(p[2]) if len(p) >= 3 else None)
    return poly, flags


def _extract_geometry(geom: dict) -> dict:
    """Reduce the raw map_detail object to the shapes the UI needs.

    Zones come from ``sub_maps`` (the BOUNDARY element of each), the dock from a
    CHARGING_PILE element (same world frame as the boundaries).
    """
    zones: list[dict] = []
    station: dict | None = None
    for sm in geom.get("sub_maps") or []:
        if not isinstance(sm, dict):
            continue
        zid = _as_int(sm.get("id"))
        polygon: list[list[float]] = []
        boundary_flags: list[int | None] = []
        for el in sm.get("elements") or []:
            if not isinstance(el, dict):
                continue
            etype = el.get("type")
            if etype == "BOUNDARY" and not polygon:
                polygon, boundary_flags = _boundary_points(el.get("points"))
            elif etype == "CHARGING_PILE" and station is None:
                pos = _points_xy([el.get("position")])
                if pos:
                    station = {
                        "x": pos[0][0],
                        "y": pos[0][1],
                        "direction": _as_float(el.get("direction")),
                    }
        if zid is None and not polygon:
            continue
        zones.append(
            {
                "id": zid,
                "name": str(sm.get("name") or (f"Zone {zid}" if zid is not None else "Zone")),
                "area": _as_float(sm.get("area")),
                "polygon": polygon,
                "boundary_flags": boundary_flags,
            }
        )

    obstacles = [
        pts
        for ob in (geom.get("obstacles") or [])
        if isinstance(ob, dict) and (pts := _points_xy(ob.get("points")))
    ]
    vision_off = [
        pts
        for vo in (geom.get("vision_off_areas") or [])
        if isinstance(vo, dict) and (pts := _points_xy(vo.get("points")))
    ]
    return {
        "area": _as_float(geom.get("area")),
        "zones": zones,
        "obstacles": obstacles,
        "vision_off": vision_off,
        "station": station,
    }


def _parse_map_detail(blob: Any) -> dict | None:
    """base64 -> zstd -> JSON -> map_detail(JSON string) -> reduced geometry.

    Fully defensive: any failure returns ``None`` (the map degrades gracefully).
    ``blob`` is the ``data`` field of map-detail-compress (a base64 string).
    """
    if not blob or not isinstance(blob, str):
        return None
    try:
        raw = base64.b64decode(blob)
    except Exception:  # noqa: BLE001
        return None
    decompressed = _zstd_decompress(raw)
    if not decompressed:
        return None
    try:
        outer = json.loads(decompressed)
        detail = outer.get("map_detail") if isinstance(outer, dict) else None
        geom = json.loads(detail) if isinstance(detail, str) else detail
    except (ValueError, TypeError):
        return None
    if not isinstance(geom, dict):
        return None
    try:
        return _extract_geometry(geom)
    except Exception:  # noqa: BLE001 - never raise into the coordinator.
        _LOGGER.debug("map geometry extraction failed", exc_info=True)
        return None


def _parse_map_detail_plain(data: Any) -> dict | None:
    """Extract geometry from the UNCOMPRESSED /map/index/map-detail response.

    ``data`` is a DeviceMapInfo dict whose ``map_detail`` field is a JSON string
    (the same geometry that the -compress variant zstd-packs). No zstd needed.
    Fully defensive: any failure returns ``None``.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    detail = data.get("map_detail")
    geom: Any = None
    if isinstance(detail, str) and detail.strip():
        try:
            geom = json.loads(detail)
        except (ValueError, TypeError):
            geom = None
    elif isinstance(detail, dict):
        geom = detail
    if not isinstance(geom, dict) or "sub_maps" not in geom:
        return None
    try:
        return _extract_geometry(geom)
    except Exception:  # noqa: BLE001 - never raise into the coordinator.
        _LOGGER.debug("plain map geometry extraction failed", exc_info=True)
        return None


def _slot_hhmm(slot: Any) -> str | None:
    """15-minute slot index from 00:00 -> 'HH:MM' (39 -> '09:45')."""
    s = _as_int(slot)
    if s is None:
        return None
    m = s * 15
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _parse_schedule(set_list: Any, zone_names: dict) -> list[dict]:
    """Normalize the weekly mowing plan into a UI/calendar-friendly structure.

    Source: ``set_list.plan_v2`` (preferred; carries per-period zones) or
    ``plan``. Entry: ``{day:1-7 (1=Mon), open:0/1, period:[{start_time,end_time,
    partition_ids}|[start,end]]}``; start/end are 15-minute slots from 00:00,
    empty ``partition_ids`` means all zones. Fully defensive.
    """
    plan = None
    if isinstance(set_list, dict):
        plan = set_list.get("plan_v2") or set_list.get("plan")
    if not isinstance(plan, list):
        return []
    out: list[dict] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        day = _as_int(entry.get("day"))
        if day is None or not 1 <= day <= 7:
            continue
        periods: list[dict] = []
        for p in entry.get("period") or []:
            if isinstance(p, dict):
                s, e = _as_int(p.get("start_time")), _as_int(p.get("end_time"))
                raw_ids = p.get("partition_ids") or []
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                s, e, raw_ids = _as_int(p[0]), _as_int(p[1]), []
            else:
                continue
            if s is None or e is None:
                continue
            ids = [i for i in (_as_int(x) for x in raw_ids) if i is not None]
            names = [zone_names.get(i, f"Zone {i}") for i in ids] if ids else ["All zones"]
            periods.append(
                {
                    "start_min": s * 15,
                    "end_min": e * 15,
                    "start_hhmm": _slot_hhmm(s),
                    "end_hhmm": _slot_hhmm(e),
                    "zone_ids": ids,
                    "zone_names": names,
                }
            )
        out.append(
            {
                "day": day,
                "weekday": _WEEKDAYS[day - 1],
                "enabled": bool(_as_bool(entry.get("open"))),
                "periods": periods,
            }
        )
    return out


def _parse_coverage(raw_list: Any, zone_names: dict) -> dict | None:
    """Per-zone mowing coverage from get-path-info-time.

    Input: ``[{partitionId, area, finishedArea, partitionPercentage}, ...]``.
    Output: ``{overall_pct, total_area, finished_area, zones:[{id,name,area,
    finished,pct}]}`` or ``None`` when there is nothing to report.
    """
    if not isinstance(raw_list, list) or not raw_list:
        return None
    zones: list[dict] = []
    tot_area = 0.0
    tot_fin = 0.0
    for it in raw_list:
        if not isinstance(it, dict):
            continue
        zid = _as_int(it.get("partitionId"))
        area = _as_float(it.get("area"))
        fin = _as_float(it.get("finishedArea"))
        pct = _as_int(it.get("partitionPercentage"))
        if area is not None:
            tot_area += area
        if fin is not None:
            tot_fin += fin
        zones.append(
            {
                "id": zid,
                "name": zone_names.get(zid, f"Zone {zid}") if zid is not None else "Zone",
                "area": area,
                "finished": fin,
                "pct": pct,
            }
        )
    if not zones:
        return None
    overall = round(100.0 * tot_fin / tot_area) if tot_area > 0 else None
    return {
        "overall_pct": overall,
        "total_area": round(tot_area, 2),
        "finished_area": round(tot_fin, 2),
        "zones": zones,
    }


def _compute_next_mow(set_list: Any, now: Any) -> str | None:
    """Next scheduled mow from ``set_list.plan`` as a readable "Wed 09:45".

    ``plan`` is a list of ``{day:1-7 (1=Mon), open:0/1, period:[[start,end],...]}``
    where start/end are 15-minute slot indices from 00:00 (39 -> 09:45). Returns
    the soonest upcoming open day+start relative to ``now`` (searching a full
    week including today), or ``None`` if nothing is scheduled.
    """
    plan = set_list.get("plan") if isinstance(set_list, dict) else None
    if not isinstance(plan, list):
        return None
    day_starts: dict[int, list[int]] = {}
    for entry in plan:
        if not isinstance(entry, dict) or not _as_bool(entry.get("open")):
            continue
        day = _as_int(entry.get("day"))
        if day is None:
            continue
        for period in entry.get("period") or []:
            start = None
            if isinstance(period, (list, tuple)) and period:
                start = _as_int(period[0])
            elif isinstance(period, dict):
                start = _as_int(period.get("start_time"))
            if start is not None:
                day_starts.setdefault(day, []).append(start)
    if not day_starts:
        return None
    now_day = (now.weekday() + 1) % 7 + 1  # py Mon=0..Sun=6 -> Navimow 1=Sun..7=Sat
    now_slot = (now.hour * 60 + now.minute) / 15.0
    for offset in range(0, 8):
        day = ((now_day - 1 + offset) % 7) + 1
        for start in sorted(day_starts.get(day, [])):
            if offset == 0 and start <= now_slot:
                continue
            hh, mm = divmod(start * 15, 60)
            if hh > 23:
                continue
            when = (now + timedelta(days=offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            return when.strftime("%a %H:%M")
    return None


class NavimowCoordinator(DataUpdateCoordinator[dict]):
    """Aggregates one mower's state for all platforms."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="navimow_pro",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        data = entry.data
        self.client = NavimowCloudClient(
            device_id=data[CONF_DEVICE_ID],
            tokens=Tokens(
                access_token=data.get(CONF_ACCESS_TOKEN, ""),
                refresh_token=data.get(CONF_REFRESH_TOKEN, ""),
                region=data.get(CONF_REGION, "fra"),
            ),
            uid=data.get(CONF_UID, ""),
            region=data.get(CONF_REGION, "fra"),
            language=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )
        self.sn: str = data[CONF_VEHICLE_SN]
        self.vehicle_type: int = int(data.get(CONF_VEHICLE_TYPE, 0) or 0)
        self._cycle = 0
        self._raw_cache: dict[str, Any] = {}
        # Parsed map geometry, cached across cycles (the map rarely changes).
        self._map_geometry: dict | None = None
        self._map_cache_key: tuple | None = None
        # Mowed-trail reconstruction: accumulate the robot's position while it
        # cuts (see SWATH/TRAIL constants). A new session (docked -> mowing
        # transition) clears the trail. Mutated in the executor thread, so it is
        # guarded by a lock against an overlapping command-triggered refresh.
        self._trail: list[list[float]] = []
        self._trail_lock = threading.Lock()
        self._prev_state_code: str | None = None
        # Persist the trail across restarts (loaded in async_load_trail, saved
        # debounced from _async_update_data only when _trail_dirty is set).
        self._trail_store: Store = trail_store(hass, entry.entry_id)
        self._trail_dirty = False
        self._last_trail_save = 0.0  # time.monotonic() of the last real write
        # User's pending zone choice for the native lawn_mower "mow" button.
        # The zone select only STORES this (does not start); the mower's
        # start_mowing reads it. Empty list = all zones. In-memory (resets on
        # restart to "all").
        self.selected_zone_ids: list[int] = []

    async def async_load_trail(self) -> None:
        """Restore the persisted mowed trail (call before the first refresh).

        SN-guarded: ignore data saved for a different mower (e.g. after a backup
        restore). Also restores ``_prev_state_code`` so the "a new mow resets the
        trail" rule still holds across a restart (no spurious wipe of the
        restored trail; a genuinely new docked->mowing transition still resets).
        Never raises -- persistence must not block setup.

        Known limitation: if HA is down across a full session boundary (mow A
        ends and mow B starts while HA is off) and the robot is mowing again at
        startup, the docked->mowing transition is never observed, so B's path is
        appended to A's (the two mows merge on the map) until the next observed
        transition resets it. This is the lesser evil of an unavoidable
        ambiguity -- the alternative (reset on any mowing-at-startup) would wipe
        a genuinely in-progress mow on every mid-mow restart, which is far more
        common.
        """
        try:
            data = await self._trail_store.async_load()
        except Exception:  # noqa: BLE001 - never block setup over persistence
            _LOGGER.debug("trail: restore failed", exc_info=True)
            return
        if not isinstance(data, dict) or data.get("sn") != self.sn:
            return
        raw = data.get("trail")
        if not isinstance(raw, list):
            return
        trail: list[list[float]] = []
        for p in raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                try:
                    trail.append([float(p[0]), float(p[1])])
                except (TypeError, ValueError):
                    continue
        with self._trail_lock:
            self._trail = trail[-TRAIL_MAX_POINTS:]
            prev = data.get("prev_state_code")
            self._prev_state_code = prev if isinstance(prev, str) else None
        _LOGGER.debug("trail: restored %d points", len(trail))

    def _trail_store_data(self) -> dict:
        """Snapshot of the trail for the persistent store (read at write time)."""
        with self._trail_lock:
            return {
                "sn": self.sn,
                "prev_state_code": self._prev_state_code,
                "trail": list(self._trail),
            }

    # ------------------------------------------------------------------ poll
    async def _async_update_data(self) -> dict:
        try:
            snapshot = await self.hass.async_add_executor_job(self._fetch_blocking)
        except NavimowAuthError as err:
            # Trigger the HA reauth flow (user must re-enter the password).
            # Back the poll off so a dead session isn't retried every 3 s.
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
            raise ConfigEntryAuthFailed(str(err)) from err
        except NavimowError as err:
            # Don't keep hammering the cloud every 3 s through an outage.
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
            raise UpdateFailed(f"API error: {err}") from err

        self._persist_session()
        # Persist the trail only when it changed. async_delay_save is a
        # trailing-edge debounce (each call resets its timer), so during a
        # continuous mow -- polled every few seconds -- it would never fire;
        # throttle a real checkpoint every _TRAIL_SAVE_DELAY seconds and use the
        # delayed save for the final flush after the trail stops changing (also
        # flushed on a graceful restart via HA's final-write listener). Writes
        # stay bounded (~1 per 30 s while mowing, none while docked/idle) to
        # spare the SD card. Best-effort: never let persistence break a poll.
        if self._trail_dirty:
            self._trail_dirty = False
            try:
                now = time.monotonic()
                if now - self._last_trail_save >= _TRAIL_SAVE_DELAY:
                    self._last_trail_save = now
                    await self._trail_store.async_save(self._trail_store_data())
                else:
                    self._trail_store.async_delay_save(
                        self._trail_store_data, _TRAIL_SAVE_DELAY
                    )
            except Exception:  # noqa: BLE001 - persistence must not break polling
                _LOGGER.debug("trail: save failed", exc_info=True)
        # Adaptive polling: cut-time is sampled densely (to trace the path),
        # returning is fast, idle is conservative.
        code = snapshot["state_code"]
        if code == STATE_MOWING:
            interval = MOW_SCAN_INTERVAL
        elif code in ACTIVE_STATES:
            interval = FAST_SCAN_INTERVAL
        else:
            interval = DEFAULT_SCAN_INTERVAL
        self.update_interval = timedelta(seconds=interval)
        return snapshot

    def _fetch_blocking(self) -> dict:
        """Runs in an executor thread. Fetches + parses one snapshot."""
        sn, vtype = self.sn, self.vehicle_type
        raw = self._raw_cache

        # One-time static info (model, area limits).
        if "device_info" not in raw:
            try:
                raw["device_info"] = self.client.device_info(sn)
            except NavimowAuthError:
                raise
            except NavimowError:
                raw["device_info"] = {}

        # Fast, every cycle.
        raw["index2"] = self.client.index2(sn)
        raw["auth_list"] = self.client.auth_list()
        try:
            raw["location"] = self.client.location(sn, vtype)
        except NavimowAuthError:
            raise  # unrecoverable auth failure -> reauth (never swallow)
        except NavimowError:
            raw.setdefault("location", {})
        # Per-zone coverage (cheap; persists for the last session even when docked).
        try:
            raw["path_info_time"] = self.client.path_info_time(sn)
        except NavimowAuthError:
            raise
        except NavimowError:
            raw.setdefault("path_info_time", [])

        # Slow, only every N cycles (or on the first successful fetch).
        self._cycle = (self._cycle + 1) % SLOW_REFRESH_EVERY
        if self._cycle == 1 or "set_list" not in raw:
            getters = {
                "set_list": lambda: self.client.set_list(sn),
                "maintenance": lambda: self.client.maintenance(sn),
                "today_plan": lambda: self.client.today_plan(sn, vtype),
                "map_list": lambda: self.client.map_list(sn),
            }
            for key, getter in getters.items():
                try:
                    raw[key] = getter()
                except NavimowAuthError:
                    raise
                except NavimowError:
                    raw.setdefault(key, {})
            # Map geometry: fetch + decode on the slow cycle, cached by map
            # id/edit-time so the 2 KB blob is only downloaded when it changes.
            self._maybe_fetch_map(raw)
        return self._parse(raw)

    def _maybe_fetch_map(self, raw: dict) -> None:
        """Fetch + decode the map once, then only when the map version changes."""
        location = raw.get("location") or {}
        map_id = location.get("map_id")
        map_base_id = location.get("map_base_id")
        edit_time = location.get("map_edit_time")
        # Fall back to map-list if get-location didn't carry the ids.
        if map_id is None:
            map_list = raw.get("map_list")
            first = map_list[0] if isinstance(map_list, list) and map_list else {}
            if isinstance(first, dict):
                map_id = first.get("map_id")
                map_base_id = first.get("map_base_id")
                edit_time = first.get("edittime")
        if map_id is None or map_base_id is None:
            return

        key = (str(map_id), str(map_base_id), str(edit_time))
        if self._map_geometry is not None and self._map_cache_key == key:
            return  # cached geometry is still current.

        # Prefer the UNCOMPRESSED map-detail (plain JSON, no zstd dependency);
        # fall back to the zstd-compressed variant only if plain fails/empty.
        geometry: dict | None = None
        try:
            plain = self.client.map_detail_plain(self.sn, str(map_id), str(map_base_id))
            geometry = _parse_map_detail_plain(plain)
        except NavimowError:
            geometry = None
        if geometry is None:
            try:
                blob = self.client.map_detail(self.sn, str(map_id), str(map_base_id))
                geometry = _parse_map_detail(blob)
            except NavimowError:
                return
        if geometry is None:
            return

        # Optional dock/approach path (a different, docking-local frame -> we do
        # NOT render it, but keep it available for debugging / future overlays).
        try:
            station_raw = self.client.station_map(self.sn, str(map_id), str(map_base_id))
            pts = _points_xy((station_raw or {}).get("points"))
            if pts:
                geometry["station_map"] = {
                    "points": pts,
                    "start_from_pile": bool((station_raw or {}).get("start_from_pile")),
                }
        except NavimowError:
            pass

        self._map_geometry = geometry
        self._map_cache_key = key

    # ------------------------------------------------------------- persist
    def _persist_session(self) -> None:
        state = self.client.session_state()
        merged = {
            **self.entry.data,
            CONF_ACCESS_TOKEN: state["access_token"],
            CONF_REFRESH_TOKEN: state["refresh_token"],
            CONF_UID: state["uid"],
            CONF_REGION: state["region"],
        }
        if merged != dict(self.entry.data):
            self.hass.config_entries.async_update_entry(self.entry, data=merged)

    # --------------------------------------------------------------- parsing
    def _auth_item(self, auth_list: Any) -> dict:
        if isinstance(auth_list, list):
            for it in auth_list:
                if isinstance(it, dict) and str(it.get("vehicle_sn")) == self.sn:
                    return it
            if auth_list and isinstance(auth_list[0], dict):
                return auth_list[0]
        return {}

    def _resolve_zones(self, current_ids: list[int]) -> list[dict]:
        """Available-zone list. Priority: real map zones > Options > current.

        The primary source is the decoded map geometry (``sub_maps``), which
        gives the real partition ids and names (e.g. id 1 "Zone 1", id 5
        "Zone 2"). The Options-flow ``id:name`` mapping is only a last-resort
        fallback for when the map cannot be decoded.
        """
        zones: list[dict] = []
        # 1) Real zones from the decoded map (sub_maps).
        for z in (self._map_geometry or {}).get("zones") or []:
            zid = z.get("id")
            if zid is None:
                continue
            zones.append({"id": zid, "name": z.get("name") or f"Zone {zid}", "area": z.get("area")})
        # 2) Fallback: user-configured mapping via the Options flow.
        if not zones:
            zones = _parse_zone_options(self.entry.options.get(OPT_ZONES))
        # 3) Last resort: whatever region is currently selected/mowing.
        if not zones and current_ids:
            zones = [{"id": rid, "name": f"Zone {rid}"} for rid in current_ids]

        # Attach the encoded command payloads.
        for z in zones:
            z["partition_ids_hex"] = encode_partition_ids([z["id"]])
        return zones

    def _parse(self, raw: dict) -> dict:
        index2 = raw.get("index2") or {}
        auth = self._auth_item(raw.get("auth_list"))
        location = raw.get("location") or {}
        set_list = raw.get("set_list") or {}
        maintenance = raw.get("maintenance") or {}
        today_plan = raw.get("today_plan") or {}

        state_code = str(index2.get("vehicle_state") or auth.get("vehicle_state") or "")
        battery = _as_int(index2.get("soc") if index2.get("soc") is not None else auth.get("soc"))
        network_status = _as_int(index2.get("network_status"))
        online = network_status == 1 if network_status is not None else None

        # Error detection from index2's inline error array (the hint-error
        # endpoint returns a compressed blob we intentionally do not decode).
        error_list = index2.get("error_data") or _find(index2, "errorData", "error_list") or []
        has_error = bool(error_list)
        error_text = None
        if has_error and isinstance(error_list, list) and error_list:
            first = error_list[0]
            if isinstance(first, dict):
                error_text = str(
                    first.get("desc") or first.get("message") or first.get("code") or "error"
                )

        # Current zone(s) from index2.partitionIdList (big-endian). An EMPTY
        # list means "all zones / whole map" -> show "All", never "Unknown".
        current_ids = decode_partition_id_list(str(index2.get("partitionIdList") or ""))
        zones = self._resolve_zones(current_ids)
        zone_names = {z["id"]: z["name"] for z in zones}
        current_zone = (
            ", ".join(zone_names.get(i, f"Zone {i}") for i in current_ids)
            if current_ids
            else "All"
        )
        available_ids = [z["id"] for z in zones]
        partition_setup = partition_setup_mask(available_ids) if available_ids else None
        map_geom = self._map_geometry or {}

        # Coverage (per-zone %) + reconstructed mowed trail (accumulated position).
        position = self._parse_position(location)
        coverage = _parse_coverage(raw.get("path_info_time"), zone_names)
        trail = self._update_trail(position, state_code)

        activity = VEHICLE_STATE_TO_ACTIVITY.get(state_code, ACTIVITY_DOCKED)
        if has_error:
            activity = ACTIVITY_ERROR

        # --- settings (MowerSettingBean; snake_case in set-list, camelCase in bean)
        night_mow = _as_bool(_find(set_list, "night_mow_switch", "nightMowSwitch"))
        rain_sensor = _as_bool(_find(set_list, "rainSensor", "rain_sensor"))
        rain_detection = _as_bool(_find(set_list, "rainDetectionSwitch", "rain_detection_switch"))
        sound = _as_bool(_find(set_list, "soundSwitch", "sound_switch"))
        power_saving = _as_bool(_find(set_list, "lowPowerSet", "low_power_set"))
        child_lock = _as_bool(_find(set_list, "childLock", "child_lock"))

        settings = {
            "night_mow": night_mow,
            "rain_sensor": rain_sensor,
            "rain_detection": rain_detection,
            "sound": sound,
            "power_saving": power_saving,
            "child_lock": child_lock,
            "cut_height": _as_int(_find(set_list, "height")),
            "charging_limit": _as_int(_find(set_list, "chargingLimit")),
        }

        # --- maintenance (blades / chassis) -- field names are firmware-specific
        maint = self._parse_maintenance(maintenance)

        snapshot: dict[str, Any] = {
            # identity / static
            "vehicle_sn": self.sn,
            "vehicle_type": self.vehicle_type,
            "model": str(auth.get("subType") or _find(raw.get("device_info"), "model") or ""),
            "name": str(auth.get("selfDefinedName") or auth.get("vehicle_name") or "Navimow"),
            # core state
            "battery": battery,
            "state_code": state_code,
            "state": VEHICLE_STATE_LABELS.get(state_code, f"Unknown ({state_code})" if state_code else "Unknown"),
            "activity": activity,
            "online": online,
            "docked": state_code in DOCKED_STATES,
            "error": has_error,
            "error_text": error_text,
            # progress / areas
            "mowing_progress": _as_int(_find(location, "mowing_percentage", "mowingPercentage", "progress")),
            "session_area": _as_float(location.get("subtotal_area")),
            "weekly_area": _as_float(location.get("mowing_week_area")),
            "total_area": (
                map_geom.get("area")
                if map_geom.get("area") is not None
                else _as_float(_find(raw.get("device_info"), "map_area_limit"))
            ),
            "next_mow": _compute_next_mow(set_list, dt_util.now()),
            # zones
            "zones": zones,
            "current_zone": current_zone,
            "current_zone_ids": current_ids,
            "available_partition_setup": partition_setup,
            # weekly mowing schedule (days -> periods -> zones)
            "schedule": _parse_schedule(set_list, zone_names),
            # connectivity
            "signal": _as_int(index2.get("network_signal") or auth.get("network_signal")),
            "signal_wifi": _as_int(index2.get("network_signal_wifi") or auth.get("network_signal_wifi")),
            "signal_4g": _as_int(index2.get("network_signal_4G") or auth.get("network_signal_4G")),
            "network_type": _as_int(index2.get("networkType") or auth.get("networkType")),
            # location / map
            "latitude": _as_float(_find(location, "latitude", "lat")),
            "longitude": _as_float(_find(location, "longitude", "lng", "lon")),
            "position": position,
            "path": self._parse_path(location),
            # per-zone coverage (%) + reconstructed mowed trail ([[x,y],...])
            "coverage": coverage,
            "trail": trail,
            # decoded map geometry (None until the map is fetched/decoded)
            "map": (
                {
                    "area": map_geom.get("area"),
                    "zones": map_geom.get("zones") or [],
                    "obstacles": map_geom.get("obstacles") or [],
                    "vision_off": map_geom.get("vision_off") or [],
                    "station": map_geom.get("station"),
                    "station_map": map_geom.get("station_map"),
                }
                if map_geom
                else None
            ),
            # groups
            "settings": settings,
            "maintenance": maint,
            # raw (for entity extra attributes / debugging)
            "raw": {
                "index2": index2,
                "auth_item": auth,
                "location": location,
                "set_list": set_list,
                "maintenance": maintenance,
                "today_plan": today_plan,
            },
        }
        return snapshot

    @staticmethod
    def _parse_maintenance(maintenance: Any) -> dict:
        """Blades/chassis life from get-component-maintenance.

        The endpoint returns ``{knife:{setTime,usedTime}, chassis:{...}, ...}``.
        Interpretation (best-effort, documented): ``setTime`` is the user's
        maintenance-reminder interval in HOURS, ``usedTime`` the component
        runtime in MINUTES, so::

            life% = clamp(100 * (1 - usedTime / (setTime * 60)), 0, 100)

        e.g. knife 80h/749min -> 84%, chassis 200h/749min -> 94%. The raw
        setTime/usedTime are surfaced as sensor attributes.
        """
        result = {
            "blades_pct": None,
            "blades_set_hours": None,
            "blades_used_min": None,
            "chassis_pct": None,
            "chassis_set_hours": None,
            "chassis_used_min": None,
        }
        if not isinstance(maintenance, dict):
            return result

        def life(component: Any) -> tuple[int | None, int | None, int | None]:
            if not isinstance(component, dict):
                return None, None, None
            set_hours = _as_float(component.get("setTime"))
            used_min = _as_float(component.get("usedTime"))
            pct = None
            if set_hours is not None and used_min is not None and set_hours > 0:
                pct = 100.0 * (1.0 - used_min / (set_hours * 60.0))
                pct = round(max(0.0, min(100.0, pct)))
            return pct, _as_int(set_hours), _as_int(used_min)

        result["blades_pct"], result["blades_set_hours"], result["blades_used_min"] = life(
            maintenance.get("knife")
        )
        result["chassis_pct"], result["chassis_set_hours"], result["chassis_used_min"] = life(
            maintenance.get("chassis")
        )
        return result

    def _update_trail(
        self, position: dict | None, state_code: str
    ) -> list[list[float]]:
        """Accumulate the mowed path from position samples (see SWATH constants).

        A new mowing session -- entering STATE_MOWING from a docked/idle state --
        clears the trail (deterministic, independent of cloud coverage timing).
        Pause/resume does NOT reset (paused is not a docked state). While cutting,
        the current position is appended if it moved at least TRAIL_MIN_STEP_M
        (drops jitter). The trail persists after docking so the finished pattern
        stays visible until the next mow. Guarded by a lock because it runs in an
        executor thread and a command-triggered refresh may overlap the poll.
        """
        with self._trail_lock:
            if state_code == STATE_MOWING and self._prev_state_code in DOCKED_STATES:
                if self._trail:
                    self._trail = []  # fresh mow (left the dock) -> new trail
                    self._trail_dirty = True
            self._prev_state_code = state_code

            if state_code == STATE_MOWING and position:
                x, y = position.get("x"), position.get("y")
                if x is not None and y is not None:
                    if not self._trail:
                        self._trail.append([x, y])
                        self._trail_dirty = True
                    else:
                        lx, ly = self._trail[-1]
                        if (x - lx) ** 2 + (y - ly) ** 2 >= TRAIL_MIN_STEP_M ** 2:
                            self._trail.append([x, y])
                            if len(self._trail) > TRAIL_MAX_POINTS:
                                del self._trail[: len(self._trail) - TRAIL_MAX_POINTS]
                            self._trail_dirty = True
            return list(self._trail)

    @staticmethod
    def _parse_position(location: Any) -> dict | None:
        x = _as_float(_find(location, "posture_x", "postureX", "last_posture_x", "x"))
        y = _as_float(_find(location, "posture_y", "postureY", "last_posture_y", "y"))
        # posture_theta is the real heading field (radians); keep older guesses.
        heading = _as_float(
            _find(location, "posture_theta", "last_posture_theta", "posture_yaw", "yaw", "heading", "angle")
        )
        if x is None and y is None:
            return None
        return {"x": x, "y": y, "heading": heading}

    @staticmethod
    def _parse_path(location: Any) -> list | None:
        points = _find(location, "points", "path", "trail")
        if isinstance(points, list) and points:
            return points
        return None

    # -------------------------------------------------------------- commands
    async def async_send(self, func, *args) -> Any:
        """Run a client command in the executor then request a quick refresh."""
        result = await self.hass.async_add_executor_job(func, *args)
        self._persist_session()
        await self.async_request_refresh()
        return result
