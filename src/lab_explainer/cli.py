"""Command line entry points.

    python -m lab_explainer.cli generate
    python -m lab_explainer.cli explain out/report_02.pdf --languages en,hi
    python -m lab_explainer.cli eval

`explain` prints the results table with its flags, then each sentence with the test it
cites in brackets. Read the PDF, read this, compare. Nothing is hidden behind a score.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FLAG_MARK = {"low": "LOW ", "normal": "    ", "high": "HIGH", "critical": "CRIT"}


def _utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot print Devanagari."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _print_report(explained, warnings, dropped) -> None:
    r = explained.report
    head = [
        r.report_id,
        r.panel.value if r.panel else None,
        f"{r.patient_age}{r.patient_sex}" if r.patient_age and r.patient_sex else None,
        str(r.collected_on) if r.collected_on else None,
    ]
    print("\n" + "  ".join(x for x in head if x))
    print("-" * 78)
    for t in r.results:
        rng = (
            f"{t.ref_low} - {t.ref_high}"
            if t.ref_low is not None and t.ref_high is not None
            else (f"< {t.ref_high}" if t.ref_high is not None else f"> {t.ref_low}")
        )
        print(f"  {FLAG_MARK[t.flag.value]}  {t.test_name:<24} {t.value:>10}  "
              f"{t.unit:<12} {rng}")

    print("-" * 78)
    print(f"  next step: {explained.next_step.value}"
          + ("   *** URGENT ***" if explained.urgent else ""))

    for e in explained.explanations:
        print(f"\n  [{e.language.value}]")
        for s in e.statements:
            print(f"    - {s.text}")
            print(f"      cites: {', '.join(s.test_ids)}")

    if warnings:
        print("\n  flag disagreements (printed vs computed):")
        for w in warnings:
            print(f"    ! {w}")

    if dropped:
        print("\n  dropped before output (guardrail working):")
        for lang, text, reason in dropped:
            print(f"    x [{lang}] {reason}\n        {text[:100]}")

    m = explained.meta
    print(f"\n  {m.model} · {m.tokens_in} in / {m.tokens_out} out · "
          f"{m.latency_ms} ms{' · cached' if m.cache_hit else ''}\n")


def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="lab_explainer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("generate", help="render the 15 synthetic PDFs from data/specs")

    ex = sub.add_parser("explain", help="explain one report PDF")
    ex.add_argument("pdf", type=Path)
    ex.add_argument("--languages", default="en,hi",
                help="comma separated; fewer languages cost fewer tokens")
    ex.add_argument("--no-cache", action="store_true")

    ev = sub.add_parser("eval", help="run the eval suite")
    ev.add_argument("--fixtures", action="store_true", help="no API calls")

    args = ap.parse_args()

    if args.cmd == "generate":
        from .generate import main as generate_main

        return generate_main() or 0

    if args.cmd == "explain":
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        from .extract import ExtractionFailed
        from .pipeline import explain_pdf

        languages = [x.strip() for x in args.languages.split(",") if x.strip()]
        try:
            explained, warnings, dropped = asyncio.run(
                explain_pdf(args.pdf, languages, use_cache=not args.no_cache)
            )
        except ExtractionFailed as e:
            print(f"extraction_failed: {e.detail}", file=sys.stderr)
            return 2
        _print_report(explained, warnings, dropped)
        return 0

    if args.cmd == "eval":
        argv = ["--fixtures"] if args.fixtures else []
        sys.argv = ["evals.run", *argv]
        sys.path.insert(0, str(ROOT))
        from evals.run import main as eval_main

        return asyncio.run(eval_main())

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
