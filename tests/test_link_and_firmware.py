"""Wi-Fi vs 4G signal, the firmware probe and signed links in diagnostics (#33)."""
from types import SimpleNamespace

import pytest

from custom_components.navimow_pro.api.client import (
    NavimowAuthError,
    NavimowCloudClient,
    NavimowError,
)
from custom_components.navimow_pro.coordinator import signal_idle
from custom_components.navimow_pro.diagnostics import _scrub_text

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
