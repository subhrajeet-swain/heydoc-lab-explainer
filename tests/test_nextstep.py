"""The escalation rules. These are the ones that must never regress silently."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from lab_explainer.models import (
    ExplainedReport,
    Flag,
    LabReport,
    Meta,
    NextStep,
    Panel,
    TestResult,
)
from lab_explainer.nextstep import decide


def _report(*flags: Flag) -> LabReport:
    return LabReport(
        report_id="R-TEST",
        panel=Panel.CBC,
        patient_age=40,
        patient_sex="M",
        collected_on=date(2026, 3, 1),
        results=[
            TestResult(
                test_id=f"cbc.t{i}",
                test_name=f"Test {i}",
                value=10.0,
                unit="g/dL",
                ref_low=5.0,
                ref_high=15.0,
                flag=f,
            )
            for i, f in enumerate(flags)
        ],
    )


def test_all_normal_means_no_action():
    step, urgent, msg = decide(_report(Flag.NORMAL, Flag.NORMAL))
    assert step is NextStep.NO_ACTION
    assert urgent is False
    assert msg is None


def test_any_high_means_follow_up_package():
    step, urgent, _ = decide(_report(Flag.NORMAL, Flag.HIGH))
    assert step is NextStep.FOLLOW_UP_PACKAGE
    assert urgent is False


def test_any_low_means_follow_up_package():
    step, _, _ = decide(_report(Flag.NORMAL, Flag.LOW))
    assert step is NextStep.FOLLOW_UP_PACKAGE


def test_critical_forces_doctor_consult():
    step, urgent, msg = decide(_report(Flag.NORMAL, Flag.CRITICAL))
    assert step is NextStep.DOCTOR_CONSULT
    assert urgent is True
    assert msg


def test_critical_outranks_high():
    """A report with both must escalate, not settle for a follow-up package."""
    step, urgent, _ = decide(_report(Flag.HIGH, Flag.CRITICAL, Flag.LOW))
    assert step is NextStep.DOCTOR_CONSULT
    assert urgent is True


def test_urgent_report_without_message_is_rejected():
    with pytest.raises(ValidationError):
        ExplainedReport(
            report=_report(Flag.CRITICAL),
            explanations=[],
            next_step=NextStep.DOCTOR_CONSULT,
            urgent=True,
            urgent_message=None,
            meta=Meta(),
        )


def test_urgent_report_not_routed_to_consult_is_rejected():
    """The schema refuses to represent an unescalated critical result."""
    with pytest.raises(ValidationError):
        ExplainedReport(
            report=_report(Flag.CRITICAL),
            explanations=[],
            next_step=NextStep.FOLLOW_UP_PACKAGE,
            urgent=True,
            urgent_message="contact a doctor today",
            meta=Meta(),
        )


# --- the closing line is decided in code, not written by the model ----------------
#
# The model used to write it, and on a report whose next step was `follow_up_package`
# it said "Take these results to a doctor" - a different pick from the same fixed list
# of three, in the same output. The structured field and the prose must agree.

from lab_explainer.nextstep import CLOSING, closing_message  # noqa: E402


def test_each_next_step_closes_with_its_own_advice():
    """The bug was one closing line used for every next step."""
    consult = closing_message(NextStep.DOCTOR_CONSULT, "en")
    follow_up = closing_message(NextStep.FOLLOW_UP_PACKAGE, "en")
    assert consult and follow_up
    assert consult != follow_up


def test_follow_up_package_recommends_a_follow_up_test():
    assert "follow-up" in closing_message(NextStep.FOLLOW_UP_PACKAGE, "en").lower()


def test_no_action_closes_with_nothing_at_all():
    for lang in ("en", "hi"):
        assert closing_message(NextStep.NO_ACTION, lang) is None


def test_every_next_step_has_both_languages_or_neither():
    for step, texts in CLOSING.items():
        assert set(texts) in ({"en", "hi"}, set()), f"{step} has {sorted(texts)}"


def test_the_closing_line_names_no_medicine_and_no_condition():
    from lab_explainer.ground import check_safety

    for texts in CLOSING.values():
        for text in texts.values():
            assert not check_safety(text), text


# --- who wrote the sentence -------------------------------------------------------
#
# The banner and the closing line are emitted from the next step in code. They cite
# correctly by construction, so counting them as model output inflated the grounding
# rate with this system's own string table. `source` is what keeps that number honest.


def _framed(*flags: Flag) -> list:
    from lab_explainer.models import Explanation, Language, Statement
    from lab_explainer.nextstep import decide
    from lab_explainer.pipeline import _frame

    report = _report(*flags)
    model_line = Statement(text="Your Test 0 is 10.0 g/dL.", test_ids=["cbc.t0"])
    step, urgent, _ = decide(report)
    return _frame(
        Explanation(language=Language.EN, statements=[model_line]), report, step, urgent
    ).statements


def test_the_model_sentence_stays_marked_as_the_models():
    kept = [s for s in _framed(Flag.HIGH) if s.source == "model"]
    assert [s.text for s in kept] == ["Your Test 0 is 10.0 g/dL."]


def test_the_lines_we_write_ourselves_are_marked_system():
    written = [s for s in _framed(Flag.CRITICAL) if s.source == "system"]
    assert len(written) == 2, [s.text for s in written]  # urgent banner + closing advice


def test_the_urgent_banner_is_first_and_is_ours():
    first = _framed(Flag.CRITICAL)[0]
    assert first.source == "system"
    assert "doctor today" in first.text


def test_an_all_normal_report_gets_nothing_added():
    assert all(s.source == "model" for s in _framed(Flag.NORMAL))
