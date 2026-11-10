"""The app's mowing reports and the weather hold: what is read, when, and how."""
import datetime as dt
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN, STATE_DOCKED_CHARGING
from custom_components.navimow_pro.coordinator import (
    WEATHER_HOLD_STATES,
    NavimowError,
    _parse_reports,
    _parse_weather,
)

# ---------------------------------------------------------------- reports

TOTAL = {"totalMowingArea": "812.5", "totalMowingTime": "7.3", "activeTime": 1}
MONTH = {
    "mowingArea": {"totalArea": "301.2", "queryAreaData": {"xaxis": [1, 2], "yaxis": ["0", "0"]}},
    "mowingDuration": {"totalDuration": "2.6"},
    "updateMessage": "updated",
}


def test_the_reports_read_as_numbers():
    assert _parse_reports(TOTAL, MONTH) == {
        "mowed_total": 812.5, "hours_total": 7.3, "mowed_month": 301.2, "hours_month": 2.6,
    }


@pytest.mark.parametrize("month", [None, {}, {"mowingArea": "x", "mowingDuration": None}])
def test_a_missing_report_reads_as_nothing(month):
    parsed = _parse_reports(None, month)
    assert parsed == {"mowed_total": None, "hours_total": None, "mowed_month": None, "hours_month": None}


# ---------------------------------------------------------------- weather

CLEAR = {"rainState": 0, "snowState": 0, "stormState": 0, "frostState": 0,
         "highTemperatureState": 0, "rainLevel": 0}


def test_no_hold():
    assert _parse_weather(CLEAR) == {"state": "none", "active": [], "rain_level": 0}


def test_a_hold_is_named():
    assert _parse_weather(dict(CLEAR, stormState=1))["state"] == "wind"
    assert _parse_weather(dict(CLEAR, highTemperatureState="1"))["state"] == "heat"


def test_several_holds_name_the_first_and_list_all():
    parsed = _parse_weather(dict(CLEAR, frostState=1, rainState=1))
    assert parsed["state"] == "rain" and parsed["active"] == ["rain", "frost"]


def test_an_unknown_value_is_not_guessed():
    assert _parse_weather(dict(CLEAR, snowState=2))["state"] is None
    assert _parse_weather(dict(CLEAR, snowState=2, rainState=1))["state"] == "rain"


@pytest.mark.parametrize("payload", [None, {}, [], {"other": 1}])
def test_no_weather_answer(payload):
    assert _parse_weather(payload) is None


def test_every_state_is_an_option():
    names = {_parse_weather(dict(CLEAR, **{key: 1}))["state"] for _, key in coord_mod._WEATHER_FIELDS}
    assert names | {"none"} == set(WEATHER_HOLD_STATES)


# ---------------------------------------------------------------- when


class Client:
    def __init__(self):
        self.asked = []
        self.weather_answer = dict(CLEAR)
        self.fail = False

    def index2(self, sn):
        return {"vehicle_state": STATE_DOCKED_CHARGING, "soc": 80}

    def report_total(self, sn):
        self.asked.append("total")
        if self.fail:
            raise NavimowError(500, "busy")
        return TOTAL

    def report_month(self, sn, query_time):
        self.asked.append(("month", query_time))
        if self.fail:
            raise NavimowError(500, "busy")
        return MONTH

    def weather(self, sn):
        self.asked.append("weather")
        return self.weather_answer

    def __getattr__(self, name):
        return lambda *a, **k: {}


@pytest.fixture
def mower(monkeypatch):
    clock = {"t": 1000.0, "day": dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        coord_mod, "dt_util", SimpleNamespace(now=lambda: clock["day"], as_local=lambda t: t)
    )
    entry = SimpleNamespace(
        entry_id="e1", options={"mower_path": False},
        data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"},
    )
    co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
    co.client = Client()

    def poll(after=12.0):
        clock["t"] += after
        return co._fetch_blocking()

    co.poll, co.clock = poll, clock
    return co


def reads(co, what):
    return sum(1 for a in co.client.asked if a == what or (isinstance(a, tuple) and a[0] == what))


def test_the_reports_are_read_at_start_then_hourly(mower):
    snap = mower.poll()
    assert snap["reports"]["mowed_total"] == 812.5 and snap["reports"]["mowed_month"] == 301.2
    assert ("month", "2026-11-10") in mower.client.asked
    mower.poll(after=1800)
    assert reads(mower, "total") == 1
    mower.poll(after=1900)
    assert reads(mower, "total") == 2


def test_a_new_month_reads_them_at_once_and_drops_the_old_figure(mower):
    mower.poll()
    mower.client.fail = True
    mower.clock["day"] = dt.datetime(2026, 12, 1, 0, 5, tzinfo=dt.timezone.utc)
    snap = mower.poll()
    assert ("month", "2026-12-1") in mower.client.asked
    assert snap["reports"]["mowed_month"] is None  # November's figure is gone
    assert snap["reports"]["mowed_total"] == 812.5  # the total is still the total


def test_a_failed_read_is_tried_again_sooner(mower):
    mower.client.fail = True
    mower.poll()
    mower.client.fail = False
    mower.poll(after=700)
    assert reads(mower, "total") == 2


def test_the_weather_every_five_minutes_and_every_minute_during_a_hold(mower):
    mower.poll()
    mower.poll(after=200)
    assert reads(mower, "weather") == 1
    mower.client.weather_answer = dict(CLEAR, rainState=1)
    snap = mower.poll(after=150)
    assert reads(mower, "weather") == 2 and snap["weather"]["state"] == "rain"
    mower.poll(after=65)
    assert reads(mower, "weather") == 3
