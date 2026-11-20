"""Schemas for the whole pipeline. See PLAN.md section 5.

The one rule that matters here: `value`, `unit` and the reference range are EXTRACTED
from the report, `flag` is COMPUTED. The model never decides severity.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator


class Flag(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class NextStep(StrEnum):
    FOLLOW_UP_PACKAGE = "follow_up_package"
    DOCTOR_CONSULT = "doctor_consult"
    NO_ACTION = "no_action"


class Panel(StrEnum):
    CBC = "cbc"
    LIPID = "lipid"
    HBA1C = "hba1c"
    THYROID = "thyroid"
    LFT = "lft"


class Language(StrEnum):
    EN = "en"
    HI = "hi"


class ExtractedTest(BaseModel):
    """One row of the printed report, exactly as the model read it.

    These are the five fields the brief asks to be schema-validated: test, value, unit,
    reference range, flag. This is the extraction boundary, and it is strict — a missing
    value fails validation rather than becoming a zero.

    Everything else about a report is administrative. It is kept beside this, optional,
    and it is never allowed to reject a report. The strictness belongs where a wrong
    answer can hurt someone, which is here and not on the letterhead.
    """

    model_config = {"extra": "forbid"}

    test_id: str
    test_name: str
    value: float
    unit: str
    ref_low: float | None = None
    ref_high: float | None = None
    printed_flag: str | None = None

    @model_validator(mode="after")
    def _range_must_be_ordered(self) -> ExtractedTest:
        if self.ref_low is not None and self.ref_high is not None:
            if self.ref_low > self.ref_high:
                raise ValueError(f"{self.test_id}: ref_low {self.ref_low} > ref_high {self.ref_high}")
        return self


class TestResult(ExtractedTest):
    """An extracted test with the flag this system computed for it."""

    __test__ = False  # it is a lab test, not a pytest test

    flag: Flag


class ReportHeader(BaseModel):
    """Whatever the letterhead happened to say.

    Every field is optional and none of them can reject a report. A real sample report
    may carry no report number at all, or print its date as `202X` because it is a
    blank template. Nothing downstream reads any of this — not a statement, not a flag,
    not the escalation, not the next step — so refusing to explain someone's blood
    because the year was redacted is a bug, not rigour.
    """

    report_id: str | None = None
    patient_age: int | None = Field(default=None, ge=0, le=120)
    patient_sex: str | None = Field(default=None, pattern="^[MF]$")
    collected_on: date | None = None


def _panel_of(results: list[ExtractedTest]) -> Panel | None:
    """Derived from the test ids, not extracted.

    Asking the model to classify the panel invites it to be wrong about something we
    can read straight off the ids we just validated.
    """
    for r in results:
        prefix = r.test_id.split(".", 1)[0]
        if prefix in Panel.__members__.values() or prefix in {p.value for p in Panel}:
            return Panel(prefix)
    return None


class LabReport(ReportHeader):
    results: list[TestResult] = Field(min_length=1)

    @computed_field  # serialised, so the API and the page can show it
    @property
    def panel(self) -> Panel | None:
        return _panel_of(self.results)


class ExtractedReport(ReportHeader):
    """What the extraction agent returns, before flagging.

    Extra keys are ignored rather than rejected. A model that volunteers a field we did
    not ask for is not a reason to discard a correctly read report.
    """

    results: list[ExtractedTest] = Field(min_length=1)

    @property
    def panel(self) -> Panel | None:
        return _panel_of(self.results)


class Statement(BaseModel):
    """One sentence of explanation, with the tests it is allowed to be about.

    `test_ids` is non-empty by construction: a sentence supported by nothing does
    not reach the user.
    """

    text: str
    test_ids: list[str] = Field(min_length=1)
    # Who wrote the sentence. The urgent banner and the closing advice are emitted from
    # the next step in code, not by the model, and saying so in the schema is what keeps
    # the grounding rate honest: a line this system wrote always cites correctly, so
    # counting it would measure our own string table rather than the model.
    source: Literal["model", "system"] = "model"


class Explanation(BaseModel):
    language: Language
    statements: list[Statement]


class Meta(BaseModel):
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_inr: float = 0.0
    model: str = ""
    cache_hit: bool = False


class ExplainedReport(BaseModel):
    report: LabReport
    explanations: list[Explanation]
    next_step: NextStep
    urgent: bool = False
    urgent_message: str | None = None
    meta: Meta = Field(default_factory=Meta)

    @model_validator(mode="after")
    def _urgent_implies_message_and_consult(self) -> ExplainedReport:
        if self.urgent:
            if not self.urgent_message:
                raise ValueError("urgent report has no urgent_message")
            if self.next_step is not NextStep.DOCTOR_CONSULT:
                raise ValueError(f"urgent report must route to doctor_consult, got {self.next_step}")
        return self
