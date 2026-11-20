# AI usage

Built with Claude Code. This is where it was used, what it got wrong, and which decisions
were mine.

## The decisions are mine

The architecture in `PLAN.md` is a set of choices I made and then held the tooling to.
The two that shaped everything came out of rejecting earlier drafts of the plan:

**Reference ranges are extracted, not stored.** The first design kept a local table of
normal ranges. I cut it. The brief's own extraction schema lists `reference range` as a
field to read off the report, ranges differ by lab and by assay method, and a stored copy
would eventually contradict the page being explained. Only *critical* thresholds are
stored, because those are the one thing never printed anywhere.

**Explanations are generated per requested language.** The first design produced English
and Hindi on every request. Cost is tokens, and generating Hindi for someone who reads
English is waste. `languages` became a request parameter, and the cache was later keyed
per language so that adding Hindi to an existing report does not re-pay for the English.

**Guardrails live in code, not in the prompt.** Flagging, critical escalation, next-step
selection and grounding verification contain no model call. Three properties carry nearly
all the risk here — was the flag right, is every sentence traceable to a printed number,
does it avoid diagnosing — and none can be guaranteed by an instruction in a prompt. A
model that never decides severity cannot get severity wrong.

Plan first, enforced by the repo: the first commit is `PLAN.md` and nothing else.

## How the work was executed

**Written briefs, verified before acceptance.** Mechanical work with an objective
pass/fail — the 15 report specs, the PDF renderer, the flagging engine and its unit tests
— went to parallel subagents on a small, fast model, each given a written brief and a
verification step it had to run and report before I accepted the result. A mistake in that
work surfaces as a failing test.

**The risky half stayed under review.** The grounding verifier, the eval metric
computation and the escalation path ran on the strongest model available and I read them
line by line. A verifier that is subtly too lenient reports 98% grounding and you publish
a number that is not real. That is the one failure mode a test suite does not catch for
you — and, as below, it still got past me once.

**Context discipline.** Subagents got a brief and returned a summary; their file reads
never entered the main context. The main thread held the plan, the schema and the
decisions, not the contents of every file.

**Token discipline is in the artefact, not just the workflow.** Every model call is cached
on disk by `sha256(model + prompt + schema)`. A full eval rerun costs 1.4 seconds and zero
tokens. Actual spend on this project: ₹0.

## What the AI got wrong, and how it was caught

Six things. None were caught by trusting output; all were caught by review, by running the
code, or by the evals.

**1. Unit scale mismatch — the dangerous one.** The generated `critical.json` stored WBC
and platelet thresholds per 10³/µL while the reports print per µL. A platelet count of
250000 would have been compared against a threshold of 20000 — wrong by a factor of a
thousand, producing a confident flag with nothing downstream to catch it. I rescaled the
data and then added a guard: on any unit mismatch the stored threshold is skipped and a
range-width heuristic takes over, since that only ever uses figures printed on that
report. Six tests cover it.

**2. T4 scale.** The same file carried free-T4 thresholds in ng/dL while reports print
total T4 in µg/dL. Same class of error. The subagent flagged its own uncertainty here,
because its brief required it to report thresholds it could not source rather than guess
silently.

**3. A claimed feature that did not exist.** An earlier draft of this file, and of the
README, described an LLM-as-judge pass in the eval. There wasn't one — the safety check
was the deny-list alone. I caught it auditing the repo against the brief line by line, and
built the judge rather than delete the claim. It now runs over every delivered statement
in both languages, and the eval reports where it disagrees with the deny-list. **This is
the failure mode worth naming: AI-written documentation describing a system more complete
than the one that exists.** In a healthcare context that is the most expensive kind of
wrong, because it reads as diligence.

**4. A grounding verifier that was quietly too strict.** The check requires every number
in a sentence to be the cited test's own value or a printed range bound. The regex also
matched digits inside *test names* — the `1` in `HbA1c`, the `3` in `Total T3`, the `1` in
`एचबीए1सी` — so twelve correct, fully sourced statements were discarded as unsupported.
English grounding read 75.5% when it was actually 100%. Separately, the harness charged
every dropped statement to English regardless of which language produced it, inflating
Hindi to a false 100%. Two bugs, wrong in opposite directions, neither throwing. **Only
the eval run surfaced them**, which is the argument for writing the evals before the
pipeline.

**5. A validator trace shown to a patient.** Uploading a CV instead of a lab report
returned a raw Pydantic error to the browser — `2 validation errors for ExtractedReport,
report_id Input should be a valid string [type=string_type...]`. Rejecting the file was
correct; showing that to someone was not, and it leaks internals. There are now two
messages: one for the person who uploaded the file, one for whoever has to debug it. A
response with no test rows at all is distinguished from a report that would not parse,
because "this isn't a lab report" and "this report couldn't be read" are different
problems and deserve different sentences.

**6. Structured output the API rejects.** Pydantic's `extra="forbid"` emits
`additionalProperties`, which Gemini will not accept in a response schema. Rather than
loosening the model I dropped the API-side schema and validate locally — the stronger
choice, since the response is then checked against the real schema here instead of being
trusted because the provider promised a shape.

**7. A schema stricter than the brief asked for.** The brief names five fields to
schema-validate: test, value, unit, reference range, flag. The generated schema also
demanded `report_id`, `panel`, `patient_age`, `patient_sex` and `collected_on`, all
mandatory. Nobody asked for them, and nothing downstream reads them.

It surfaced the first time I ran a real downloaded CBC report through the system instead
of my own synthetic ones. The report was read perfectly — all six values, correct units,
correct ranges, and it worked out that "Packed Cell Volume" is haematocrit — and was then
discarded whole, because the sample template prints its year as `202X` and carries no
report number. Two header fields nobody reads rejected a flawless clinical extraction.

I cut the schema back to what the brief specifies and made the letterhead optional.
**The lesson is the general one: the invented requirement was the one that broke it.**
Fifteen synthetic reports I generated myself all had tidy ids and real dates, so the
extra strictness cost nothing until it met a real document. The same report also carried
local units (`cumm`, `mill/cumm`), word flags (`Low` instead of `L`) and a `Borderline`
flag this system does not recognise — none of which my own test data contained.

**8. Readability edits that walked into unsafe territory — caught by the judge.** The
first explanations read like a mail-merge: "Results like this are usually discussed with
a doctor" after every single result, four times in one report, and "the printed range"
where a person would say "the normal range". I rewrote the instruction for shorter,
plainer sentences and told it to match the wording to how far outside the range a value
sat.

It did exactly that, and produced *"Your ALT is 850.0 U/L ... so this is critical"* and,
in Hindi, *"यह गंभीर है"*. That is our internal flag name leaking into text a patient
reads, where "critical" is a statement about **them**, not about the number.

The deny-list allowed all seven — none of them contains a listed word. **The LLM judge
flagged every one**, which is the only time in this project the two layers disagreed on
real output, and precisely the case a word list structurally cannot catch.

The fix was not better wording but removing the model's licence to grade severity at all.
Urgency already has a controlled channel: the urgent banner, written in code and placed
first. Per-result sentences now state the value against its range and stop. A mildly low
result and a critical one get the same sentence shape; the number carries the difference.

**The sequence is the point: a change made purely for readability moved the system
toward diagnosis, and only the second opinion saw it.**

**9. Prose that contradicted the structured decision beside it.** The next step is chosen
in code from the brief's fixed list of three. The closing sentence of the explanation was
not — the model wrote it — and on reports whose next step was `follow_up_package` it said
*"Take these results to a doctor"*, which is a different option on the same list.

Nothing failed. Both halves were individually reasonable, the safety checks passed, and
the grounding check passed because the sentence cites real tests and quotes no numbers.
It only shows up if you read the structured field and the prose together, which is what
the person asking me to check it did.

It also falsified a claim in my own README — "there is no free-text path to an action" —
while that sentence was sitting there being a free-text action. The closing line now
comes from a table keyed on `next_step`, in both languages, with `no_action` closing with
nothing. Five tests cover it.

**The pattern across items 8 and 9 is one thing: every time the model was left to
describe a decision the system had already made, it eventually described it wrongly.**
The flags went into code first, then severity wording, now the next step. What the model
is for is phrasing the values; everything that announces a decision belongs beside the
decision.

**10. Four defects a structured review pass found that 89 tests did not.** Near the end I
ran an adversarial review over the whole diff — every finding reproduced by running the
code, not by reading it. Four were real, and each one had been invisible for the same
reason: nothing in the suite was looking at it.

- **The deny-list was ASCII only.** `check_safety("आपको मधुमेह है और आपको मेटफॉर्मिन लेना
  चाहिए।")` returned no hits; the English sentence saying the same thing returned three.
  The Hindi explanation is generated, not translated, so Hindi output had no
  deterministic safety gate at all. The judge reads Hindi, but it runs only in the eval.
  This is the half of a 20-point block that I had written a README section about.
- **The CLI crashed exactly when the guardrail fired.** `dropped` became
  `(language, text, reason)` when drops started being attributed per language; the
  printer still unpacked two. `ValueError: too many values to unpack` — never on a clean
  report, only on the one that demonstrates the guardrail working.
- **The grounding rate counted my own strings.** The urgent banner and closing line from
  item 9 are generated in code, so they cite correctly by construction, and the eval
  counted them as model output. Fixing item 9 silently inflated the metric that measures
  item 9. Statements now carry `source`, and the published rate fell from 55/55 to 40/40.
- **The urgent banner was English for a Hindi-only reader.** `decide()` returns one
  `urgent_message` and the web UI rendered it at the top, even though the per-language
  banner already existed in the statements below it.

The first three are the same shape as items 8 and 9: a decision made in one place, a
description of it maintained somewhere else. The third is the sharpest version — the fix
for one problem moved the number that would have reported it.

## Where AI was deliberately not used

Flagging, grounding, safety and escalation contain no model call at all. They are plain
Python with unit tests that run with no API key and no budget — 91 tests, no network.

The one place a model does judge something is the LLM-as-judge pass, and it sits *behind*
the deterministic deny-list rather than replacing it. An LLM judge is biased toward
agreeing with fluent text, so a "safe" verdict from it does not make something safe; a
disagreement with the deny-list is the only signal worth reading. The eval reports exactly
that disagreement.

## Tools

- **Claude Code** — design review and the safety-critical modules on the strongest model
  available; spec generation, the PDF renderer and the flagging engine delegated to a
  smaller, faster model running as parallel subagents
- **Google ADK 2.x** with `gemini-3.5-flash-lite` for extraction, explanation and the
  judge
- reportlab, Pydantic, FastAPI, pytest — ordinary library code
