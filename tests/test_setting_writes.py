"""Setting stages, wire types and eventual readback (#31), without a mower."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro import switch as switch_mod
from custom_components.navimow_pro.api import NavimowError
from custom_components.navimow_pro.const import (
    CONF_DEVICE_ID,
    CONF_VEHICLE_SN,
    SETTING_CONFIRM_S,
)
from custom_components.navimow_pro.entity import NavimowEntity
from custom_components.navimow_pro.switch import NavimowSwitch, SWITCHES


def make_coordinator():
    entry = SimpleNamespace(
        entry_id="e1", options={},
        data={CONF_DEVICE_ID: "test", CONF_VEHICLE_SN: "test"},
    )
    co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
    co.calls = []
    co.data = {"settings": {"child_lock": False}}
    co.last_update_success = True

    async def run(func, *args):
        return func(*args)

    async def refresh():
        co.calls.append("refresh")

    co.hass = SimpleNamespace(
        async_add_executor_job=run,
        async_create_background_task=lambda coro, name: asyncio.create_task(coro),
    )
    co._persist_session = lambda: None
    co.async_request_refresh = refresh
    co.client.command_status = lambda *args: {"status": 3}
    return co


class Timers:
    """async_call_later stand-in: records the delay, fires on demand."""

    def __init__(self):
        self.pending = []

    def __call__(self, hass, delay, action):
        entry = [delay, action]
        self.pending.append(entry)
        return lambda: self.pending.remove(entry)

    def fire(self):
        for delay, action in list(self.pending):
            self.pending.remove([delay, action])
            action(None)


@pytest.fixture
def timers(monkeypatch):
    timers = Timers()
    monkeypatch.setattr(switch_mod, "async_call_later", timers)
    # The placeholder HA base class has no coordinator hook to chain to.
    monkeypatch.setattr(
        NavimowEntity, "_handle_coordinator_update",
        lambda self: self.async_write_ha_state(), raising=False,
    )
    return timers


def make_switch(co, key):
    entity = NavimowSwitch(co, next(d for d in SWITCHES if d.key == key))
    entity.coordinator = co  # the lightweight HA base class stub stores nothing
    entity.hass = co.hass
    entity.states = []
    entity.async_write_ha_state = lambda: entity.states.append(entity.is_on)
    return entity


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("key,wire_key,robot_type,cloud_type,iot", [
    ("child_lock", "childLock", int, str, True),
    ("lift_alarm", "liftSwitch", int, str, True),
    ("mowing_cycle", "mowingCycle", str, str, True),
    ("rain_sensor", "rainSensor", int, str, False),
    ("rain_detection", "rainDetectionSwitch", int, str, False),
])
def test_switch_wire_types_and_one_refresh(
    timers, on, key, wire_key, robot_type, cloud_type, iot
):
    async def scenario():
        co = make_coordinator()

        def call(path, data):
            co.calls.append((path, data))
            return {"cmd_num": "private-command-id"}

        co.client.call = call
        entity = make_switch(co, key)
        await entity._write(on)
        assert len(co.calls) == 3
        device, cloud, refresh = co.calls
        assert device[0] == "/vehicle/set/send"
        assert device[1]["cmdCode"] == "s:mower"
        device_value = json.loads(device[1]["data"])[wire_key]
        assert type(device_value) is robot_type
        assert str(device_value) == str(int(on))
        assert cloud[0] == "/vehicle/set/save-set-data"
        cloud_value = cloud[1]["data"][wire_key]
        assert type(cloud_value) is cloud_type
        assert cloud_value == (str(int(on)) if iot else f"0{int(on)}")
        assert cloud[1].get("operation_type") == ("iot_set" if iot else None)
        assert refresh == "refresh"
        # A submitted command is not an observed state: once the hold runs out
        # the switch shows what the mower reports.
        if key == "child_lock":
            timers.fire()
            assert entity.is_on is False
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_switch_moves_at_once_and_holds_until_reported(timers):
    async def scenario():
        co = make_coordinator()
        co.client.call = lambda path, data: co.calls.append(path) or {}
        entity = make_switch(co, "child_lock")
        await entity._write(True)
        # Shown before anything was sent, kept while the cloud lags behind.
        assert entity.states[0] is True
        assert entity.is_on is True
        assert timers.pending[0][0] == SETTING_CONFIRM_S
        entity._handle_coordinator_update()
        assert entity.is_on is True and timers.pending
        co.data["settings"]["child_lock"] = True
        entity._handle_coordinator_update()
        assert entity._shown is None and not timers.pending
        assert entity.is_on is True
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_never_reported_goes_back_to_what_the_mower_has(timers):
    async def scenario():
        co = make_coordinator()
        co.client.call = lambda path, data: {}
        entity = make_switch(co, "child_lock")
        await entity._write(True)
        timers.fire()
        assert entity.is_on is False
        assert entity.states[-1] is False
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_refused_write_puts_the_switch_back(timers):
    async def scenario():
        co = make_coordinator()

        def call(path, data):
            raise NavimowError("test", "request failed")

        co.client.call = call
        entity = make_switch(co, "child_lock")
        with pytest.raises(NavimowError):
            await entity._write(True)
        assert entity.states == [True, False]
        assert entity._shown is None and not timers.pending
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_write_only_switch_keeps_the_commanded_value(timers):
    async def scenario():
        co = make_coordinator()
        co.client.call = lambda path, data: {}
        desc = next(d for d in SWITCHES if d.assumed)
        entity = make_switch(co, desc.key)
        assert entity._attr_assumed_state
        await entity._write(True)
        assert entity._shown is None and not timers.pending
        assert entity.is_on is True
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_stage", [0, 1])
def test_stage_failure_stops_write_and_records_partial_outcome(fail_stage):
    async def scenario():
        co = make_coordinator()

        def stage(index):
            co.calls.append(index)
            if index == fail_stage:
                raise NavimowError("test", "request failed")
            return {"cmd_num": "private-command-id"}

        with pytest.raises(NavimowError):
            await co.async_write_setting("child_lock", True, [(stage, 0), (stage, 1)])
        assert co.calls == list(range(fail_stage + 1))
        assert co.setting_writes["child_lock"]["completed_stages"] == fail_stage
        assert co.setting_writes["child_lock"]["submission"] == (
            "partial_failure" if fail_stage else "failed"
        )
        assert "child_lock" in co._setting_checks  # read back, never resubmit
        assert co._force_slow
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


@pytest.mark.parametrize("fresh,success,value,expected", [
    (True, True, True, "matched"),
    (True, True, False, "mismatch"),
    (True, True, None, "unavailable"),
    (False, True, True, "unavailable"),  # cached desired value proves nothing
    (True, False, True, "unavailable"),
])
def test_readback_requires_fresh_successful_settings(monkeypatch, fresh, success, value, expected):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(coord_mod.asyncio, "sleep", sleep)

    async def scenario():
        co = make_coordinator()
        co.data["settings"]["child_lock"] = value
        co.last_update_success = success

        async def refresh():
            co._settings_read_success = fresh
            if fresh:
                co._settings_read_generation += 1

        co.async_refresh_settings = refresh
        record = {"requested": True}
        await co._async_check_setting("child_lock", record, "private-command-id")
        assert record["readback"] == expected
        assert record["command_status"] == 3  # an answer alone is not confirmation
        assert record["check_after_seconds"] == 75
        assert "private-command-id" not in str(record)
        assert sleeps == [15, 60]

    asyncio.run(scenario())


def test_new_write_cancels_old_check_and_unload_cancels_remaining():
    async def scenario():
        co = make_coordinator()
        await co.async_write_setting("child_lock", True, [(lambda: {},)])
        old = co._setting_checks["child_lock"]
        await co.async_write_setting("child_lock", False, [(lambda: {},)])
        new = co._setting_checks["child_lock"]
        assert old is not new
        await co.async_cancel_setting_checks()
        await asyncio.gather(old, return_exceptions=True)
        assert old.cancelled() and new.cancelled()
        assert co._setting_checks == {}
        assert co.setting_writes["child_lock"]["requested"] is False

    asyncio.run(scenario())


def test_concurrent_writes_wait_for_each_complete_batch():
    async def scenario():
        co = make_coordinator()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run(func, *args):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return func(*args)

        def stage(label):
            co.calls.append(label)
            return {}

        co.hass.async_add_executor_job = run
        first = asyncio.create_task(co.async_write_setting(
            "child_lock", True, [(stage, "device1"), (stage, "cloud1")]
        ))
        await entered.wait()
        second = asyncio.create_task(co.async_write_setting(
            "lift_alarm", True, [(stage, "device2"), (stage, "cloud2")]
        ))
        release.set()
        await asyncio.gather(first, second)
        assert co.calls == ["device1", "cloud1", "refresh", "device2", "cloud2", "refresh"]
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_poll_waits_for_both_write_stages():
    async def scenario():
        co = make_coordinator()
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(func, *args):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return func(*args)

        def stage(label):
            co.calls.append(label)
            return {}

        def poll():
            co.calls.append("poll")
            raise NavimowError("test", "end test before snapshot processing")

        co.hass.async_add_executor_job = run
        co._fetch_blocking = poll
        write = asyncio.create_task(co.async_write_setting(
            "child_lock", True, [(stage, "device"), (stage, "cloud")]
        ))
        await entered.wait()
        polling = asyncio.create_task(co._async_update_data())
        release.set()
        await write
        with pytest.raises(coord_mod.UpdateFailed):
            await polling
        assert co.calls == ["device", "cloud", "refresh", "poll"]
        await co.async_cancel_setting_checks()

    asyncio.run(scenario())


def test_failed_settings_endpoint_keeps_cache_but_is_not_fresh():
    class Client:
        fail = False

        def set_list(self, sn):
            if self.fail:
                raise NavimowError("test", "unavailable")
            return {"childLock": "1"}

        def __getattr__(self, name):
            return lambda *args: {}

    co = make_coordinator()
    co.client = Client()
    co._maybe_fetch_map = lambda raw: None
    co._parse = lambda raw: raw
    co._fetch_blocking()
    generation = co._settings_read_generation
    assert co._settings_read_success
    co.client.fail = True
    co._force_slow = True
    co._fetch_blocking()
    assert co._raw_cache["set_list"] == {"childLock": "1"}
    assert not co._settings_read_success
    assert co._settings_read_generation == generation


@pytest.mark.parametrize("status", [0, 2, 3, 4, 5, 9, "3", None])
def test_command_status_does_not_override_readback(monkeypatch, status):
    async def sleep(delay):
        pass

    monkeypatch.setattr(coord_mod.asyncio, "sleep", sleep)

    async def scenario():
        co = make_coordinator()
        co.client.command_status = lambda *args: {"status": status}

        async def refresh():
            co._settings_read_generation += 1
            co._settings_read_success = True

        co.async_refresh_settings = refresh
        record = {"requested": True}
        await co._async_check_setting("child_lock", record, "private-command-id")
        assert record["readback"] == "mismatch"
        assert record["command_status"] == (int(status) if status is not None else "unknown")

    asyncio.run(scenario())
