"""The app's message feed (#28): what is read from it, and when it is read."""
import datetime as dt
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.const import (
    CONF_DEVICE_ID,
    CONF_VEHICLE_SN,
    SLOW_REFRESH_EVERY,
    STATE_DOCKED_CHARGING,
    STATE_MOWING,
    STATE_RETURNING,
)
from custom_components.navimow_pro.coordinator import _parse_coverage, _parse_messages


def msg(title=None, content=None, addtime=None, code=None):
    item = {"message_id": "1"}
    if title is not None:
        item["title"] = title
    if content is not None:
        item["content"] = content
    if addtime is not None:
        item["addtime"] = addtime
    if code is not None:
        item["vendor_code"] = code
    return item


def feed(*items):
    return {"vehicle_message_list": list(items), "has_history_message": False}


# ---------------------------------------------------------------- reading


def test_no_answer_and_an_empty_answer_are_told_apart():
    assert _parse_messages(None) is None
    assert _parse_messages({}) is None
    assert _parse_messages(feed()) == []


def test_newest_first_with_its_time():
    out = _parse_messages(
        feed(msg("Old", addtime=1_700_000_000), msg("New", addtime=1_700_000_600_000))  # s and ms
    )
    assert [m["text"] for m in out] == ["New", "Old"]
    assert out[0]["time"] == dt.datetime.fromtimestamp(1_700_000_600, dt.timezone.utc).isoformat()


def test_an_unfilled_title_gives_way_to_the_body():
    [m] = _parse_messages(feed(msg("Completed on %@", "Mowing motor undervoltage", 1, "1003")))
    assert m["text"] == "Mowing motor undervoltage"
    assert m["title"] is None and m["code"] == "1003"


def test_the_code_is_the_last_resort():
    [m] = _parse_messages(feed(msg(code="2102", addtime=1)))
    assert m["text"] == "Message 2102"


def test_kept_short_and_few():
    out = _parse_messages(feed(*[msg("x" * 400, addtime=i) for i in range(15)]))
    assert len(out) == 10
    assert all(len(m["text"]) == 255 for m in out)


def test_the_end_code_is_passed_on_raw():
    cov = _parse_coverage(
        [
            {"partitionId": 1, "area": 10, "finishedArea": 5, "partitionPercentage": 50,
             "endTime": 100, "endTimeAlias": 3},
            {"partitionId": 2, "area": 10, "finishedArea": 2, "partitionPercentage": 22,
             "endTime": 200, "endTimeAlias": 8},
            {"partitionId": 3, "area": 10, "finishedArea": 0, "partitionPercentage": 0,
             "endTime": 300, "endTimeAlias": -1},
        ],
        {},
    )
    assert cov["last_end_code"] == 8  # the latest real end; -1 is "no end"
    assert [z["end_code"] for z in cov["zones"]] == [3, 8, -1]


# ---------------------------------------------------------------- when


class Client:
    def __init__(self):
        self.state = STATE_DOCKED_CHARGING
        self.asked = 0

    def index2(self, sn):
        return {"vehicle_state": self.state, "soc": 80}

    def messages(self, sn):
        self.asked += 1
        return feed(msg("Returning: battery low", addtime=1))

    def __getattr__(self, name):  # every other read answers with nothing
        return lambda *a, **k: {}


@pytest.fixture
def mower(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        coord_mod,
        "dt_util",
        SimpleNamespace(now=lambda: dt.datetime.now(dt.timezone.utc), as_local=lambda t: t),
    )
    entry = SimpleNamespace(
        entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
    )
    co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
    co.client = Client()

    def poll(state=None, after=12.0):
        clock["t"] += after
        if state:
            co.client.state = state
        return co._fetch_blocking()

    co.poll = poll
    return co


def test_read_on_the_first_poll(mower):
    snap = mower.poll()
    assert mower.client.asked == 1
    assert snap["messages"][0]["text"] == "Returning: battery low"


def test_not_on_every_poll(mower):
    mower.poll()
    for _ in range(SLOW_REFRESH_EVERY - 2):
        mower.poll()
    assert mower.client.asked == 1


def test_read_again_after_the_mower_heads_home(mower):
    mower.poll(STATE_MOWING)
    asked = mower.client.asked
    mower.poll(STATE_RETURNING, after=40)
    assert mower.client.asked == asked + 1
    mower.poll(after=5)  # too soon after the last read
    assert mower.client.asked == asked + 1
    mower.poll(after=40)  # still within the follow-up window
    assert mower.client.asked == asked + 2


def test_the_follow_up_window_ends(mower):
    mower.poll(STATE_MOWING)
    mower.poll(STATE_RETURNING, after=40)
    asked = mower.client.asked
    mower._cycle = 2  # keep the slow cycle out of the way
    mower.poll(after=400)
    assert mower.client.asked == asked


def test_a_feed_that_fails_does_not_break_the_poll(mower):
    def broken(sn):
        raise coord_mod.NavimowError("x", "not supported")

    mower.client.messages = broken
    snap = mower.poll()
    assert snap["messages"] is None
