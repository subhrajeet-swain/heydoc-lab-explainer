"""Shapes that real lab reports take, and the synthetic ones never did.

All of this came from one downloaded CBC report that the first version rejected
outright. It had read every blood value correctly and then discarded the lot, because
the sample template prints its year as `202X` and carries no report number.

The rule the whole file encodes: strict on the five fields the extraction schema is
about — test, value, unit, reference range, flag — and permissive on the letterhead,
which nothing downstream reads.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lab_explainer.flags import compute_flag, flag_report, unit_mismatch
from lab_explainer.models import ExtractedReport, ExtractedTest, Flag, Panel

CBC_ROW = {
    "test_id": "cbc.hemoglobin", "test_name": "Hemoglobin (Hb)", "value": 12.5,
    "unit": "g/dL", "ref_low": 13.0, "ref_high": 17.0, "printed_flag": "Low",
}


# --- the letterhead must never reject a report -----------------------------------


def test_a_report_with_no_id_and_no_date_still_parses():
    r = ExtractedReport(results=[ExtractedTest(**CBC_ROW)])
    assert r.report_id is None
    assert r.collected_on is None
    assert len(r.results) == 1


def test_a_redacted_date_is_null_not_a_rejection():
    """The sample report prints `202X`. Null is the only honest reading of that."""
    r = ExtractedReport(results=[ExtractedTest(**CBC_ROW)], collected_on=None)
    assert r.collected_on is None


def test_panel_is_derived_from_the_test_ids_not_extracted():
    r = ExtractedReport(results=[ExtractedTest(**CBC_ROW)])
    assert r.panel is Panel.CBC


def test_an_unexpected_extra_key_does_not_discard_the_report():
    r = ExtractedReport(results=[ExtractedTest(**CBC_ROW)], lab_name="Smart Pathology")
    assert len(r.results) == 1


# --- the clinical five stay strict ------------------------------------------------


def test_a_report_with_no_results_is_still_rejected():
    with pytest.raises(ValidationError):
        ExtractedReport(results=[])


def test_a_missing_value_is_still_rejected_never_defaulted():
    row = {k: v for k, v in CBC_ROW.items() if k != "value"}
    with pytest.raises(ValidationError):
        ExtractedTest(**row)


def test_a_non_numeric_value_is_still_rejected():
    with pytest.raises(ValidationError):
        ExtractedTest(**{**CBC_ROW, "value": "twelve point five"})


# --- Indian lab units -------------------------------------------------------------


@pytest.mark.parametrize("unit", ["cumm", "cu mm", "/cumm", "cmm", "cells/cumm"])
def test_cubic_millimetre_is_microlitre(unit):
    t = ExtractedTest(test_id="cbc.platelets", test_name="Platelet Count",
                      value=150000.0, unit=unit, ref_low=150000.0, ref_high=410000.0)
    assert unit_mismatch(t) is None


def test_millions_per_cubic_millimetre_is_millions_per_microlitre():
    t = ExtractedTest(test_id="cbc.rbc", test_name="Total RBC count", value=5.2,
                      unit="mill/cumm", ref_low=4.5, ref_high=5.5)
    assert unit_mismatch(t) is None


def test_thousands_per_cubic_millimetre_is_still_a_mismatch():
    """A different scale. It must keep failing rather than compare across scales."""
    t = ExtractedTest(test_id="cbc.platelets", test_name="Platelet Count", value=150.0,
                      unit="thou/cumm", ref_low=150.0, ref_high=410.0)
    assert unit_mismatch(t) is not None


def test_a_critically_low_platelet_count_in_local_units_still_escalates():
    t = ExtractedTest(test_id="cbc.platelets", test_name="Platelet Count", value=15000.0,
                      unit="cumm", ref_low=150000.0, ref_high=410000.0)
    assert compute_flag(t) is Flag.CRITICAL


# --- flags printed as words -------------------------------------------------------


@pytest.mark.parametrize("printed,value", [("Low", 12.5), ("High", 18.0), ("Normal", 15.0)])
def test_word_flags_agree_with_the_computed_flag_and_raise_no_warning(printed, value):
    r = ExtractedReport(results=[ExtractedTest(**{**CBC_ROW, "value": value,
                                                  "printed_flag": printed})])
    _, warnings = flag_report(r)
    assert warnings == []


def test_a_word_flag_that_contradicts_the_value_is_still_caught():
    r = ExtractedReport(results=[ExtractedTest(**{**CBC_ROW, "value": 18.0,
                                                  "printed_flag": "Low"})])
    _, warnings = flag_report(r)
    assert warnings and "printed flag" in warnings[0]


def test_an_unrecognised_printed_flag_warns_rather_than_being_assumed_normal():
    """One real report printed 'Borderline'. Saying nothing would be the bug."""
    r = ExtractedReport(results=[ExtractedTest(**{**CBC_ROW, "value": 15.0,
                                                  "printed_flag": "Borderline"})])
    _, warnings = flag_report(r)
    assert warnings and "unrecognised printed flag" in warnings[0]
