"""Flag each extracted result: critical first, then the printed range. Pure computation, no model.

Order matters: critical.json, then the fallback rule, then the printed range. A value is
never NORMAL if it is critical.
"""

from __future__ import annotations

import json
from pathlib import Path

from lab_explainer.models import ExtractedReport, ExtractedTest, Flag, LabReport, TestResult

_CRITICAL: dict[str, dict] = json.loads(
    (Path(__file__).resolve().parents[2] / "data" / "critical.json").read_text(encoding="utf-8")
)
_PRINTED = {
    "L": Flag.LOW, "H": Flag.HIGH, "N": Flag.NORMAL,
    # Indian lab reports commonly print the word rather than the letter. Without these
    # the printed-vs-computed cross-check is silently lost on real reports.
    "LOW": Flag.LOW, "HIGH": Flag.HIGH, "NORMAL": Flag.NORMAL,
    "L*": Flag.LOW, "H*": Flag.HIGH, "": Flag.NORMAL,
}

# Units that are the same quantity written differently. Anything not listed here and not
# an exact match is treated as a genuine mismatch.
_UNIT_ALIASES = {
    "10^6/ul": "million/ul",
    "10e6/ul": "million/ul",
    "miu/l": "uiu/ml",  # numerically equal for TSH
    "cells/ul": "/ul",
    "iu/l": "u/l",
    # Indian lab reports. "cumm" is cubic millimetre, which is a microlitre.
    "cumm": "/ul",
    "cmm": "/ul",
    "/cumm": "/ul",
    "/cmm": "/ul",
    "cells/cumm": "/ul",
    "mill/cumm": "million/ul",
    "mil/cumm": "million/ul",
    "million/cumm": "million/ul",
    # Deliberately absent: thou/cumm, 10^3/cumm and similar. Those are a thousand per
    # microlitre, a different scale from the thresholds stored here, so they must keep
    # failing the unit check rather than quietly comparing across scales.
}


def _norm_unit(unit: str) -> str:
    u = unit.strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")
    return _UNIT_ALIASES.get(u, u)


def unit_mismatch(test: ExtractedTest) -> str | None:
    """Does the printed unit disagree with the unit our threshold is expressed in?

    This is the quiet one. Platelets printed as 250000 /uL compared against a threshold
    stored per 10^3/uL is off by a thousand, produces a confident flag, and nothing else
    in the pipeline would ever notice.
    """
    crit = _CRITICAL.get(test.test_id)
    if crit is None:
        return None
    if _norm_unit(crit["unit"]) == _norm_unit(test.unit):
        return None
    return (
        f"{test.test_id}: report says {test.unit!r} but threshold is in {crit['unit']!r} — "
        f"critical thresholds skipped for this result"
    )


def compute_flag(test: ExtractedTest) -> Flag:
    crit = _CRITICAL.get(test.test_id)
    if crit is not None and unit_mismatch(test):
        # Comparing across scales is worse than having no threshold. Fall through to the
        # range-width heuristic, which only ever uses figures printed on this report.
        crit = None
    if crit is not None:
        low, high = crit["critical_low"], crit["critical_high"]
        if (low is not None and test.value <= low) or (high is not None and test.value >= high):
            return Flag.CRITICAL
    elif test.ref_low is not None and test.ref_high is not None:
        # ponytail: heuristic, not clinical. Ceiling: a wide printed range makes this too lax,
        # and a zero-width range makes any deviation critical. Upgrade: add the test to critical.json.
        width = test.ref_high - test.ref_low
        if test.value < test.ref_low - width or test.value > test.ref_high + width:
            return Flag.CRITICAL

    if test.ref_low is not None and test.value < test.ref_low:
        return Flag.LOW
    if test.ref_high is not None and test.value > test.ref_high:
        return Flag.HIGH
    return Flag.NORMAL


def _direction(test: ExtractedTest) -> Flag:
    """Which side of the range a value is on. A critical with no printed range is judged by its threshold."""
    if test.ref_low is not None and test.value < test.ref_low:
        return Flag.LOW
    if test.ref_high is not None and test.value > test.ref_high:
        return Flag.HIGH
    low = _CRITICAL.get(test.test_id, {}).get("critical_low")
    return Flag.LOW if low is not None and test.value <= low else Flag.HIGH


def _why(test: ExtractedTest, direction: Flag) -> str:
    if test.ref_low is None or test.ref_high is None:
        return f"value {test.value} has no complete printed range"
    word = {Flag.LOW: "below", Flag.HIGH: "above"}.get(direction, "within")
    return f"value {test.value} is {word} range {test.ref_low}-{test.ref_high}"


def _warning(test: ExtractedTest, computed: Flag) -> str | None:
    printed = _PRINTED.get((test.printed_flag or "N").strip().upper())
    if printed is None:
        return f"{test.test_id}: unrecognised printed flag {test.printed_flag!r} (computed: {computed})"
    if computed is Flag.CRITICAL:
        ok = printed is _direction(test)
    else:
        ok = printed is computed
    if ok:
        return None
    direction = _direction(test) if computed is Flag.CRITICAL else computed
    return (
        f"{test.test_id}: printed flag {test.printed_flag!r} but {_why(test, direction)} "
        f"(computed: {computed})"
    )


def flag_report(extracted: ExtractedReport) -> tuple[LabReport, list[str]]:
    results = []
    warnings = []
    for test in extracted.results:
        computed = compute_flag(test)
        results.append(TestResult(**test.model_dump(), flag=computed))
        if mismatch := unit_mismatch(test):
            warnings.append(mismatch)
        if warning := _warning(test, computed):
            warnings.append(warning)
    report = LabReport(**extracted.model_dump(exclude={"results"}), results=results)
    return report, warnings
