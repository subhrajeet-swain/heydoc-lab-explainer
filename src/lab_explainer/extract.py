"""Reads a lab report PDF into schema-validated JSON.

This is the only place the model sees the report. It returns what is printed and
nothing more — no severity, no interpretation. A response that does not validate is
rejected rather than repaired: a well-formed but wrong object passing straight through
is the failure you find in production, not in testing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from google.adk.agents import LlmAgent
from google.genai import types
from pydantic import ValidationError

from . import cache
from .models import ExtractedReport, Meta

# Per stage, because the stages have different jobs. Extraction reads a page and is
# the cheap, high-volume half; explanation writes the words a person reads. Being
# able to move one without the other is how you test whether the expensive model is
# earning its place - and it keeps one stage's cache warm while the other changes.
MODEL = os.getenv("LAB_EXTRACT_MODEL") or os.getenv(
    "LAB_MODEL", "gemini-3.5-flash-lite"
)

# The vocabulary the pipeline understands. Anything outside it has no critical
# threshold and no stable id for grounding to cite, so the model is told to use these
# ids exactly and to omit a row it cannot map.
VOCABULARY = """
cbc.hemoglobin        Hemoglobin              g/dL
cbc.rbc               RBC Count               million/uL
cbc.wbc               WBC Count               /uL
cbc.platelets         Platelet Count          /uL
cbc.hematocrit        Hematocrit              %
cbc.mcv               MCV                     fL
lipid.total_cholesterol  Total Cholesterol    mg/dL
lipid.ldl             LDL Cholesterol         mg/dL
lipid.hdl             HDL Cholesterol         mg/dL
lipid.triglycerides   Triglycerides           mg/dL
hba1c.hba1c           HbA1c                   %
thyroid.tsh           TSH                     uIU/mL
thyroid.t3            Total T3                ng/dL
thyroid.t4            Total T4                ug/dL
lft.bilirubin_total   Total Bilirubin         mg/dL
lft.alt               ALT (SGPT)              U/L
lft.ast               AST (SGOT)              U/L
lft.alp               Alkaline Phosphatase    U/L
lft.albumin           Albumin                 g/dL
""".strip()

INSTRUCTION = f"""You read diagnostics lab reports and return exactly what is printed.

Return one entry per test row in the results table, using these test_id values:

{VOCABULARY}

Rules:
- `unit` must be copied from the report, not from the list above. The list shows the
  usual unit; the report is the authority. If they differ, report what is printed.
- `ref_low` and `ref_high` come from the printed reference range. A range printed as
  "< 200" has ref_low null and ref_high 200. A range printed as "> 40" has ref_low 40
  and ref_high null. A range printed as "13.0 - 17.0" has both.
- `printed_flag` is the flag character as printed: "L", "H", "N", or null if the cell
  is blank. Copy it even if it looks wrong. Do not correct it.
- Do not judge whether a value is normal, high, low or dangerous. That is not your job
  and the field does not exist.
- Omit any row whose test you cannot map to an id above.
- The report may be rotated, faint or crowded. Read it anyway.

- `printed_flag` is the flag as printed, whatever form it takes: "L", "H", "N", or a
  word like "Low", "High", "Normal". Null if the cell is blank.
- The report may print units in local forms. `cumm` and `cu mm` mean per microlitre;
  `mill/cumm` means millions per microlitre. Copy what is printed; do not convert.

Return a single JSON object and nothing else:

{{
  "results": [
    {{"test_id": "...", "test_name": "...", "value": <number>, "unit": "...",
      "ref_low": <number|null>, "ref_high": <number|null>,
      "printed_flag": "L"|"H"|"N"|"Low"|"High"|"Normal"|null}}
  ],
  "report_id": "<as printed, or null>",
  "patient_age": <int or null>,
  "patient_sex": "M" | "F" | null,
  "collected_on": "YYYY-MM-DD, or null"
}}

`results` is the part that matters and must be right. The four fields after it are
whatever the letterhead happened to say. **Use null for any of them you cannot read
with certainty** — a sample or template report may have its date or id blanked out,
and a guessed date is worse than no date. Never invent one, and never reformat a
partly redacted date such as "202X" into something that looks valid.

No commentary outside the object.
"""


def _agent() -> LlmAgent:
    return LlmAgent(
        name="extractor",
        model=MODEL,
        description="Reads a lab report PDF into structured JSON.",
        instruction=INSTRUCTION,
        # No output_schema, and this is a deliberate trade rather than a limitation
        # someone forgot about.
        #
        # `ExtractedTest` sets `extra="forbid"`, which Pydantic renders as
        # `additionalProperties`. The Gemini API has no such field in a response schema
        # and rejects the whole request:
        #
        #   400 INVALID_ARGUMENT - Unknown name "additional_properties" at
        #   'generation_config.response_schema.properties[4].value.items'
        #
        # Removing `extra="forbid"` does make it work — verified against both the raw
        # client and ADK's `output_schema`, including the date field and the sex regex.
        # That would buy API-side enforcement of the shape and let the JSON template
        # come out of the prompt, at the price of no longer rejecting unknown keys.
        #
        # It was left as is because the response is validated here regardless, against
        # the real model, rather than trusted because the provider promised a shape —
        # and provider enforcement is itself a thing worth having a second opinion on.
        # Doing both would be better than either; see README, "Not done, and why".
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def _json_from(text: str) -> dict:
    """Pull the JSON object out of a response that may carry fences or prose."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if m := re.search(r"\{[\s\S]*\}", text):
            return json.loads(m.group(0))
        raise


def failure_message(text: str) -> str:
    """Which sentence the person who uploaded the file should see.

    A response with no test rows at all is what a CV, an invoice or a holiday photo
    looks like - a different problem from a lab report that would not parse, and it
    deserves a different sentence. Everything else is treated as unreadable.
    """
    compact = re.sub(r"\s+", "", text)
    if '"results":[]' in compact or '"results":null' in compact:
        return ExtractionFailed.NOT_A_REPORT
    return ExtractionFailed.UNREADABLE


async def extract_async(
    pdf_path: Path, use_cache: bool = True
) -> tuple[ExtractedReport, Meta]:
    pdf = pdf_path.read_bytes()
    key = f"extract:{hashlib.sha256(pdf).hexdigest()}:{INSTRUCTION}"

    if use_cache and (hit := cache.get(MODEL, key)):
        return ExtractedReport(**hit["report"]), Meta(
            latency_ms=0,
            tokens_in=hit["tokens_in"],
            tokens_out=hit["tokens_out"],
            model=MODEL,
            cache_hit=True,
        )

    started = time.monotonic()
    from .adk_runner import run_agent

    text, tokens_in, tokens_out = await run_agent(
        _agent(),
        [
            types.Part.from_bytes(data=pdf, mime_type="application/pdf"),
            types.Part(text="Extract this report."),
        ],
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        report = ExtractedReport(**_json_from(text))
    except (ValidationError, json.JSONDecodeError) as e:
        # Fail loudly. A partial read explained confidently is worse than no answer.
        # An empty results list means the model found no test rows at all, which is
        # what a CV or an invoice looks like - a different problem from a lab report
        # that would not parse, and worth a different sentence.
        raise ExtractionFailed(str(e), text, message=failure_message(text)) from e

    if use_cache:
        cache.put(
            MODEL,
            key,
            {
                "report": report.model_dump(mode="json"),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )

    return report, Meta(
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=MODEL,
        cache_hit=False,
    )


class ExtractionFailed(Exception):
    """Raised when the response is not a lab report we can stand behind.

    Carries two messages on purpose. `message` is for the person who uploaded the file;
    `detail` is the validator output, which is for whoever has to debug it and says
    nothing useful to a patient.
    """

    NOT_A_REPORT = (
        "This file does not look like a lab report. Upload the report PDF your lab "
        "sent you — the page with the results table on it."
    )
    UNREADABLE = (
        "This looks like a lab report, but not enough of it could be read reliably. "
        "Rather than explain a partial reading, nothing is shown. If the file is a "
        "scan, a clearer copy usually works."
    )

    def __init__(self, detail: str, raw: str = "", message: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.raw = raw
        self.message = message or self.UNREADABLE


def extract(pdf_path: Path, use_cache: bool = True) -> tuple[ExtractedReport, Meta]:
    return asyncio.run(extract_async(pdf_path, use_cache))
