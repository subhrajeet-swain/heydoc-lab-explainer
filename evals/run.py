"""Eval harness. Writes out/results.md, out/failures.md and out/cases/*.json.

Two halves, deliberately:

  fixtures  - 15 cases that touch no network. They run when the free tier is down, and
              they are where the off-by-one and the safety checks live.
  reports   - the 15 synthetic PDFs through the whole pipeline. Their specs are the
              answer key, so extraction accuracy is measured against what was actually
              printed rather than against a hand-typed label.

Everything is sequential. A parallel fan-out trips the free-tier rate limit halfway and
leaves you debugging 429s instead of the model.

  python -m evals.run            all of it
  python -m evals.run --fixtures fixtures only, no API key needed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from lab_explainer.extract import ExtractionFailed, extract_async  # noqa: E402
from lab_explainer.flags import compute_flag, flag_report, unit_mismatch  # noqa: E402
from lab_explainer.ground import check_safety, check_statement  # noqa: E402
from lab_explainer.judge import judge_async  # noqa: E402
from lab_explainer.models import (  # noqa: E402
    ExtractedReport,
    ExtractedTest,
    Flag,
    Statement,
)
from lab_explainer.pipeline import explain_report  # noqa: E402

OUT = ROOT / "out"
CASES = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))

# Published rates for gemini-3.5-flash-lite, USD per million tokens, and the USD->INR
# rate used for the projection. Actual spend on the free tier is zero; this is what the
# same traffic would cost at paid rates.
RATE_IN_USD_PER_M = 0.10
RATE_OUT_USD_PER_M = 0.40
USD_INR = 84.0


def inr(tokens_in: int, tokens_out: int) -> float:
    usd = (tokens_in * RATE_IN_USD_PER_M + tokens_out * RATE_OUT_USD_PER_M) / 1_000_000
    return usd * USD_INR


# --------------------------------------------------------------------------- fixtures


def run_fixtures() -> tuple[list[dict], dict]:
    results: list[dict] = []

    for c in CASES["boundary"]:
        got = compute_flag(ExtractedTest(**c["test"])).value
        results.append(
            {
                "id": c["id"], "group": "boundary", "why": c["why"],
                "passed": got == c["expect_flag"],
                "expected": c["expect_flag"], "got": got,
            }
        )

    for c in CASES["unit_scale"]:
        test = ExtractedTest(**c["test"])
        mismatch = unit_mismatch(test)
        ok = bool(mismatch) == c["expect_mismatch"]
        if ok and "expect_flag" in c:
            ok = compute_flag(test).value == c["expect_flag"]
        results.append(
            {
                "id": c["id"], "group": "unit_scale", "why": c["why"], "passed": ok,
                "expected": f"mismatch={c['expect_mismatch']}"
                + (f", flag={c['expect_flag']}" if "expect_flag" in c else ""),
                "got": mismatch or "no mismatch",
            }
        )

    # Grounding traps are checked against the report they claim to describe.
    trap_report = _report_from_spec(json.loads((ROOT / "data/specs/report_01.json").read_text("utf-8")))
    for c in CASES["grounding"]:
        reason = check_statement(Statement(**c["statement"]), trap_report)
        kept = reason is None
        ok = kept == c["expect_kept"]
        if ok and not kept and (needle := c.get("expect_reason_contains")):
            ok = needle in (reason or "")
        results.append(
            {
                "id": c["id"], "group": "grounding", "why": c["why"], "passed": ok,
                "expected": "kept" if c["expect_kept"] else f"dropped ({c.get('expect_reason_contains','')})",
                "got": reason or "kept",
            }
        )

    for c in CASES["safety"]:
        hits = check_safety(c["text"])
        ok = bool(hits) == c["expect_blocked"]
        results.append(
            {
                "id": c["id"], "group": "safety", "why": c["why"], "passed": ok,
                "expected": "blocked" if c["expect_blocked"] else "allowed",
                "got": ", ".join(hits) if hits else "allowed",
            }
        )

    unsafe_total = len(CASES["safety"])
    # The denominator for "got through" is the cases that were supposed to be blocked,
    # not every safety case - the allowed ones cannot be missed.
    unsafe_blockable = sum(1 for c in CASES["safety"] if c["expect_blocked"])
    unsafe_missed = sum(
        1
        for c in CASES["safety"]
        if c["expect_blocked"] and not check_safety(c["text"])
    )
    false_blocks = sum(
        1
        for c in CASES["safety"]
        if not c["expect_blocked"] and check_safety(c["text"])
    )
    return results, {
        "unsafe_missed": unsafe_missed,
        "unsafe_total": unsafe_total,
        "unsafe_blockable": unsafe_blockable,
        "false_blocks": false_blocks,
    }


# --------------------------------------------------------------------------- reports


def _report_from_spec(spec: dict):
    spec = {k: v for k, v in spec.items() if k != "render"}
    report, _ = flag_report(ExtractedReport(**spec))
    return report


def _compare(expected: ExtractedTest, got: ExtractedTest) -> list[str]:
    diffs = []
    for field in ("value", "unit", "ref_low", "ref_high"):
        e, g = getattr(expected, field), getattr(got, field)
        if isinstance(e, float) and isinstance(g, float):
            if abs(e - g) > 1e-6:
                diffs.append(f"{field}: expected {e}, got {g}")
        elif (e or "") != (g or ""):
            diffs.append(f"{field}: expected {e!r}, got {g!r}")
    return diffs


async def run_reports(languages: list[str], use_cache: bool = True) -> tuple[list[dict], dict]:
    specs = sorted((ROOT / "data/specs").glob("*.json"))
    cases: list[dict] = []
    field_hits: Counter = Counter()
    field_total: Counter = Counter()
    matrix: Counter = Counter()
    grounded: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # lang -> [kept, total]
    latencies: list[int] = []
    lat_extract: list[int] = []
    lat_explain: list[int] = []
    judged: dict = {"total": 0, "flagged": []}
    tokens_in = tokens_out = 0
    (OUT / "cases").mkdir(parents=True, exist_ok=True)

    for path in specs:
        spec = json.loads(path.read_text(encoding="utf-8"))
        render = spec.get("render", "clean")
        panel = spec["panel"]
        expected_report = _report_from_spec(spec)
        pdf = OUT / f"{path.stem}.pdf"

        case: dict = {
            "id": path.stem, "group": f"report:{render}", "panel": panel, "passed": False,
        }

        try:
            extracted, meta_x = await extract_async(pdf, use_cache)
        except ExtractionFailed as e:
            case["error"] = f"extraction failed: {e.detail[:200]}"
            cases.append(case)
            field_total[panel] += len(expected_report.results) * 4
            continue

        got_by_id = {r.test_id: r for r in extracted.results}
        diffs: list[str] = []
        for exp in expected_report.results:
            field_total[panel] += 4
            got = got_by_id.get(exp.test_id)
            if got is None:
                diffs.append(f"{exp.test_id}: missing from extraction")
                continue
            row_diffs = _compare(exp, got)
            field_hits[panel] += 4 - len(row_diffs)
            diffs.extend(f"{exp.test_id}: {d}" for d in row_diffs)

        flagged, warnings = flag_report(extracted)
        got_flags = {r.test_id: r.flag for r in flagged.results}
        for exp in expected_report.results:
            matrix[(exp.flag.value, got_flags.get(exp.test_id, Flag.NORMAL).value)] += 1

        explained, dropped, meta_e = await explain_report(flagged, languages, use_cache)
        for e in explained.explanations:
            # Only the model's own sentences. The urgent banner and the closing advice
            # are written in code from the next step, so they cite correctly by
            # construction - counting them would inflate the rate with our own strings.
            kept = sum(1 for s in e.statements if s.source == "model")
            grounded[e.language.value][0] += kept
            grounded[e.language.value][1] += kept
        for lang, _text, _reason in dropped:
            # Generated then rejected. Counted against the language that produced it -
            # charging every drop to one language would make the others look perfect.
            grounded[lang][1] += 1

        expect_urgent = any(r.flag is Flag.CRITICAL for r in expected_report.results)
        escalation_ok = explained.urgent == expect_urgent
        if not escalation_ok:
            diffs.append(f"escalation: expected urgent={expect_urgent}, got {explained.urgent}")

        unsafe = [s.text for e in explained.explanations for s in e.statements if check_safety(s.text)]
        if unsafe:
            diffs.append(f"unsafe language survived: {unsafe[0][:80]}")

        # Second opinion. The deny-list is a word list; this catches the phrasings a
        # word list structurally cannot. A disagreement is the interesting signal.
        for e in explained.explanations:
            for st in e.statements:
                v = await judge_async(st.text, use_cache)
                judged["total"] += 1
                if v.get("verdict") != "safe":
                    judged["flagged"].append(
                        {"case": path.stem, "lang": e.language.value,
                         "text": st.text, "verdict": v.get("verdict"),
                         "reason": v.get("reason", ""),
                         "deny_list_agreed": bool(check_safety(st.text))}
                    )

        # Each stage is timed only when it actually ran. Adding a cached stage's zero
        # to a live stage's time produces a per-report figure that is true of neither.
        if not meta_x.cache_hit:
            lat_extract.append(meta_x.latency_ms)
        if not meta_e.cache_hit:
            lat_explain.append(meta_e.latency_ms)
        if not meta_x.cache_hit and not meta_e.cache_hit:
            latencies.append(meta_x.latency_ms + meta_e.latency_ms)
        tokens_in += meta_x.tokens_in + meta_e.tokens_in
        tokens_out += meta_x.tokens_out + meta_e.tokens_out

        case.update(
            passed=not diffs,
            diffs=diffs,
            warnings=warnings,
            dropped=[{"lang": lang, "text": t, "reason": r} for lang, t, r in dropped],
            urgent=explained.urgent,
            next_step=explained.next_step.value,
            tokens_in=meta_x.tokens_in + meta_e.tokens_in,
            tokens_out=meta_x.tokens_out + meta_e.tokens_out,
            latency_ms=meta_x.latency_ms + meta_e.latency_ms,
        )
        cases.append(case)
        (OUT / "cases" / f"{path.stem}.json").write_text(
            explained.model_dump_json(indent=2), encoding="utf-8"
        )

    return cases, {
        "field_hits": field_hits, "field_total": field_total, "matrix": matrix,
        "grounded": grounded, "latencies": latencies, "judged": judged,
        "lat_extract": lat_extract, "lat_explain": lat_explain,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
    }


async def run_judge_probes(use_cache: bool = True) -> list[dict]:
    """Where the deny-list and the judge disagree. Not part of the 30 cases."""
    out = []
    for c in CASES["judge_probes"]:
        v = await judge_async(c["text"], use_cache)
        out.append(
            {
                "id": c["id"], "why": c["why"], "text": c["text"],
                "expected": c["expect_judge"], "judge": v.get("verdict"),
                "reason": v.get("reason", ""), "deny_list": bool(check_safety(c["text"])),
                "agree": v.get("verdict") == c["expect_judge"],
            }
        )
    return out


async def run_injection(languages: list[str]) -> list[dict]:
    out = []
    for c in CASES["injection"]:
        report = _report_from_spec({**c["report"], "panel": c["report"]["panel"]})
        explained, dropped, _ = await explain_report(report, languages)
        unsafe = [
            s.text for e in explained.explanations for s in e.statements if check_safety(s.text)
        ]
        ok = not unsafe
        if "expect_urgent" in c:
            ok = ok and explained.urgent == c["expect_urgent"]
        if "expect_next_step" in c:
            ok = ok and explained.next_step.value == c["expect_next_step"]
        out.append(
            {
                "id": c["id"], "group": "injection", "why": c["why"], "passed": ok,
                "expected": "no unsafe output"
                + (f", urgent={c['expect_urgent']}" if "expect_urgent" in c else ""),
                "got": (unsafe[0][:90] if unsafe else "clean")
                + f", urgent={explained.urgent}, {explained.next_step.value}",
                "dropped": [{"lang": l, "text": t, "reason": r} for l, t, r in dropped],
            }
        )
    return out


# ---------------------------------------------------------------------------- report


def write_results(fixtures, safety_stats, report_cases, stats, injection, languages, probes=None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_cases = fixtures + injection + report_cases
    passed = sum(1 for c in all_cases if c.get("passed"))

    L = []
    L.append("# Eval results\n")
    L.append(f"Run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
             f"languages {', '.join(languages)}\n")
    L.append(f"**{passed}/{len(all_cases)} cases passed.**\n")

    L.append("\n## Extraction accuracy, per panel\n")
    L.append("Field-level exact match on value, unit and both range bounds, against the "
             "spec the PDF was printed from. Per panel, because one bad panel hides "
             "inside an average.\n")
    L.append("| Panel | Fields correct | Accuracy |")
    L.append("|---|---|---|")
    ft, fh = stats["field_total"], stats["field_hits"]
    for panel in sorted(ft):
        total, hit = ft[panel], fh[panel]
        L.append(f"| {panel} | {hit}/{total} | {hit / total:.1%} |" if total else f"| {panel} | - | - |")
    tot, hit = sum(ft.values()), sum(fh.values())
    L.append(f"| **all** | **{hit}/{tot}** | **{hit / tot:.1%}** |" if tot else "| all | - | - |")

    L.append("\n## Flag correctness\n")
    L.append("Confusion matrix. `critical` read as `high` is a different kind of failure "
             "from `high` read as `critical`, so an accuracy number alone would hide the "
             "one that matters.\n")
    order = ["low", "normal", "high", "critical"]
    L.append("| expected \\ got | " + " | ".join(order) + " |")
    L.append("|---" * (len(order) + 1) + "|")
    for e in order:
        row = [f"**{e}**"] + [str(stats["matrix"].get((e, g), 0)) for g in order]
        L.append("| " + " | ".join(row) + " |")
    wrong = sum(v for (e, g), v in stats["matrix"].items() if e != g)
    total_flags = sum(stats["matrix"].values())
    missed_crit = sum(v for (e, g), v in stats["matrix"].items() if e == "critical" and g != "critical")
    L.append(f"\n{total_flags - wrong}/{total_flags} flags correct. "
             f"**Critical values missed: {missed_crit}.**")

    L.append("\n## Grounding\n")
    L.append("Share of generated sentences that survived the check: every cited test must "
             "exist on the report, and every number must be that test's own value or a "
             "printed range bound. Measured per language, because they fail "
             "independently.\n")
    L.append("Counted over the model's own sentences only. The urgent banner and the "
             "closing advice are emitted from the next step in code and cite correctly "
             "by construction, so counting them pads both sides of the fraction with "
             "lines that can never fail: a real failure would be diluted by roughly a "
             "quarter, and a 100% here would partly be this system marking its own "
             "string table.\n")
    L.append("| Language | Kept | Total | Grounded |")
    L.append("|---|---|---|---|")
    for lang, (kept, total) in sorted(stats["grounded"].items()):
        L.append(f"| {lang} | {kept} | {total} | {kept / total:.1%} |" if total else f"| {lang} | - | - | - |")

    L.append("\n## Safety\n")
    s = safety_stats
    inj_failed = sum(1 for c in injection if not c["passed"])
    L.append(f"- Unsafe phrasings that got through the deny-list: **{s['unsafe_missed']}/"
             f"{s['unsafe_blockable']}** (target 0)")
    L.append(f"- Allowed phrasings wrongly blocked: **{s['false_blocks']}** "
             "(a checker that blocks everything scores perfectly and is useless)")
    L.append(f"- Prompt-injection cases that produced unsafe or unescalated output: "
             f"**{inj_failed}/{len(injection)}**")
    unsafe_in_reports = sum(
        1 for c in report_cases for d in c.get("diffs", []) if d.startswith("unsafe language")
    )
    L.append(f"- Reports whose final output contained diagnosis or prescription language: "
             f"**{unsafe_in_reports}/{len(report_cases)}**")

    j = stats.get("judged", {"total": 0, "flagged": []})
    if j["total"]:
        L.append(f"\n**LLM-as-judge, second opinion over all {j['total']} delivered "
                 f"statements in both languages:** {len(j['flagged'])} flagged.")
        if j["flagged"]:
            L.append("\n| Case | Lang | Judge verdict | Deny-list agreed | Statement |")
            L.append("|---|---|---|---|---|")
            for f in j["flagged"][:12]:
                L.append(f"| {f['case']} | {f['lang']} | {f['verdict']}: "
                         f"{str(f['reason'])[:60]} | {'yes' if f['deny_list_agreed'] else 'no'} "
                         f"| {f['text'][:70]} |")
            L.append("\nA row where the deny-list did **not** agree is the point of running "
                     "a judge at all: a phrasing the word list cannot see.")
        else:
            L.append("\nThe judge agreed with the deny-list on every statement. That is a "
                     "weaker result than it looks - it means this run produced nothing in "
                     "the gap between them, not that the gap is closed. The judge has "
                     "known biases and sits behind the deterministic check, never in "
                     "front of it.")

    if probes:
        L.append("\n### Deny-list vs judge\n")
        L.append("Four strings run past both layers. The row that matters is the one the "
                 "deny-list misses and the judge catches — that is the only reason to run "
                 "a judge at all.\n")
        L.append("| | Deny-list | Judge | As expected | Text |")
        L.append("|---|---|---|---|---|")
        for pr in probes:
            L.append(f"| {pr['id']} | {'blocks' if pr['deny_list'] else 'allows'} | "
                     f"**{pr['judge']}** | {'yes' if pr['agree'] else 'NO'} | "
                     f"{pr['text'][:66]} |")
        for pr in [p for p in probes if not p["deny_list"] and p["judge"] == "unsafe"]:
            L.append(f"\n**{pr['id']} is the gap.** None of the deny-listed words appear in "
                     f"it, so the deterministic check allows it. The judge reads it as "
                     f"diagnosis by implication: _{pr['reason'][:150]}_")

    L.append("\n## Latency and cost\n")
    def pct(xs: list[int]) -> str:
        p50 = int(statistics.median(xs))
        p95 = sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]
        return f"p50 **{p50} ms**, p95 **{p95} ms** (n={len(xs)})"

    stages = [("Extraction", stats["lat_extract"]), ("Explanation", stats["lat_explain"]),
              ("End to end", stats["latencies"])]
    live = [(name, xs) for name, xs in stages if xs]
    if live:
        for name, xs in live:
            L.append(f"- {name}: {pct(xs)}")
        L.append("\n  Each stage is timed only on the runs where it actually called the "
                 "model. A cached stage contributes nothing rather than a zero, so an "
                 "end-to-end figure appears only when both stages ran live.")
    else:
        L.append("- Latency: everything was served from cache this run, so there is "
                 "nothing to time. Run `--no-cache` for live figures.")
    ti, to = stats["tokens_in"], stats["tokens_out"]
    n = max(1, len(report_cases))
    L.append(f"- Tokens: {ti} in, {to} out across {n} reports "
             f"(~{ti // n} in, ~{to // n} out per report)")
    L.append(f"- **Actual spend: ₹0** — free tier.")
    L.append(f"- Projected at published paid rates: **₹{inr(ti, to):.4f}** for this run, "
             f"**₹{inr(ti // n, to // n):.4f} per report**.")

    L.append("\n## Cases\n")
    L.append("| Case | Group | Result | Notes |")
    L.append("|---|---|---|---|")
    for c in all_cases:
        note = c.get("error") or ("; ".join(c.get("diffs", []))[:90]) or c.get("got", "")
        L.append(f"| {c['id']} | {c['group']} | {'pass' if c.get('passed') else 'FAIL'} | "
                 f"{str(note)[:90]} |")

    (OUT / "results.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    F = ["# Failures\n", "Only what failed, with the expected value beside what came back. "
         "Open the named PDF next to this file.\n"]
    any_fail = False
    for c in all_cases:
        if c.get("passed"):
            continue
        any_fail = True
        F.append(f"\n## {c['id']} ({c['group']})\n")
        if c.get("why"):
            F.append(f"_{c['why']}_\n")
        if c.get("error"):
            F.append(f"- {c['error']}")
        for d in c.get("diffs", []):
            F.append(f"- {d}")
        if "expected" in c:
            F.append(f"- expected: {c['expected']}")
            F.append(f"- got: {c['got']}")
    if not any_fail:
        F.append("\nNothing failed in this run.\n")

    dropped_any = [(c["id"], d) for c in all_cases for d in c.get("dropped", [])]
    if dropped_any:
        F.append("\n## Statements dropped before reaching the user\n")
        F.append("These were generated and then rejected. This is the guardrail working, "
                 "not a failure.\n")
        for cid, d in dropped_any:
            F.append(f"- **{cid}** — {d['reason']}\n  > {d['text'][:160]}")
    (OUT / "failures.md").write_text("\n".join(F) + "\n", encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", action="store_true", help="fixtures only, no API calls")
    ap.add_argument("--languages", default="en,hi")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the cache, for live latency and cost figures")
    args = ap.parse_args()
    languages = [x.strip() for x in args.languages.split(",") if x.strip()]

    fixtures, safety_stats = run_fixtures()
    print(f"fixtures: {sum(1 for c in fixtures if c['passed'])}/{len(fixtures)} passed")

    if args.fixtures:
        for c in fixtures:
            if not c["passed"]:
                print(f"  FAIL {c['id']}: expected {c['expected']}, got {c['got']}")
        return 0 if all(c["passed"] for c in fixtures) else 1

    probes = await run_judge_probes(use_cache=not args.no_cache)
    print(f"judge:     {sum(1 for p in probes if p['agree'])}/{len(probes)} probes matched expectation")

    injection = await run_injection(languages)
    print(f"injection: {sum(1 for c in injection if c['passed'])}/{len(injection)} passed")

    report_cases, stats = await run_reports(languages, use_cache=not args.no_cache)
    print(f"reports:  {sum(1 for c in report_cases if c['passed'])}/{len(report_cases)} passed")

    write_results(fixtures, safety_stats, report_cases, stats, injection, languages, probes)
    print(f"\nwrote {OUT / 'results.md'}\n      {OUT / 'failures.md'}\n      {OUT / 'cases'}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
