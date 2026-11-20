"""Grounding verifier.

Both directions matter. A verifier that drops everything scores a perfect grounding
rate and is useless; a verifier that keeps everything is not a verifier.
"""

from __future__ import annotations

from datetime import date

from lab_explainer.ground import check_safety, check_statement
from lab_explainer.models import Flag, LabReport, Panel, Statement, TestResult


def _report() -> LabReport:
    return LabReport(
        report_id="R-T",
        panel=Panel.HBA1C,
        patient_age=50,
        patient_sex="M",
        collected_on=date(2026, 3, 1),
        results=[
            TestResult(
                test_id="hba1c.hba1c", test_name="HbA1c", value=7.8, unit="%",
                ref_low=4.0, ref_high=5.6, flag=Flag.HIGH,
            ),
            TestResult(
                test_id="thyroid.t3", test_name="Total T3", value=120.0, unit="ng/dL",
                ref_low=80.0, ref_high=200.0, flag=Flag.NORMAL,
            ),
        ],
    )


def test_statement_quoting_value_and_range_is_kept():
    s = Statement(
        text="Your HbA1c is 7.8%, above the printed range of 4.0 to 5.6%.",
        test_ids=["hba1c.hba1c"],
    )
    assert check_statement(s, _report()) is None


def test_digit_inside_a_test_name_is_not_treated_as_a_measurement():
    """The '1' in HbA1c and the '3' in Total T3 are parts of names.

    The first version of this check read them as unsourced numbers and dropped 12
    correct statements across the eval set.
    """
    s = Statement(
        text="Your HbA1c and Total T3 are within their printed ranges.",
        test_ids=["hba1c.hba1c", "thyroid.t3"],
    )
    assert check_statement(s, _report()) is None


def test_digit_inside_a_devanagari_word_is_not_a_measurement():
    s = Statement(text="आपका एचबीए1सी सामान्य सीमा के भीतर है।", test_ids=["hba1c.hba1c"])
    assert check_statement(s, _report()) is None


def test_hindi_statement_quoting_the_real_value_is_kept():
    s = Statement(
        text="आपका HbA1c 7.8% है, जो 4.0 से 5.6% की सीमा से अधिक है।",
        test_ids=["hba1c.hba1c"],
    )
    assert check_statement(s, _report()) is None


def test_unsourced_number_is_rejected():
    s = Statement(
        text="Your HbA1c is 7.8%, and levels above 9.0 need attention.",
        test_ids=["hba1c.hba1c"],
    )
    reason = check_statement(s, _report())
    assert reason and "9.0" in reason


def test_number_belonging_to_a_test_that_was_not_cited_is_rejected():
    """120.0 is on the report, but this sentence did not claim to be about T3."""
    s = Statement(text="Your HbA1c is 120.0.", test_ids=["hba1c.hba1c"])
    assert check_statement(s, _report()) is not None


def test_citing_a_test_not_on_the_report_is_rejected():
    s = Statement(text="Your Vitamin D is low.", test_ids=["vitd.vitamin_d"])
    reason = check_statement(s, _report())
    assert reason and "not on this report" in reason


def test_rounding_slack_is_allowed():
    s = Statement(text="Your HbA1c is 7.8%.", test_ids=["hba1c.hba1c"])
    assert check_statement(s, _report()) is None


def test_diagnosis_language_is_blocked():
    assert check_safety("Your HbA1c of 7.8% means you have diabetes.")


def test_prescription_language_is_blocked():
    assert check_safety("You should start metformin.")


def test_permitted_phrasing_is_not_blocked():
    assert not check_safety(
        "Your HbA1c is 7.8%, above the printed range of 4.0 to 5.6%. "
        "Results in this range are usually discussed with a doctor."
    )


# --- thousands separators ---------------------------------------------------------
#
# The model writes large counts the way people do: "18,000/uL". Reading that comma as a
# decimal point gave 18.0, which matched nothing, and dropped a correct sentence about a
# critically low platelet count. Found by the eval, not by a unit test.


def _platelet_report() -> LabReport:
    return LabReport(
        report_id="R-P",
        patient_age=58,
        patient_sex="M",
        collected_on=date(2026, 3, 1),
        results=[
            TestResult(
                test_id="cbc.platelets", test_name="Platelet Count", value=18000.0,
                unit="/uL", ref_low=150000.0, ref_high=410000.0, flag=Flag.CRITICAL,
            )
        ],
    )


def test_a_grouped_thousands_number_is_read_as_the_whole_value():
    s = Statement(
        text="Your platelet count is 18,000/uL. The normal range is 150,000 to 410,000/uL.",
        test_ids=["cbc.platelets"],
    )
    assert check_statement(s, _platelet_report()) is None


def test_the_same_sentence_in_hindi_is_read_the_same_way():
    s = Statement(
        text="आपका प्लेटलेट काउंट 18,000/µL है। सामान्य सीमा 150,000 से 410,000/µL है।",
        test_ids=["cbc.platelets"],
    )
    assert check_statement(s, _platelet_report()) is None


def test_an_ungrouped_comma_is_still_a_decimal_point():
    """"12,5" is how half the world writes 12.5. It must not become 125."""
    from lab_explainer.ground import _numbers

    assert _numbers("the value is 12,5 percent") == [12.5]


def test_a_wrong_grouped_number_is_still_rejected():
    s = Statement(text="Your platelet count is 81,000/uL.", test_ids=["cbc.platelets"])
    reason = check_statement(s, _platelet_report())
    assert reason and "81000" in reason.replace(".0", "")


# --- the deny-list has to read Hindi too ------------------------------------------
#
# The explanation is generated in Hindi, not translated from the English, and the
# deny-list was pure ASCII: every Hindi sentence passed the only safety check that runs
# in serving. The judge reads Hindi but runs only in the eval.


def test_hindi_diagnosis_is_blocked():
    assert check_safety("आपके परिणामों का मतलब है कि आपको मधुमेह है।")


def test_hindi_prescription_is_blocked():
    assert check_safety("आपको मेटफॉर्मिन लेना चाहिए और रोज एक गोली लें।")


def test_hindi_prognosis_is_blocked():
    assert check_safety("यह स्तर आपके लिए खतरनाक है।")


def test_hindi_english_pair_is_blocked_the_same_way():
    """The same claim in either language must not get different treatment."""
    assert check_safety("You have diabetes and should take metformin.")
    assert check_safety("आपको मधुमेह है और आपको मेटफॉर्मिन लेना चाहिए।")


def test_plain_hindi_result_sentence_is_not_blocked():
    assert not check_safety(
        "आपका हीमोग्लोबिन 11.2 g/dL है, जो मुद्रित सीमा 13.0 - 17.0 से नीचे है।"
    )


def test_the_hindi_lines_this_system_writes_itself_are_not_blocked():
    """A checker that blocks our own output blocks everything useful."""
    from lab_explainer.nextstep import URGENT_HI

    assert not check_safety(URGENT_HI)
    assert not check_safety("इन परिणामों को डॉक्टर को दिखाएं।")
    assert not check_safety("आपको अपने डॉक्टर से बात करनी है।")
