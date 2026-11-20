"""Picks the next step from the fixed three-value list.

No model is consulted here. The escalation path for a critical value must be the same
every time, and it must not depend on how a sentence was phrased.
"""

from __future__ import annotations

from .models import Flag, LabReport, NextStep

URGENT_EN = (
    "One or more of your results is far outside the expected range. "
    "Please contact a doctor today."
)
URGENT_HI = (
    "आपके एक या अधिक परिणाम अपेक्षित सीमा से बहुत बाहर हैं। "
    "कृपया आज ही डॉक्टर से संपर्क करें।"
)


# The closing advice, one per next step, written here rather than by the model.
#
# The next step is chosen in code from a fixed list, so the sentence announcing it must
# come from the same place. When the model wrote this line it would say "take these to a
# doctor" on a report whose next step was `follow_up_package` - two different picks from
# the same three-value list, in one output. `no_action` gets no line at all: there is
# nothing to advise, and inventing reassurance is its own kind of claim.
CLOSING: dict[NextStep, dict[str, str]] = {
    NextStep.DOCTOR_CONSULT: {
        "en": "Take these results to a doctor.",
        "hi": "इन परिणामों को डॉक्टर को दिखाएं।",
    },
    NextStep.FOLLOW_UP_PACKAGE: {
        "en": "Ask your lab or your doctor about a follow-up test for the results above.",
        "hi": "ऊपर दिए गए परिणामों के लिए अपनी लैब या डॉक्टर से दोबारा जांच के बारे में पूछें।",
    },
    NextStep.NO_ACTION: {},
}


def closing_message(step: NextStep, language: str) -> str | None:
    return CLOSING[step].get(language) or CLOSING[step].get("en")


def decide(report: LabReport) -> tuple[NextStep, bool, str | None]:
    """Return (next_step, urgent, urgent_message) for a flagged report."""
    flags = {r.flag for r in report.results}

    if Flag.CRITICAL in flags:
        return NextStep.DOCTOR_CONSULT, True, URGENT_EN

    if flags & {Flag.HIGH, Flag.LOW}:
        return NextStep.FOLLOW_UP_PACKAGE, False, None

    return NextStep.NO_ACTION, False, None


def urgent_message(language: str) -> str:
    return URGENT_HI if language == "hi" else URGENT_EN
