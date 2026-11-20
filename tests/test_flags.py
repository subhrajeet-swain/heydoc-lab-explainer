from datetime import date

from lab_explainer.flags import compute_flag, flag_report
from lab_explainer.models import ExtractedReport, ExtractedTest, Flag, Panel

# cbc.hemoglobin: critical 7.0 / 20.0. Printed range used below: 13.0-17.0.


def _hgb(value: float, printed_flag: str | None = None) -> ExtractedTest:
    return ExtractedTest(
        test_id="cbc.hemoglobin", test_name="Hemoglobin", value=value, unit="g/dL",
        ref_low=13.0, ref_high=17.0, printed_flag=printed_flag,
    )


def _report(*results: ExtractedTest) -> ExtractedReport:
    return ExtractedReport(
        report_id="R1", panel=Panel.CBC, patient_age=40, patient_sex="F",
        collected_on=date(2026, 1, 1), results=list(results),
    )


def test_value_exactly_at_ref_high_is_normal():
    assert compute_flag(_hgb(17.0)) is Flag.NORMAL


def test_value_exactly_at_ref_low_is_normal():
    assert compute_flag(_hgb(13.0)) is Flag.NORMAL


def test_value_just_above_ref_high_is_high():
    assert compute_flag(_hgb(17.1)) is Flag.HIGH


def test_value_just_below_ref_low_is_low():
    assert compute_flag(_hgb(12.9)) is Flag.LOW


def test_value_exactly_at_critical_low_is_critical():
    assert compute_flag(_hgb(7.0)) is Flag.CRITICAL


def test_value_exactly_at_critical_high_is_critical():
    assert compute_flag(_hgb(20.0)) is Flag.CRITICAL


def test_critical_beats_high_when_value_is_beyond_critical_high():
    assert compute_flag(_hgb(21.0)) is Flag.CRITICAL


def test_fallback_marks_far_outside_range_critical_for_test_not_in_critical_json():
    # range 10-20, width 10: anything below 0 or above 30 is critical
    unknown = ExtractedTest(test_id="xyz.unknown", test_name="X", value=31.0, unit="u",
                            ref_low=10.0, ref_high=20.0)
    assert compute_flag(unknown) is Flag.CRITICAL


def test_fallback_does_not_fire_for_value_only_mildly_outside_range():
    unknown = ExtractedTest(test_id="xyz.unknown", test_name="X", value=25.0, unit="u",
                            ref_low=10.0, ref_high=20.0)
    assert compute_flag(unknown) is Flag.HIGH


def test_fallback_boundary_is_strictly_more_than_range_width():
    unknown = ExtractedTest(test_id="xyz.unknown", test_name="X", value=30.0, unit="u",
                            ref_low=10.0, ref_high=20.0)
    assert compute_flag(unknown) is Flag.HIGH


def test_test_with_no_printed_range_and_no_critical_entry_is_normal():
    unknown = ExtractedTest(test_id="xyz.unknown", test_name="X", value=5.0, unit="u")
    assert compute_flag(unknown) is Flag.NORMAL


def test_flag_report_warns_when_printed_flag_contradicts_computed_flag():
    _, warnings = flag_report(_report(_hgb(18.2, printed_flag="L")))
    assert warnings == [
        "cbc.hemoglobin: printed flag 'L' but value 18.2 is above range 13.0-17.0 (computed: high)"
    ]


def test_flag_report_is_silent_when_printed_flag_agrees_with_computed_flag():
    report, warnings = flag_report(_report(_hgb(18.2, printed_flag="H")))
    assert warnings == []
    assert report.results[0].flag is Flag.HIGH


def test_critically_low_value_printed_as_L_produces_no_warning():
    report, warnings = flag_report(_report(_hgb(6.5, printed_flag="L")))
    assert report.results[0].flag is Flag.CRITICAL
    assert warnings == []
