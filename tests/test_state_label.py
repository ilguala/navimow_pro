"""The status after a return: finished, or back halfway to charge (same code)."""
import pytest

from custom_components.navimow_pro.coordinator import _state_label


@pytest.mark.parametrize(
    ("progress", "battery", "limit", "label"),
    [
        (100, 60, 90, "Docked (finished)"),
        (76, 9, 90, "Charging (job unfinished)"),  # seen live: back at 10 %, 76 %
        (76, 90, 90, "Docked (job unfinished)"),  # charged, waiting to resume
        (76, 95, None, "Charging (job unfinished)"),  # no limit known: up to 100 %
        (76, None, 90, "Docked (job unfinished)"),
        (None, 50, 90, "Docked"),
    ],
)
def test_back_in_the_dock_after_a_trip(progress, battery, limit, label):
    assert _state_label("0102", progress, battery, limit) == label


def test_other_codes_keep_their_label():
    assert _state_label("0101", 50, 10, 90) == "Docked"
    assert _state_label("0210") == "Mowing"
    assert _state_label("0310").startswith("Stopped")
    assert _state_label("") == "Unknown"
