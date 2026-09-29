"""The mower event detector, fed with snapshot sequences.

Each snapshot is (state_code, mowing_progress, error) -- the three fields the
detector decides on. ``docked`` is derived with the integration's own
``is_docked``, as the coordinator does.
"""
from custom_components.navimow_pro.const import is_docked
from custom_components.navimow_pro.event_detector import (
    EVENT_ERROR,
    EVENT_MOWING_FINISHED,
    EVENT_MOWING_STARTED,
    EVENT_RETURNED_TO_DOCK,
    MowerEventDetector,
)

STARTED, RETURNED, FINISHED, ERROR = (
    EVENT_MOWING_STARTED,
    EVENT_RETURNED_TO_DOCK,
    EVENT_MOWING_FINISHED,
    EVENT_ERROR,
)


def snap(code, progress=None, error=False, **extra):
    return {
        "state_code": code,
        "docked": is_docked(code),
        "mowing_progress": progress,
        "error": error,
        **extra,
    }


def events(seq, detector=None):
    detector = detector or MowerEventDetector()
    out = []
    for item in seq:
        out += [event for event, _ in detector.update(snap(*item))]
    return out


# An i220 LiDAR Pro, 22-24 Sep 2026, as recorded by Home Assistant: the status,
# progress and problem sensors merged in order. Runs of progress-only updates
# while mowing are cut down to their last value.
REAL_DAYS = [
    ("0210", None, False),
    ("0210", 52, False),
    ("0220", 52, False),
    ("0102", 52, False),  # "Docked (finished)" at 52 % -- not finished
    ("0301", 52, False),  # fault raised while in the dock
    ("0301", 52, True),
    ("0102", 52, True),
    ("0202", 52, True),
    ("0202", 52, False),
    ("0102", 52, False),
    ("0210", 52, False),
    ("0210", 74, False),
    ("0220", 74, False),
    ("0103", 74, False),  # 0103 at 74 % -- not finished either
    ("0102", 74, False),
    ("0210", 74, False),
    ("0210", 79, False),
    ("0220", 79, False),
    ("0301", 79, False),  # fault on the way home
    ("0301", 79, True),
    ("0220", 79, True),
    ("0220", 79, False),
    ("0102", 79, False),
    ("0101", 79, False),
    ("0103", 79, False),
    ("0255", 79, False),  # unmapped 02xx codes flickering with Charging
    ("0255", 94, False),
    ("0202", 94, False),
    ("0204", 94, False),
    ("0202", 94, False),
    ("0204", 94, False),
    ("0202", 94, False),
    ("0206", 94, False),
    ("0202", 94, False),
    ("0210", 94, False),  # new job; progress still the old job's for one poll
    ("0210", 81, False),
    ("0220", 81, False),
    ("0102", 81, False),
    ("0210", 81, False),
    ("0210", 82, False),
    ("0220", 82, False),
    ("0210", 82, False),  # turned back out before reaching the dock
    ("0210", 99, False),
]

REAL_DAYS_EVENTS = [
    RETURNED,
    ERROR,
    STARTED,
    RETURNED,
    STARTED,
    ERROR,
    RETURNED,
    STARTED,
    RETURNED,
    STARTED,
]


def test_real_days():
    assert events(REAL_DAYS) == REAL_DAYS_EVENTS


def test_real_days_with_the_finish():
    # The recording stops at 99 % because the integration was not loaded when
    # the job ended; afterwards it read 0103 at 100 %. This is that ending.
    tail = [("0220", 100, False), ("0103", 100, False)]
    assert events(REAL_DAYS + tail) == REAL_DAYS_EVENTS + [RETURNED, FINISHED]


def test_first_snapshot_only_primes():
    assert events([("0103", 100, False)]) == []
    assert events([("0301", 10, True)]) == []
    assert events([("0210", 100, False)]) == []


def test_a_whole_job():
    seq = [
        ("0101", 0, False),
        ("0210", 10, False),
        ("0210", 100, False),
        ("0220", 100, False),
        ("0102", 100, False),
    ]
    assert events(seq) == [STARTED, RETURNED, FINISHED]


def test_100_percent_reported_after_arrival():
    seq = [
        ("0101", 0, False),
        ("0210", 99, False),
        ("0220", 99, False),
        ("0102", 99, False),
        ("0102", 100, False),
        ("0103", 100, False),
    ]
    assert events(seq) == [STARTED, RETURNED, FINISHED]


def test_charging_midway_finishes_once():
    seq = [
        ("0101", 0, False),
        ("0210", 10, False),
        ("0220", 60, False),
        ("0202", 60, False),
        ("0210", 60, False),
        ("0210", 100, False),
        ("0220", 100, False),
        ("0202", 100, False),
    ]
    assert events(seq) == [STARTED, RETURNED, STARTED, RETURNED, FINISHED]


def test_docked_finished_code_below_100_is_not_finished():
    seq = [("0101", 0, False), ("0210", 30, False), ("0220", 52, False), ("0102", 52, False)]
    assert FINISHED not in events(seq)


def test_stale_100_from_the_last_job_is_not_finished():
    seq = [
        ("0101", 100, False),
        ("0210", 100, False),
        ("0210", 3, False),
        ("0220", 40, False),
        ("0102", 40, False),
    ]
    assert events(seq) == [STARTED, RETURNED]


def test_no_second_finish_while_it_stays_docked():
    seq = [
        ("0210", 90, False),
        ("0220", 100, False),
        ("0102", 100, False),
        ("0202", 100, False),
        ("0103", 100, False),
        ("0101", 100, False),
    ]
    assert events(seq) == [RETURNED, FINISHED]


def test_flicker_in_the_dock_is_not_a_trip():
    seq = [("0202", 50, False)] + [("0204", 50, False), ("0202", 50, False)] * 3
    assert events(seq) == []


def test_resuming_on_the_lawn_is_not_a_start():
    seq = [("0210", 80, False), ("0220", 82, False), ("0210", 82, False)]
    assert events(seq) == []


def test_fault_in_the_dock_is_not_a_return():
    seq = [("0102", 50, False), ("0301", 50, True), ("0102", 50, True), ("0102", 50, False)]
    assert events(seq) == [ERROR]


def test_error_fires_once_per_fault():
    seq = [
        ("0210", 10, False),
        ("0301", 10, True),
        ("0301", 10, True),
        ("0210", 10, False),
        ("0301", 10, True),
    ]
    assert events(seq) == [ERROR, ERROR]


def test_attributes():
    detector = MowerEventDetector()
    detector.update(snap("0101", 0))
    (event, attrs), = detector.update(
        snap("0210", 5, state="Mowing", current_zone="Front lawn", battery=97)
    )
    assert event == STARTED
    assert attrs == {
        "state": "Mowing",
        "state_code": "0210",
        "mowing_progress": 5,
        "current_zone": "Front lawn",
        "battery": 97,
    }
    (event, attrs), = detector.update(
        snap("0301", 5, True, error_text="Wheel stuck", error_codes=["1234"])
    )
    assert event == ERROR
    assert attrs["error_text"] == "Wheel stuck"
    assert attrs["error_codes"] == ["1234"]


def test_empty_snapshot_is_ignored():
    detector = MowerEventDetector()
    assert detector.update({}) == []
    assert detector.update({"state_code": ""}) == []
    detector.update(snap("0101", 0))
    assert detector.update({}) == []
    assert [e for e, _ in detector.update(snap("0210", 1))] == [STARTED]
