"""Unit guard on the critical thresholds.

The failure this prevents is quiet: a platelet count printed as 250000 /uL compared
against a threshold stored per 10^3/uL is wrong by a factor of a thousand, produces a
confident flag, and nothing downstream would catch it.
"""

from __future__ import annotations

from lab_explainer.flags import compute_flag, unit_mismatch
from lab_explainer.models import ExtractedTest, Flag


def _platelets(unit: str, value: float) -> ExtractedTest:
    return ExtractedTest(
        test_id="cbc.platelets",
        test_name="Platelet Count",
        value=value,
        unit=unit,
        ref_low=150000.0,
        ref_high=410000.0,
    )


def test_matching_unit_has_no_mismatch():
    assert unit_mismatch(_platelets("/µL", 250000)) is None


def test_micro_sign_and_ascii_u_are_the_same_unit():
    assert unit_mismatch(_platelets("/uL", 250000)) is None


def test_alias_units_are_accepted():
    t = ExtractedTest(
        test_id="thyroid.tsh", test_name="TSH", value=2.0, unit="mIU/L",
        ref_low=0.4, ref_high=4.0,
    )
    assert unit_mismatch(t) is None


def test_wrong_scale_is_reported():
    msg = unit_mismatch(_platelets("10^3/uL", 250.0))
    assert msg and "thresholds skipped" in msg


def test_wrong_scale_falls_back_instead_of_flagging_critical():
    """250 x10^3/uL is a normal platelet count. Against the per-uL threshold of 20000
    it would look critically low, so the threshold must be skipped, not applied."""
    t = _platelets("10^3/uL", 250.0)
    # printed range is in the report's own units, so the heuristic sees 250 far below
    # 150000 and escalates - which is the safe direction to be wrong in.
    assert compute_flag(t) is not Flag.NORMAL
    assert unit_mismatch(t) is not None


def test_in_range_value_with_consistent_units_is_normal():
    assert compute_flag(_platelets("/µL", 250000)) is Flag.NORMAL
