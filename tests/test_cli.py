"""The CLI printer, on the path where the guardrail actually fires.

`dropped` carries (language, text, reason) since drops started being attributed per
language. The printer still unpacked two values, so the one command meant to show the
guardrail working crashed with `ValueError: too many values to unpack` exactly when a
statement was dropped - and never on a clean report, which is what you demo.
"""

from __future__ import annotations

from lab_explainer.cli import _print_report
from lab_explainer.models import (
    ExplainedReport,
    Explanation,
    Flag,
    LabReport,
    Language,
    Meta,
    NextStep,
    Statement,
    TestResult,
)


def _explained() -> ExplainedReport:
    report = LabReport(
        results=[
            TestResult(
                test_id="cbc.hemoglobin", test_name="Hemoglobin", value=11.2,
                unit="g/dL", ref_low=13.0, ref_high=17.0, flag=Flag.LOW,
            )
        ]
    )
    return ExplainedReport(
        report=report,
        explanations=[
            Explanation(
                language=Language.EN,
                statements=[Statement(text="Your hemoglobin is 11.2 g/dL.",
                                      test_ids=["cbc.hemoglobin"])],
            )
        ],
        next_step=NextStep.FOLLOW_UP_PACKAGE,
        urgent=False,
        meta=Meta(),
    )


def test_printing_a_dropped_statement_does_not_crash(capsys):
    dropped = [("hi", "आपका हीमोग्लोबिन 9.9 g/dL है।", "number 9.9 is not on this report")]
    _print_report(_explained(), ["cbc.hemoglobin: printed flag N but value 11.2"], dropped)
    out = capsys.readouterr().out
    assert "[hi]" in out
    assert "not on this report" in out


def test_a_clean_report_prints_neither_section(capsys):
    _print_report(_explained(), [], [])
    out = capsys.readouterr().out
    assert "dropped before output" not in out
    assert "flag disagreements" not in out
