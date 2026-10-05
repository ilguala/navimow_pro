"""Which mowers get a zone mow without the custom-order bit (#16, #25).

On an H800 and an H1500 a zone mow with partitionSetup 0x02 (custom order) is
never answered and the mower stays put; the same zones with auto routing are
mowed. Other models keep the order.
"""
import pytest

from custom_components.navimow_pro.const import model_lacks, mow_setup


@pytest.mark.parametrize("model", ["H800", "H1500", "h1500"])
def test_first_generation_h_mowers_skip_the_order(model):
    assert model_lacks(model, "ordered_mow")


@pytest.mark.parametrize("model", ["i108", "X315", "H510 Pro", "H500", "H3000", "", None])
def test_other_mowers_keep_it(model):
    assert not model_lacks(model, "ordered_mow")


def test_the_two_setups_differ_only_in_the_order_bit():
    assert mow_setup(reset=True, ordered=False) == 0x21
    assert mow_setup(reset=True, ordered=True) == 0x22
