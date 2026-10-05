"""The rain delay is reported as a two-digit hex string of quarter hours (#24).

Read as decimal first, 6 h ("18") showed as 4.5 h, and 8, 9 and 10 h as 5, 6
and 7.
"""
import pytest

from custom_components.navimow_pro.coordinator import _rain_delay_wire

SCALE = 4  # number.py: wire = hours * 4


@pytest.mark.parametrize("hours", range(1, 13))
def test_every_hour_reads_back_as_itself(hours):
    reported = f"{hours * SCALE:02X}"  # how the mower reports it, and how we write it
    assert _rain_delay_wire(reported) / SCALE == hours


@pytest.mark.parametrize(("reported", "wire"), [("18", 24), ("20", 32), ("0C", 12), ("0c", 12), (" 30 ", 48)])
def test_hex_strings(reported, wire):
    assert _rain_delay_wire(reported) == wire


def test_an_actual_number_is_taken_as_it_is():
    assert _rain_delay_wire(24) == 24


@pytest.mark.parametrize("bad", [None, "", "zz", True])
def test_nothing_usable_is_none(bad):
    assert _rain_delay_wire(bad) is None
