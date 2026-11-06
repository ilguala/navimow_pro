"""Wi-Fi vs 4G signal, firmware, the device lookup and what diagnostics hide (#33)."""
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro.api.client import (
    NavimowAuthError,
    NavimowCloudClient,
    NavimowError,
)
from custom_components.navimow_pro import coordinator as coord_mod
from custom_components.navimow_pro.const import CONF_DEVICE_ID, CONF_VEHICLE_SN, DOMAIN
from custom_components.navimow_pro.coordinator import firmware_version, signal_idle
from custom_components.navimow_pro.diagnostics import _redact, _scrub_text
from custom_components.navimow_pro.services import _device_entry_ids

# ---------------------------------------------------------------- signal


def test_wifi_at_zero_on_a_4g_mower_is_not_a_reading():
    # The X450 report: on 4G the whole time, Wi-Fi read 0 and looked like a fault.
    assert signal_idle(0, 18) is True


def test_the_link_in_use_keeps_its_value():
    assert signal_idle(18, 0) is False
    assert signal_idle(12, 20) is False


def test_both_at_zero_stays_visible():
    assert signal_idle(0, 0) is False


def test_nothing_known_about_the_other_link():
    assert signal_idle(0, None) is False
    assert signal_idle(None, 18) is False


# ---------------------------------------------------------------- firmware


def _client(answers):
    """A client whose call() answers per path, or raises what it is given."""
    asked = []

    def call(path, extra):
        asked.append((path, extra))
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return SimpleNamespace(call=call), asked


def test_both_paths_asked_with_the_mower():
    fake, asked = _client(
        {
            "/vehicle/firmware/get-new-firmware": {"has_new": 0},
            "/mowerbot/vehicle/firmware/get-firmware-list": [{"name": "ACU"}],
        }
    )
    out = NavimowCloudClient.firmware(fake, "SN1", 1)
    assert out == {"new_firmware": {"has_new": 0}, "firmware_list": [{"name": "ACU"}]}
    assert [extra for _, extra in asked] == [{"vehicle_sn": "SN1", "vehicle_type": 1}] * 2


def test_a_refused_path_is_recorded_and_the_other_still_asked():
    fake, _ = _client(
        {
            "/vehicle/firmware/get-new-firmware": NavimowError(340000, "fail"),
            "/mowerbot/vehicle/firmware/get-firmware-list": [{"name": "ACU"}],
        }
    )
    out = NavimowCloudClient.firmware(fake, "SN1", 1)
    assert out["new_firmware"] == {"error": "340000: fail"}
    assert out["firmware_list"] == [{"name": "ACU"}]


def test_an_auth_failure_is_not_swallowed():
    fake, _ = _client({"/vehicle/firmware/get-new-firmware": NavimowAuthError(401, "x")})
    with pytest.raises(NavimowAuthError):
        NavimowCloudClient.firmware(fake, "SN1", 1)


# ---------------------------------------------------------------- signed links


def test_a_signed_link_loses_its_query_and_keeps_its_path():
    url = (
        "https://bucket.s3.eu-central-1.amazonaws.com/ota/acu_1.0.5.bin"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAEXAMPLE%2F20261106"
        "&X-Amz-Signature=0123456789abcdef"
    )
    out = _scrub_text(url, frozenset())
    assert out.startswith("https://bucket.s3.eu-central-1.amazonaws.com/ota/acu_1.0.5.bin?")
    assert "AKIAEXAMPLE" not in out and "0123456789abcdef" not in out


def test_an_ordinary_link_is_left_alone():
    url = "https://example.com/img/mower.png?v=3"
    assert _scrub_text(url, frozenset()) == url


# ---------------------------------------------------------------- firmware version

# The X450's get-firmware-list, as pasted in #33 (part numbers left out there).
X450_FIRMWARE = {
    "firmwareList": [
        {"type": "sw", "version": "005D"},
        {"type": "ecu", "version": "0024"},
        {"type": "vcu", "version": "0017"},
        {"type": "swdp", "version": "0001"},
        {"type": "ble", "version": "0219"},
        {"type": "bsu", "version": "001D"},
    ],
    "osInfo": {"currentVersion": "4.3.0", "newVersion": "4.3.0"},
}


def test_the_main_firmware_reads_as_in_the_app():
    # The app shows the X450's ACU firmware as 1.0.5.D.
    assert firmware_version(X450_FIRMWARE) == "1.0.5.D"


def test_the_rule_matches_the_other_modules_too():
    # Same rule, checked against the app on the X450: ble 0219 -> 1.2.1.9.
    payload = {"firmwareList": [{"type": "sw", "version": "0219"}]}
    assert firmware_version(payload) == "1.2.1.9"


def test_a_version_in_another_shape_is_shown_as_it_came():
    payload = {"firmwareList": [{"type": "SW", "version": "2.3.0"}]}
    assert firmware_version(payload) == "2.3.0"


def test_no_main_firmware_means_no_version():
    assert firmware_version({"firmwareList": [{"type": "ble", "version": "0219"}]}) is None
    assert firmware_version({"firmwareList": [{"type": "sw", "version": ""}]}) is None
    assert firmware_version({}) is None
    assert firmware_version(None) is None
    assert firmware_version([]) is None


class _FirmwareClient:
    def __init__(self):
        self.asked = 0
        self.answer = X450_FIRMWARE

    def firmware_list(self, sn, vehicle_type):
        self.asked += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def mower(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])
    entry = SimpleNamespace(
        entry_id="e1", options={}, data={CONF_DEVICE_ID: "d", CONF_VEHICLE_SN: "SN1"}
    )
    co = coord_mod.NavimowCoordinator(SimpleNamespace(), entry)
    co.client = _FirmwareClient()
    co.hass = SimpleNamespace()  # the placeholder coordinator base sets none

    def later(seconds):
        clock["t"] += seconds
        co._maybe_fetch_firmware()

    return co, later


def test_read_at_startup_then_twice_a_day(mower):
    co, later = mower
    later(0)
    assert co.sw_version == "1.0.5.D" and co.client.asked == 1
    later(11 * 3600)
    assert co.client.asked == 1
    co.client.answer = {"firmwareList": [{"type": "sw", "version": "005E"}]}
    later(3600)
    assert co.client.asked == 2 and co.sw_version == "1.0.5.E"


def test_a_failed_read_keeps_the_version_and_tries_again_soon(mower):
    co, later = mower
    later(0)
    co.client.answer = NavimowError(500, "busy")
    later(12 * 3600)
    assert co.sw_version == "1.0.5.D" and co.client.asked == 2
    co.client.answer = X450_FIRMWARE
    later(1799)
    assert co.client.asked == 2
    later(1)
    assert co.client.asked == 3


def test_a_firmware_auth_failure_is_not_swallowed(mower):
    co, later = mower
    co.client.answer = NavimowAuthError(401, "x")
    with pytest.raises(NavimowAuthError):
        later(0)


class _Registry:
    def __init__(self, device):
        self.device = device
        self.updates = []

    def async_get_device(self, identifiers):
        assert identifiers == {(DOMAIN, "SN1")}
        return self.device

    def async_update_device(self, device_id, **changes):
        self.updates.append((device_id, changes))


def test_a_later_version_reaches_the_device_page(mower, monkeypatch):
    co, later = mower
    registry = _Registry(SimpleNamespace(id="dev1", sw_version=None))
    monkeypatch.setattr(coord_mod, "dr", SimpleNamespace(async_get=lambda hass: registry))
    co._sync_sw_version()
    assert registry.updates == []  # nothing read yet
    later(0)
    co._sync_sw_version()
    co._sync_sw_version()
    assert registry.updates == [("dev1", {"sw_version": "1.0.5.D"})]


def test_no_device_yet_is_left_to_the_entities(mower, monkeypatch):
    co, later = mower
    registry = _Registry(None)
    monkeypatch.setattr(coord_mod, "dr", SimpleNamespace(async_get=lambda hass: registry))
    later(0)
    co._sync_sw_version()
    assert registry.updates == [] and co._sw_version_applied is None


# ---------------------------------------------------------------- device lookup


def test_a_device_with_a_single_config_entry():
    # Home Assistant 2026.11 and later: config_entries is deprecated.
    class Device:
        config_entry_id = "e1"

        @property
        def config_entries(self):
            raise AssertionError("deprecated property read")

    assert _device_entry_ids(Device()) == {"e1"}


def test_a_device_on_an_older_home_assistant():
    assert _device_entry_ids(SimpleNamespace(config_entries={"e1", "e2"})) == {"e1", "e2"}


# ---------------------------------------------------------------- part numbers


def test_a_firmware_part_number_is_hidden():
    payload = {"firmwareList": [{"type": "bsu", "version": "001D", "pn": "P123456"}]}
    out = _redact(payload)
    assert out["firmwareList"][0]["pn"] == "**REDACTED**"
    assert out["firmwareList"][0]["version"] == "001D"
