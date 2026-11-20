"""Grounding and safety checks over generated explanations.

Two independent questions, deliberately kept apart:

  grounded  — is every sentence about a test that exists, quoting numbers that are
              actually on the report
  safe      — does it diagnose or prescribe

An explanation can be correct and ungrounded at the same time. "Your HbA1c suggests
pre-diabetes" may be true and still comes from the model's memory rather than the page,
and it stays confidently true after the lab changes its ranges. So grounding is measured
on its own, not folded into an accuracy number.

Nothing here calls a model.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Explanation, LabReport, Statement

# Rounding slack when comparing a number in prose to an extracted value. The model is
# allowed to write 11.2 for 11.20, and 7 for 7.0.
TOLERANCE = 0.051

# A digit wedged inside a word is part of a name, not a measurement: the "1" in HbA1c,
# the "3" in Total T3, the "1" in एचबीए1सी. The first version of this regex matched them
# and dropped correct, fully grounded sentences as unsourced. The eval run is what
# surfaced it - 12 valid statements removed across the HbA1c and thyroid panels.
# `[^\W\d_]` is "any unicode letter", so the guard holds for Devanagari too.
_NUMBER = re.compile(r"(?<![^\W\d_])(?<!\d)(?<!\d\.)\d[\d,]*(?:\.\d+)?(?![^\W\d_])")

# A comma means two different things depending on where it sits. In "18,000" it groups
# thousands; in "12,5" it is a decimal point. Only a comma followed by exactly three
# digits, with more digits or nothing after, is a grouping separator. Reading "18,000"
# as 18.0 dropped a correct sentence about a critically low platelet count - the eval
# caught it, the same way it caught the digits inside test names.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?:[,.]|$))")

# Diagnosis and prescription language. Deterministic first pass; the eval's LLM judge is
# the second opinion that catches phrasings this list cannot anticipate.
DENY_PATTERNS: tuple[str, ...] = (
    # naming a condition as the patient's
    r"\byou (?:have|are suffering from|suffer from|are diagnosed)\b",
    r"\bdiagnos(?:ed|is|e)\b",
    r"\byou are (?:diabetic|anaemic|anemic|hypothyroid|hyperthyroid)\b",
    r"\bthis (?:means|indicates|confirms) (?:you|that you)\b",
    # condition names
    r"\b(?:diabetes|anaemia|anemia|thalassemia|leukemia|leukaemia|hepatitis|cirrhosis)\b",
    r"\b(?:hypothyroidism|hyperthyroidism|kidney disease|heart disease|cancer)\b",
    # treatment
    r"\b(?:metformin|insulin|statin|atorvastatin|thyroxine|levothyroxine|aspirin)\b",
    r"\byou should (?:take|start|stop|increase|reduce) (?:a |the )?(?:dose|medicine|medication|tablet)\b",
    r"\b(?:prescrib|medicat|dosage)\w*\b",
    # unsupported prognosis
    r"\b(?:dangerous|life[- ]threatening|fatal|will cause|could kill)\b",
    # --- Hindi -------------------------------------------------------------------
    # The explanation is generated in Hindi, not translated from the English, so an
    # ASCII-only list leaves the Hindi output with no deterministic gate at all. The
    # judge reads Hindi but runs only in the eval; this is the one that runs on every
    # response. No \b: Hindi takes case suffixes directly onto the noun (मधुमेह /
    # मधुमेही), and a boundary would miss the inflected form.
    # condition names
    r"(?:मधुमेह|डायबिटीज|शुगर की बीमारी)",
    r"(?:एनीमिया|खून की कमी|रक्ताल्पता|थैलेसीमिया|ल्यूकेमिया|ब्लड कैंसर)",
    r"(?:हाइपोथायरायडिज्म|हाइपरथायरायडिज्म|थायरॉयड की बीमारी)",
    r"(?:हेपेटाइटिस|पीलिया|सिरोसिस|लिवर की बीमारी|जिगर की बीमारी)",
    r"(?:किडनी की बीमारी|गुर्दे की बीमारी|हृदय रोग|दिल की बीमारी|कैंसर)",
    # naming it as theirs, or diagnosing
    # "आपको ... बीमारी/रोग है" only. Not a bare "है": "आपको डॉक्टर से बात करनी है" is
    # the allowed sentence, and a checker that blocks everything scores perfectly and
    # is useless.
    r"आपको .{0,20}(?:बीमारी|रोग) है",
    r"(?:निदान|डायग्नोस)",
    r"इसका (?:मतलब|अर्थ) है कि आप",
    # treatment
    r"(?:मेटफॉर्मिन|इंसुलिन|स्टैटिन|थायरोक्सिन|एस्पिरिन)",
    r"(?:दवा|दवाई|गोली|खुराक)\s*(?:लें|लेना|शुरू|बढ़ा|घटा)",
    # unsupported prognosis
    r"(?:खतरनाक|जानलेवा|घातक|जान को खतरा)",
)

_DENY = tuple(re.compile(p, re.IGNORECASE) for p in DENY_PATTERNS)

# Devanagari digits, so a Hindi sentence's numbers are checkable by the same code.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("०१२३४५६७८९")}


def _numbers(text: str) -> list[float]:
    normalised = unicodedata.normalize("NFKC", text).translate(_DIGIT_MAP)
    out = []
    for raw in _NUMBER.findall(normalised):
        try:
            out.append(float(_THOUSANDS.sub("", raw).replace(",", ".")))
        except ValueError:
            continue
    return out


def _permitted(report: LabReport, test_ids: list[str]) -> set[float]:
    """Numbers a sentence about these tests is allowed to contain.

    The value itself plus its printed range bounds — a sentence may legitimately say
    "above the range of 4.0 - 5.6". Nothing else.
    """
    allowed: set[float] = set()
    for r in report.results:
        if r.test_id in test_ids:
            allowed.add(r.value)
            if r.ref_low is not None:
                allowed.add(r.ref_low)
            if r.ref_high is not None:
                allowed.add(r.ref_high)
    return allowed


def check_statement(stmt: Statement, report: LabReport) -> str | None:
    """Return None if grounded, else the reason it is not."""
    known = {r.test_id for r in report.results}

    unknown = [t for t in stmt.test_ids if t not in known]
    if unknown:
        return f"cites test not on this report: {', '.join(unknown)}"

    allowed = _permitted(report, stmt.test_ids)
    for n in _numbers(stmt.text):
        # ponytail: every number in the sentence must be one of the cited test's own
        # figures. Strict — it also rejects an incidental "2 weeks". In a health
        # explanation an unsourced number is exactly what we are trying to catch, so
        # the false positives are the cheap direction to be wrong in. Upgrade path is
        # a unit-aware parser if the drop rate turns out to be noise rather than signal.
        if not any(abs(n - a) <= TOLERANCE for a in allowed):
            return f"number {n} is not any cited value or range bound"

    return None


def check_safety(text: str) -> list[str]:
    """Deny-list hits. Empty means the deterministic check passed."""
    return [m.group(0) for p in _DENY if (m := p.search(text))]


def verify(
    explanation: Explanation, report: LabReport
) -> tuple[Explanation, list[tuple[str, str]]]:
    """Drop every statement that is ungrounded or unsafe.

    Returns the surviving explanation and a list of (statement text, reason) for
    everything removed, so failures.md can show what was dropped and why.
    """
    kept: list[Statement] = []
    dropped: list[tuple[str, str]] = []

    for stmt in explanation.statements:
        if reason := check_statement(stmt, report):
            dropped.append((stmt.text, reason))
            continue
        if hits := check_safety(stmt.text):
            dropped.append((stmt.text, f"unsafe language: {', '.join(hits)}"))
            continue
        kept.append(stmt)

    return Explanation(language=explanation.language, statements=kept), dropped
