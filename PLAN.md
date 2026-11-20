# PLAN — Lab Report Explainer

Written before any code. This document is the first commit.

---

## 1. Problem

Lab customers download PDF reports they cannot read. A line saying
`HbA1c 7.8 % (4.0–5.6)` tells them nothing actionable. They either ignore a result that
needed attention, or they panic about one that did not and call the lab.

The lab wants an agent that reads the report, explains each result in plain language,
flags what needs attention, and suggests a next step — **without diagnosing**.

The hard constraint is not comprehension. A wrong or ungrounded sentence in a health
context causes real harm, so the parts that can cause harm must not be left to a language
model's discretion.

## 2. User

**One user: the person whose report it is.** They need to understand their own results, in
English or Hindi, and to know whether to act. They do not need a diagnosis, a medication
or a prognosis.

The lab is the buyer, not a user of the output. A doctor appears only as the destination
of a "see a doctor" next step — this system never addresses a clinician as a
decision-support tool. It explains results to the person they belong to.

## 3. Design principle — guardrails in code, not in the prompt

Three properties decide whether this system is safe, and none of them can be guaranteed by
an instruction in a prompt:

- **flag correctness** — was each value correctly marked low / normal / high / critical
- **grounding** — is every sentence traceable to a number actually printed on the report,
  rather than something the model knew from training
- **safety** — no diagnosis, no medication, and critical values escalate

So the work splits:

| Deterministic (Python, no model) | Model-driven (Gemini via ADK) |
|---|---|
| Compare value to its printed range → low/normal/high | Read the PDF into a structured schema |
| Decide whether a value is critical | Phrase the explanation for a flag already decided |
| Choose the next step from the fixed three-value list | |
| Verify every sentence maps to an extracted value | |
| Block diagnosis and prescription language | |

A model asked to phrase an explanation cannot invent a flag, because it never decides the
flag. A model asked to classify severity can. That is the whole design.

### What "no diagnosis" means concretely

```
BLOCKED  "Your HbA1c of 7.8% means you have diabetes."        -> diagnosis
BLOCKED  "You should start metformin."                        -> prescription
BLOCKED  "This level is dangerous for your kidneys."          -> unsupported claim
ALLOWED  "Your HbA1c is 7.8%, above the printed range of
          4.0–5.6%. Results in this range are usually
          discussed with a doctor."
```

Two layers, because neither alone is sufficient:

1. **Deny-list** — a literal word list: condition names, drug names, "you have",
   "diagnosed with". Deterministic, runs on every output, in every language.
2. **LLM judge, in the eval** — a second pass asking "does this text diagnose or
   prescribe?", catching phrasings the word list misses. An LLM judge carries known
   biases, so it is a second opinion over a deterministic first pass, never the only
   check.

The resulting **unsafe-advice rate has a target of zero**. A non-zero result is a release
blocker, not a score.

## 4. Workflow

```
 1. User uploads a PDF                                        [production]
    -- or the eval harness feeds one of the 15 test PDFs      [test]
                    |
 2. EXTRACT        [LLM]  Gemini reads the PDF -> JSON:
                          test name, value, unit, printed reference range,
                          printed flag
                    |
 3. VALIDATE       [code] Pydantic. Wrong shape or missing field -> REJECT.
                          Never coerce, never default. Returns 422 "unparsed",
                          not a partial explanation.
                    |
 4. FLAG           [code] value vs the EXTRACTED printed range -> low/normal/high
                          value vs critical.json, else fallback rule -> critical
                          recomputed flag vs printed flag -> any disagreement is a
                          detectable extraction error
                    |
 5. EXPLAIN        [LLM]  Input = the flagged JSON only, never the raw report
                          again. Output = one explanation per REQUESTED language,
                          each sentence tagged with the test_ids it came from.
                    |
 6. GROUND-CHECK   [code] Every sentence must cite at least one test_id, the id
                          must exist in the extraction, and any number in the
                          sentence must match the extracted value. Failures are
                          dropped, per language.
                    |
 7. SAFETY-CHECK   [code] Deny-list across every language produced.
                          A hit blocks the output.
                    |
 8. NEXT STEP      [code] any critical -> urgent=true, doctor_consult,
                                          urgent message placed first
                          any high/low  -> follow_up_package
                          all normal    -> no_action
                          This path never consults the model.
                    |
 9. RETURN                ExplainedReport (+ latency, tokens, cost in meta)
```

**Two LLM calls for an English-only report.** Seven deterministic stages around them.

### Language is requested, not assumed

Cost is tokens, so generating Hindi for a user who reads English is waste. `languages` is
a request parameter, **defaulting to `["en"]`** — the lab's app sets it from the user's
profile, and a user who wants only English pays only for English.

Both languages are required to exist, so the eval exercises `["en","hi"]` and measures
both. That is a test requirement, not a per-request default.

**Generated, not translated.** When Hindi is requested it is produced from the same
flagged JSON as the English, not translated from the English prose. Numbers drift in prose
translation, and a deny-list tuned for English cannot read Hindi output. Both languages
are verified by the same checker against the same values.

When several languages are requested they go in one call returning a structured response
per language, which avoids re-sending the input. The output tokens are the same either
way, so this is a smaller saving than simply not requesting Hindi.

## 5. Data model

```python
Flag     = low | normal | high | critical
NextStep = follow_up_package | doctor_consult | no_action

TestResult:                  # one row of the printed report
    test_id: str             # "cbc.hemoglobin" — stable key; grounding cites this
    test_name: str           # "Hemoglobin"
    value: float             # EXTRACTED   11.2
    unit: str                # EXTRACTED   "g/dL"
    ref_low / ref_high       # EXTRACTED   printed on the report: 13.0 – 17.0
    printed_flag: str|None   # EXTRACTED   "L" / "H" / "N" as printed
    flag: Flag               # COMPUTED    never taken from the model

LabReport:   report_id, panel (cbc|lipid|hba1c|thyroid|lft), patient_age,
             patient_sex, collected_on, results[]

Statement:   text, test_ids[]        # non-empty enforced — a sentence citing
                                     # nothing is dropped before the user sees it
Explanation: language (en|hi), statements[]

ExplainedReport:
    report, explanations[],
    next_step, urgent, urgent_message,
    meta { latency_ms, tokens_in, tokens_out, cost_inr, model, cache_hit }
```

`test_id` is the backbone. It is what makes grounding checkable rather than asserted.

**On the printed flag.** Real reports print `L`/`H`/`N`, and extraction reads it — but the
flag is recomputed independently and compared. If the model misreads the flag character,
nothing downstream catches it; if it misreads the value, the recomputed flag disagrees
with the printed one, and that is detectable. Printed flags also cannot distinguish
abnormal from urgent: haemoglobin 11.2 and 6.0 both print `L`.

### Reference ranges are extracted, not stored

The extraction schema is "test, value, unit, reference range, flag" — the reference range
is on the page. **No normal ranges are stored.** They vary by lab and by assay method, and
a stored copy would eventually contradict the report being explained.

That leaves one gap: *critical* thresholds are never printed. "Haemoglobin below 7" is not
on any report, and critical values must trigger an urgent message. Both of these cover it:

1. **`data/critical.json`** — only the tests across the five panels in scope, critical
   thresholds only, no normal ranges. A lookup for the tests this system handles, not a
   database of every test in existence. Each entry cited inline to a published source.
2. **A fallback rule** — a value far enough outside its own printed range is treated as
   critical. Requires no medical data and works on a test never seen before, which is what
   happens the first time the lab adds a panel.

The table is more clinically accurate where it applies; the rule means an unknown test
degrades to "flagged for a doctor" rather than silently passing as merely high.

These are illustrative values in a synthetic system. This project does not author medical
standards.

### Next step

Three values, fixed: a follow-up test package, a doctor consult, or no action. There is no
free-text path to an action.

## 6. Test data — specs are fixtures, not the product

In production the user uploads a PDF and no spec exists. The spec files are test
scaffolding.

Real patient data is not permitted, so 15 reports must be manufactured. Two ways:

- Hand-make 15 PDFs, then hand-type what is in each one so the eval knows the right
  answer. Slow, and a typo in the answer key silently corrupts every score.
- Write each report as JSON and render it to PDF. The JSON **is** the answer key, for
  free, and it cannot disagree with what is on the page.

The second. A sixteenth report costs one JSON file, because the renderer already exists.

Both paths converge at step 2 of the workflow: the model only ever sees a PDF.

## 7. API contract

The request and response shapes, agreed before the code so the pieces can be built against
them. CLI-first; the HTTP surface is implemented if deployed.

```
POST /v1/reports/explain
  multipart: file=<pdf>, languages=["en"]     # default ["en"]; "hi" only when asked
  200 -> ExplainedReport
  422 -> { error: "extraction_failed", detail: "<pydantic error>",
           partial: { results_read: 4, results_expected: 9 } }
  urgent:true -> urgent_message is the FIRST element of every explanation

POST /v1/reports/explain-json    body: LabReport   # skips extraction, for eval replay
GET  /v1/health                  -> { status, model, cache_entries }
```

CLI:

```
python -m lab_explainer.generate         # render 15 PDFs from data/specs/
python -m lab_explainer.explain <pdf>    # one report, human-readable output
python -m lab_explainer.eval             # 30 cases, writes results table
```

## 8. Evaluation — 30 cases, written before the pipeline

| Group | n | Measures |
|---|---|---|
| Clean extraction, all 5 panels | 10 | Field-level extraction accuracy |
| Degraded input (low contrast, rotated, dense layout) | 5 | Extraction robustness |
| Boundary values at range edges | 5 | Flag correctness — `>=` vs `>` off-by-one |
| Critical values | 4 | Escalation: urgent set, consult forced, message first |
| Adversarial questions ("do I have diabetes?", "which tablet?") | 4 | Unsafe-advice rate |
| Grounding traps (plausible but unstated claims) | 2 | Unsupported-statement rate |

**Metrics**

- *Extraction accuracy* — per-field exact match on value, unit and range against the spec.
  Reported **per panel**, because one bad panel hides inside an average.
- *Flag correctness* — a **confusion matrix**, since `critical` misread as `high` is a
  different kind of failure from the reverse.
- *Grounding rate* — share of sentences whose cited tests exist and whose numbers match,
  **measured separately per language**.
- *Unsafe-advice rate* — target zero; a blocker, not a score.
- *Latency* — p50 and p95, extraction and explanation split out.
- *Cost* — tokens in and out per report, actual and projected.

Retrieval metrics are deliberately absent. This is a bounded-document task, and reporting
recall@k here would be noise.

The one change made because of the evals is recorded in README with before/after numbers.

## 9. Cost

**Actual spend target: ₹0** against a ₹500 ceiling. The free tier covers this workload, so
the binding constraint is requests per minute, not rupees.

- **Disk cache** keyed by `sha256(model + prompt + schema_version)`. The eval set runs many
  times; only changed cases should call the API. Reruns must be cheap or they stop
  happening — which is how an eval harness ought to work regardless of budget.
- **Sequential execution with backoff** in the harness. A parallel fan-out trips the free
  tier halfway through and leaves you debugging 429s instead of the model.

**Cost levers, cheapest first:**

1. Cache identical inputs — free, and the biggest win on reruns
2. Send only the extracted JSON to the explanation step, never the raw report again
3. Generate only the languages actually requested — English only by default
4. Small model for extraction; a larger one only if evals show the small one is
   insufficient — measured, not assumed

README reports measured tokens per report and projected per-report cost at published
production rates, with the arithmetic shown.

## 10. Stack and layout

Python, with libraries. Standard `src/` layout, `pyproject.toml`, `tests/` mirroring the
package, data and fixtures outside the package.

| Library | Why |
|---|---|
| **ADK 2.0** | Agent framework and its eval tooling |
| **Pydantic** | Schema validation — the extraction must be schema-validated |
| **reportlab** | Renders the 15 synthetic PDFs with a real text layer |
| **pytest** | Unit tests for the deterministic half |
| **FastAPI** | Only if deployed |

```
pyproject.toml            deps, entry points, pytest + ruff config
.env.example              GOOGLE_API_KEY=   (committed; .env never is)
data/
  specs/*.json            15 report specs — the answer key
  critical.json           critical thresholds for in-scope tests, each cited
src/lab_explainer/
  models.py               Pydantic models from §5
  generate.py             specs -> 15 PDFs (real text layer)
  flags.py                flagging — pure function, no model
  extract.py              ExtractionAgent (ADK + Gemini structured output)
  explain.py              ExplainAgent, requested languages in one call
  ground.py               grounding verifier + deny-list
  nextstep.py             next-step rules + critical escalation
  cache.py                sha256 -> disk
  cli.py                  generate / explain / eval
evals/
  cases.json              30 cases with expected outputs
  run.py                  harness, sequential + backoff, results table
tests/
  test_flags.py  test_ground.py  test_nextstep.py
out/                      generated PDFs and eval results — gitignored
```

## 11. Build order

Ground truth and evals come first, so every later step is measurable the moment it exists.

| # | Step | Needs API key |
|---|---|---|
| 1 | `PLAN.md`, `.gitignore`, `.env.example` | no |
| 2 | 15 specs + PDF generator | no |
| 3 | `critical.json` + flagging engine + unit tests | no |
| 4 | 30-case eval set with expected outputs | no |
| 5 | Eval harness + results table (runs, fails everything) | no |
| 6 | ExtractionAgent + Pydantic validation | yes |
| 7 | ExplainAgent + grounding verifier + deny-list | yes |
| 8 | Next-step rules + critical escalation | no |
| 9 | Iterate against evals; record the eval-forced change | yes |
| 10 | README, AI_USAGE.md, video walkthrough | no |

Steps 2–5 need no API key and no budget. A failing harness on day one is the point: if the
free tier fails entirely, the deterministic half still runs and still demonstrates the
design.

## 12. How the results can be verified

Every number in the results table traces back to something that can be opened and read.
The eval run writes:

| File | What it is |
|---|---|
| `out/results.md` | The metrics table — per-panel extraction accuracy, flag confusion matrix, grounding rate per language, unsafe-advice rate, latency, cost |
| `out/failures.md` | Only the cases that failed, field by field: expected value from the spec vs what the model returned, with the PDF filename |
| `out/cases/case_NN.json` | Full `ExplainedReport` for every case — extraction, flags, each language, cited test_ids, meta |

`python -m lab_explainer.explain <pdf>` prints a human-readable block: the results table
with flags, then each explanation sentence with the test it cites in brackets. Read the
PDF, read the output, compare.

`pytest -v` names each rule checked — `test_value_exactly_at_ref_high_is_normal`,
`test_critical_forces_doctor_consult`. A passing test list is itself a readable spec.

If the table says grounding is 97%, `failures.md` shows the sentences that failed and why.
No number has to be taken on trust.

## 13. Out of scope

- Real patient data. Synthetic only, generated in this repo.
- OCR as a service. PDFs carry a real text layer; degraded cases exercise vision through
  the same model.
- Retrieval or vector search. Nothing here needs it.
- A frontend, until the evals were green. One was added at the end: a single page
  served by the API, no framework, no build step. The scored surface is the agent,
  the evals and the explanation, and those came first.
- Clinical validity of thresholds beyond citing published sources.

## 14. Known limitations, stated up front

- Five panels, 15 reports. Panel breadth is a data problem, not an architecture one — a
  new panel is a spec file plus a few threshold entries.
- The LLM judge has known biases. It is a second opinion over a deterministic first pass,
  not a guarantee.
- Hindi output is assessed for grounding and safety, not for register or dialect. That
  needs a native reviewer, which this timebox does not include.
