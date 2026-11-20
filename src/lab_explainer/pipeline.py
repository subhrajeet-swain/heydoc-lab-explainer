"""The nine steps of PLAN.md section 4, in order.

Read this top to bottom and the design is visible: two model calls, with the flagging,
grounding, safety and escalation decisions made in code around them.
"""

from __future__ import annotations

from pathlib import Path

from .explain import explain_async
from .extract import extract_async
from .flags import flag_report
from .ground import verify
from .models import (
    ExplainedReport,
    Explanation,
    Flag,
    LabReport,
    Meta,
    NextStep,
    Statement,
)
from .nextstep import closing_message, decide, urgent_message


def _frame(
    explanation: Explanation, report: LabReport, next_step: NextStep, urgent: bool
) -> Explanation:
    """Wrap the model's sentences in the two lines that are decided in code.

    The urgent banner goes first and the next-step advice goes last. Neither is written
    by the model, for the same reason the flags are not: they announce decisions this
    system already made, and a sentence that contradicts the decision beside it is worse
    than no sentence at all.
    """
    lang = explanation.language.value
    statements = list(explanation.statements)

    if urgent:
        critical = [r.test_id for r in report.results if r.flag is Flag.CRITICAL]
        statements.insert(
            0,
            Statement(text=urgent_message(lang), test_ids=critical, source="system"),
        )

    if closing := closing_message(next_step, lang):
        abnormal = [r.test_id for r in report.results if r.flag is not Flag.NORMAL]
        statements.append(
            Statement(
                text=closing,
                test_ids=abnormal or [r.test_id for r in report.results],
                source="system",
            )
        )

    return Explanation(language=explanation.language, statements=statements)


async def explain_report(
    report: LabReport, languages: list[str] | None = None, use_cache: bool = True
) -> tuple[ExplainedReport, list[tuple[str, str, str]], Meta]:
    """Steps 5-9. Returns the report, (language, text, reason) for everything dropped, and usage."""
    languages = languages or ["en"]

    explanations, meta = await explain_async(report, languages, use_cache)

    # Dropped statements carry their language. Attributing them all to one language
    # would deflate that language's grounding rate and inflate every other one.
    kept, dropped = [], []
    for e in explanations:
        checked, removed = verify(e, report)
        kept.append(checked)
        dropped.extend((e.language.value, text, reason) for text, reason in removed)

    next_step, urgent, message = decide(report)
    kept = [_frame(e, report, next_step, urgent) for e in kept]

    explained = ExplainedReport(
        report=report,
        explanations=kept,
        next_step=next_step,
        urgent=urgent,
        urgent_message=message,
        meta=meta,
    )
    return explained, dropped, meta


async def explain_pdf(
    pdf: Path, languages: list[str] | None = None, use_cache: bool = True
) -> tuple[ExplainedReport, list[str], list[tuple[str, str, str]]]:
    """Full pipeline. Returns the report, flag warnings, and dropped statements."""
    extracted, extract_meta = await extract_async(pdf, use_cache)
    report, warnings = flag_report(extracted)
    explained, dropped, explain_meta = await explain_report(report, languages, use_cache)

    explained.meta = Meta(
        latency_ms=extract_meta.latency_ms + explain_meta.latency_ms,
        tokens_in=extract_meta.tokens_in + explain_meta.tokens_in,
        tokens_out=extract_meta.tokens_out + explain_meta.tokens_out,
        model=extract_meta.model,
        cache_hit=extract_meta.cache_hit and explain_meta.cache_hit,
    )
    return explained, warnings, dropped
