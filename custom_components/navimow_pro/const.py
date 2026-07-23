"""Constants and small pure helpers for the Navimow (Private) integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "navimow_pro"
MANUFACTURER: Final = "Segway Navimow"

# --- Config entry / options keys -------------------------------------------
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"  # only used transiently during the config flow
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_UID: Final = "uid"
CONF_DEVICE_ID: Final = "device_id"
CONF_REGION: Final = "region"
CONF_LANGUAGE: Final = "language"
CONF_VEHICLE_SN: Final = "vehicle_sn"
CONF_VEHICLE_TYPE: Final = "vehicle_type"
CONF_VEHICLE_NAME: Final = "vehicle_name"
CONF_MODEL: Final = "model"

# Options-flow keys
OPT_ZONES: Final = "zones"  # user-supplied "id:name,id:name" mapping (fallback zone list)

DEFAULT_LANGUAGE: Final = "en"

# --- Polling ---------------------------------------------------------------
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds, conservative (private API has no push)
FAST_SCAN_INTERVAL: Final = 12  # while returning to dock
MOW_SCAN_INTERVAL: Final = 3  # while actively cutting: dense enough to trace the path
# how many normal cycles between refreshing slow-changing data (settings/maint/plan)
SLOW_REFRESH_EVERY: Final = 6

# --- Coverage / mowed-trail overlay ----------------------------------------
# We reconstruct the mowed area exactly like the app does: by sampling the
# robot's position frequently while it cuts and painting the swept path. (The
# app polls get-location over HTTP ~every 1.5-2 s; the cloud exposes no coverage
# grid and get-path-info-data-compress is not reachable standalone.)
SWATH_WIDTH_M: Final = 0.25  # approx cutting width (m) used as the trail stroke width
TRAIL_MAX_POINTS: Final = 10000  # cap accumulated points (a long mow) to bound memory
TRAIL_MIN_STEP_M: Final = 0.12  # drop position jitter: ignore steps smaller than this
TRAIL_BREAK_M: Final = 2.0  # split the drawn trail where consecutive samples jump farther

# --- vehicle_state (empirical hex from index2 / auth-list; see README_POC) --
STATE_IDLE_DOCKED: Final = "0101"
STATE_IDLE_DOCKED_POST: Final = "0102"
STATE_MOWING: Final = "0210"
STATE_PAUSED: Final = "0211"
STATE_RETURNING: Final = "0220"

# HA lawn_mower activity constants (kept as plain strings to avoid import churn)
ACTIVITY_MOWING: Final = "mowing"
ACTIVITY_PAUSED: Final = "paused"
ACTIVITY_DOCKED: Final = "docked"
ACTIVITY_RETURNING: Final = "returning"
ACTIVITY_ERROR: Final = "error"

VEHICLE_STATE_TO_ACTIVITY: Final[dict[str, str]] = {
    STATE_IDLE_DOCKED: ACTIVITY_DOCKED,
    STATE_IDLE_DOCKED_POST: ACTIVITY_DOCKED,
    STATE_MOWING: ACTIVITY_MOWING,
    STATE_PAUSED: ACTIVITY_PAUSED,
    STATE_RETURNING: ACTIVITY_RETURNING,
}

VEHICLE_STATE_LABELS: Final[dict[str, str]] = {
    STATE_IDLE_DOCKED: "Docked",
    STATE_IDLE_DOCKED_POST: "Docked (finished)",
    STATE_MOWING: "Mowing",
    STATE_PAUSED: "Paused",
    STATE_RETURNING: "Returning to dock",
}

# States considered "docked / at station".
DOCKED_STATES: Final = {STATE_IDLE_DOCKED, STATE_IDLE_DOCKED_POST}
# States where the mower is out working (used for adaptive polling).
ACTIVE_STATES: Final = {STATE_MOWING, STATE_RETURNING}


# --- Mow "restart vs continue" (partitionSetup, proven via NSKPTLOG diff) ------
# The app's "Cancella i progressi e riparti da zero" checkbox is encoded in the
# s:mower command's `partitionSetup` field (NOT a separate flag): 34 = restart
# from scratch (clear progress), 18 = continue only the uncut area. Verified live
# by diffing the plaintext command with the box OFF (18) vs ON (34), same zone.
# partitionSetup is zone-independent (partitionIds carries the zone selection).
MOW_SETUP_RESTART: Final = 34  # "riparti da zero" (clear progress)
MOW_SETUP_CONTINUE: Final = 18  # "continua" (keep progress, mow only uncut)


# --- Zone / partition encoding (proven; see Navimow_PrivateAPI_Catalogo §3.2)
def encode_partition_ids(region_ids: list[int]) -> str:
    """Encode region ids as concatenated little-endian uint16 hex.

    Proven: region id 1 -> "0100", 5 -> "0500", [1,5] -> "01000500".
    """
    out = []
    for rid in region_ids:
        out.append(f"{rid & 0xFF:02x}{(rid >> 8) & 0xFF:02x}")
    return "".join(out)


def partition_setup_mask(available_region_ids: list[int]) -> int:
    """Bitmask of *available* map regions: (1<<id) OR'd together.

    Proven: available regions {1,5} -> (1<<1)|(1<<5) = 34.
    """
    mask = 0
    for rid in available_region_ids:
        mask |= 1 << rid
    return mask


def decode_partition_id_list(be_hex: str) -> list[int]:
    """Decode index2.partitionIdList (big-endian uint16 hex) to region ids.

    Proven: "0001" -> [1], "00010005" -> [1, 5].
    """
    ids: list[int] = []
    if not be_hex:
        return ids
    s = be_hex.strip()
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return ids
    for i in range(0, len(raw) - 1, 2):
        ids.append(int.from_bytes(raw[i : i + 2], "big"))
    return [i for i in ids if i]
