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
CONF_MOWER_HOST: Final = "mower_host"  # resolved at setup, so runtime is deterministic
CONF_LANGUAGE: Final = "language"
CONF_VEHICLE_SN: Final = "vehicle_sn"
CONF_VEHICLE_TYPE: Final = "vehicle_type"
CONF_VEHICLE_NAME: Final = "vehicle_name"
CONF_MODEL: Final = "model"

# Options-flow keys
OPT_ZONES: Final = "zones"  # user-supplied "id:name,id:name" mapping (fallback zone list)

DEFAULT_LANGUAGE: Final = "en"

# The vendor app accepts 6-18 character passwords ("Please enter password (6-18
# characters)" in its own resources) and silently keeps only the first 18 when a
# longer one is set. Entering the full thing here then fails as a wrong password,
# which is baffling -- so a refusal with an over-long password gets its own hint
# rather than a plain "wrong password". Not enforced: an account may legitimately
# hold a longer password set elsewhere.
PASSWORD_MAX_LEN: Final = 18

# --- Server regions -----------------------------------------------------------
# The account lives on ONE regional server; talking to the wrong one fails at
# login ("account not exists"), so the region must be resolved, not assumed.
# Enumerated 2026-07-28 by DNS across ninebot.com / willand.com / navimow.com and
# verified functionally (each host answers a signed /v3/region call):
#
#   us   Americas        fra  Europe (alias "eu")
#   sg   Asia-Pacific    bj   mainland China
#       (alias "sea")
#
# `ninebot.com` carries all four; `willand.com` is the older domain (it has no
# `us`) but is the one proven in production for `fra`, so it stays first there.
DEFAULT_REGION: Final = "fra"
REGION_AUTO: Final = "auto"  # config-flow choice: detect from the account

# Codes the backend also answers to, mapped onto the primary code. `ore`
# (Oregon) is what a US account actually reports back and resolves to the same
# server as `us`.
_REGION_ALIASES: Final = {"eu": "fra", "sea": "sg", "ore": "us"}

PASSPORT_HOSTS: Final = {
    "fra": ("api-passport-fra.willand.com", "api-passport-fra.ninebot.com"),
    "sg": ("api-passport-sg.willand.com", "api-passport-sg.ninebot.com"),
    "us": (
        "api-passport-us.ninebot.com",
        "api-passport-ore.ninebot.com",  # same IP; the code a US account reports
        "api-passport-ore.willand.com",
    ),
    "bj": ("api-passport-bj.willand.com", "api-passport-bj.ninebot.com"),
}

# Mower-cloud candidates per region, tried in order at setup; the first one that
# answers is persisted (CONF_MOWER_HOST). There is no `navimow-us*` host: a US
# account's mower data is served by the Frankfurt host, confirmed from a working
# US setup (region "ore", mower host navimow-fra.ninebot.com).
MOWER_HOSTS: Final = {
    "fra": ("navimow-fra.ninebot.com", "navimow-fra.willand.com"),
    "sg": ("navimow-sg.willand.com",),
    "bj": ("navimow-bj.ninebot.com", "navimow-bj.willand.com"),
    "us": ("navimow-fra.ninebot.com", "navimow-ore.willand.com"),
}

REGIONS: Final = tuple(PASSPORT_HOSTS)

_ALL_MOWER_HOSTS: Final = tuple(
    dict.fromkeys(h for hosts in MOWER_HOSTS.values() for h in hosts)
)

# Order to ask "which region owns this account?": the primary host of each region
# first (so every region is covered in 4 calls), then the secondary ones as a
# fallback. NB: do NOT use passport_hosts(None) for this -- it resolves to the
# default region and would only ever ask Europe.
ALL_PASSPORT_HOSTS: Final = tuple(
    dict.fromkeys(
        [hosts[0] for hosts in PASSPORT_HOSTS.values() if hosts]
        + [h for hosts in PASSPORT_HOSTS.values() for h in hosts[1:]]
    )
)


def canonical_region(region: str | None) -> str:
    """Normalise a region code ('EU' -> 'fra'); unknown codes are kept as-is."""
    code = str(region or "").strip().lower()
    if not code:
        return DEFAULT_REGION
    return _REGION_ALIASES.get(code, code)


def passport_hosts(region: str | None) -> tuple[str, ...]:
    """Passport hosts to try for a region (all of them if the code is unknown)."""
    return PASSPORT_HOSTS.get(canonical_region(region)) or ALL_PASSPORT_HOSTS


def mower_hosts(region: str | None) -> tuple[str, ...]:
    """Mower-cloud hosts to try for a region.

    Falls back to every known host when the region has none mapped (``us``) or is
    unrecognised, so a new/unknown region degrades to "try everything" instead of
    failing outright.
    """
    hosts = MOWER_HOSTS.get(canonical_region(region)) or ()
    extra = tuple(h for h in _ALL_MOWER_HOSTS if h not in hosts)
    return hosts + extra

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

# The state code's first byte is the family: 01 docked, 02 working, 03 stopped
# with a fault (seen live as 0310 on a mower stuck in the garden). Codes we have
# not identified individually still get a sensible family label, and an 03xx code
# is what triggers fetching the fault detail.
STATE_FAMILY_LABELS: Final[dict[str, str]] = {
    "01": "Docked",
    "02": "Working",
    "03": "Stopped (fault)",
}
FAULT_STATE_FAMILY: Final = "03"

KNOWN_STATES: Final = frozenset(VEHICLE_STATE_TO_ACTIVITY)

# --- Fault codes --------------------------------------------------------------
# The mower reports a numeric fault code; these are our own short descriptions of
# what each one means, so a stopped mower says something useful instead of just a
# number. Deliberately paraphrased and kept terse -- the vendor's own wording is
# not reproduced here. Unknown codes fall back to "Fault <code>".
#
# Families: 1xxx cutting/battery, 2xxx sensors, 3xxx drive, 4xxx electronics and
# links, 5xxx charging station, 6xxx navigation and mission, 7xxx positioning,
# 8xxx vision.
ERROR_CODES: Final[dict[str, str]] = {
    "1001": "Blade motor failed to start",
    "1002": "Blade motor overvoltage",
    "1003": "Blade motor undervoltage",
    "1004": "Blade motor overcurrent",
    "1005": "Blade motor jammed - check the blade disc for obstructions",
    "1008": "Cutting-height motor jammed",
    "1009": "Cutting-height motor overcurrent",
    "1010": "Control unit self-check failed",
    "1018": "Cutting-height motor control failure",
    "1020": "Battery overvoltage",
    "1021": "Battery undervoltage",
    "1022": "Battery discharge protection",
    "1023": "Battery charge protection",
    "1024": "Battery too hot",
    "1025": "Battery too cold",
    "2010": "Sensor self-check failed",
    "3001": "Wheel motor jammed - check the wheels for obstructions",
    "3002": "Wheel motor overspeed",
    "3003": "Motion sensor (IMU) bias error",
    "3004": "Wheel motor calibration error",
    "3005": "Wheel motor short circuit",
    "3006": "Wheel motor calibration corrupted",
    "3007": "Wheel motor communication error",
    "3008": "Wheel motor overheated",
    "3010": "Drive unit self-check failed",
    "4001": "Storage error",
    "4003": "Control electronics overheated",
    "4004": "Drive electronics overheated",
    "4007": "Bluetooth link error",
    "4008": "Cloud link error",
    "4010": "Blade motor communication error",
    "4011": "Charging station communication error",
    "4012": "Sensor communication error",
    "4013": "Ultrasonic module communication error",
    "4014": "Charging station communication error",
    "4020": "Electronics self-check failed",
    "5001": "Charging station antenna error",
    "5002": "Charging station guide wire error",
    "5003": "Charging station over-current or over-voltage",
    "6002": "Mower is outside the boundary",
    "6003": "Mower has turned over",
    "6004": "Cannot find the way back to the charging station",
    "6005": "Cannot find the way back to the charging station",
    "6006": "Bumper triggered continuously",
    "6007": "Mower has been lifted",
    "6008": "Bumper triggered too many times",
    "6010": "Cannot plan a route - move it to flat ground inside the boundary",
    "6011": "Mower is stuck - free it",
    "6012": "Charging station or antenna moved since mapping",
    "6014": "Mower is outside the boundary - put it back inside",
    "6015": "Mower is stuck on an obstacle - free it and place it on flat ground",
    "6016": "Mower is stuck on an obstacle - free it and place it on flat ground",
    "6017": "Mower is stuck on an obstacle - free it and place it on flat ground",
    "6018": "Cannot plan a route - move it to flat ground inside the boundary",
    "6020": "Mower failed to start",
    "6021": "Mower could not reach its destination",
    "6022": "Mower could not get through the channel",
    "6023": "Mower could not get through",
    "7001": "Motion sensor data error",
    "7002": "Cannot read the drive unit version",
    "7003": "GPS data error",
    "7004": "Compass data error",
    "7005": "Positioning data stream fault",
    "7006": "Could not recover its position",
    "8001": "VisionFence camera communication error",
    "8002": "VisionFence map error",
    "8003": "Camera is dirty - clean the lens",
    "8005": "VisionFence camera disconnected",
    "8006": "VisionFence camera system error",
}

# Most faults need the STOP button pressed on the mower before it will resume;
# worth saying once rather than repeating it in every message above.
ERROR_RESUME_HINT: Final = "press STOP on the mower, then MOW+OK to resume"

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


# --- Mow options (`partitionSetup`) -- two hex nibbles, 0xAB ------------------
# Captured live by diffing the plaintext s:mower command across three runs with
# each variable isolated (app's "riparti da zero" checkbox and "Personalizza la
# sequenza di falciatura" toggle):
#
#   A (high) = 1 continue (mow only the uncut area) | 2 restart (clear progress)
#   B (low)  = 1 automatic order (the robot picks the route)
#              2 custom sequence (mow the zones in the order given by partitionIds)
#
# So the zone ORDER in partitionIds is only honoured when B == 2; with B == 1 the
# robot ignores it and optimises its own route. (0x21 is the one combination not
# captured directly -- deduced from the scheme.)
MOW_SETUP_CONTINUE_AUTO: Final = 0x11  # 17 - continue, robot's own order
MOW_SETUP_CONTINUE: Final = 0x12  # 18 - continue, honour our zone order
MOW_SETUP_RESTART_AUTO: Final = 0x21  # 33 - restart, robot's own order (deduced)
MOW_SETUP_RESTART: Final = 0x22  # 34 - restart, honour our zone order


def mow_setup(*, reset: bool, ordered: bool) -> int:
    """`partitionSetup` for a mow command: restart-vs-continue + order mode.

    ``ordered=False`` lets the robot route itself (use it when the caller has no
    zone preference, e.g. "all zones"); ``ordered=True`` makes it follow the
    partitionIds sequence.
    """
    return (0x20 if reset else 0x10) | (0x02 if ordered else 0x01)


# --- Zone / partition encoding (proven; see Navimow_PrivateAPI_Catalogo §3.2)
def encode_partition_ids(region_ids: list[int]) -> str:
    """Encode region ids as concatenated little-endian uint16 hex.

    Proven: region id 1 -> "0100", 5 -> "0500", [1,5] -> "01000500".
    """
    out = []
    for rid in region_ids:
        out.append(f"{rid & 0xFF:02x}{(rid >> 8) & 0xFF:02x}")
    return "".join(out)


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
