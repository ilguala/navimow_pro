"""Motion commands wait for the mower to confirm them (#16, #17).

/vehicle/set/send only says the cloud queued a command. The mower's own answer
is on /vehicle/set/response: 0 queued, 2 delivered and waiting, 3 answered.
"""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.api import NavimowAuthError, NavimowError
from custom_components.navimow_pro.api.client import NavimowCommandError
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN


class Client:
    def __init__(self, statuses, cmd_num="c1"):
        self.statuses = list(statuses)
        self.cmd_num = cmd_num
        self.probes = 0

    def mow(self, sn):
        return {"cmd_num": self.cmd_num} if self.cmd_num else {}

    def command_status(self, sn, cmd_num):
        self.probes += 1
        nxt = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    cmd_num_of = staticmethod(coord_mod.NavimowCloudClient.cmd_num_of)
    resp_data_of = staticmethod(coord_mod.NavimowCloudClient.resp_data_of)


@pytest.fixture
def make(monkeypatch):
    clock = {"t": 0.0}

    async def sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(coord_mod.asyncio, "sleep", sleep)

    def build(statuses, cmd_num="c1"):
        entry = SimpleNamespace(
            entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
        )
        co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)

        async def run(func, *args):
            return func(*args)

        co.hass = SimpleNamespace(
            loop=SimpleNamespace(time=lambda: clock["t"]), async_add_executor_job=run
        )
        co.client = Client(statuses, cmd_num)
        co._persist_session = lambda: None

        async def refresh():
            co.refreshed = True

        co.async_request_refresh = refresh
        co.refreshed = False
        return co, clock

    return build


def send(co):
    return asyncio.run(co.async_send_confirmed(co.client.mow, "SN1"))


def test_an_answer_is_success(make):
    co, _ = make([{"status": 2}, {"status": 3, "respData": "{}"}])
    assert send(co)["status"] == 3
    assert co.refreshed


@pytest.mark.parametrize("stuck", [0, 2])
def test_no_answer_says_it_may_still_happen(make, stuck):
    co, clock = make([{"status": stuck}])
    with pytest.raises(NavimowCommandError) as err:
        send(co)
    assert "may still carry it out" in err.value.desc
    assert 8 <= clock["t"] <= 9  # gave up at the deadline, not before
    assert co.refreshed  # the state is re-read either way


@pytest.mark.parametrize("unknown", [{}, {"status": 5}, {"other": 1}])
def test_a_status_never_measured_is_not_called_a_failure(make, unknown):
    co, _ = make([unknown])
    send(co)  # no error: it proves nothing either way


def test_a_failing_probe_does_not_stop_the_wait(make):
    co, _ = make([NavimowError("network", "x"), {"status": 3}])
    assert send(co)["status"] == 3
    assert co.client.probes == 2


def test_only_failing_probes_prove_nothing(make):
    co, _ = make([NavimowError("network", "x")])
    send(co)  # no error


def test_a_dead_session_is_not_swallowed(make):
    co, _ = make([NavimowAuthError(401903, "expired")])
    with pytest.raises(NavimowAuthError):
        send(co)


def test_no_command_id_means_nothing_to_wait_for(make):
    co, _ = make([{"status": 2}], cmd_num="")
    send(co)
    assert co.client.probes == 0
