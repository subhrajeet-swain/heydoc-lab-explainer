"""HTTP surface, implementing the contract in PLAN.md section 7.

Thin on purpose. Every decision already happened in the pipeline; this layer moves bytes
and maps one failure to a 422. It adds `warnings` and `dropped` to the response so the
page can show what the guardrails caught — a dropped statement is the system working,
and hiding it from the UI would defeat the point of having it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

from . import cache  # noqa: E402
from .extract import MODEL, ExtractionFailed  # noqa: E402
from .models import ExplainedReport  # noqa: E402
from .pipeline import explain_pdf  # noqa: E402

STATIC = Path(__file__).parent / "static"
MAX_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Lab Report Explainer", version="1.0")


class Dropped(BaseModel):
    language: str
    text: str
    reason: str


class ExplainResponse(ExplainedReport):
    """The contract's ExplainedReport, plus what the guardrails removed."""

    warnings: list[str] = []
    dropped: list[Dropped] = []


@app.get("/v1/health")
def health() -> dict:
    return {"status": "ok", "model": MODEL, "cache_entries": cache.entries()}


@app.post("/v1/reports/explain", response_model=ExplainResponse)
async def explain(
    file: UploadFile = File(...),
    languages: str = Form("en,hi"),
) -> ExplainResponse:
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, detail=f"file over {MAX_BYTES // 1024 // 1024} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, detail="not a PDF")

    wanted = [x.strip() for x in languages.split(",") if x.strip() in {"en", "hi"}]

    # The pipeline reads from a path, so the upload lands in a temp file that is deleted
    # on the way out. Nothing a user uploads is kept.
    tmp = Path(tempfile.mkdtemp()) / "report.pdf"
    try:
        tmp.write_bytes(data)
        explained, warnings, dropped = await explain_pdf(tmp, wanted or ["en"])
    except ExtractionFailed as e:
        raise HTTPException(
            422,
            detail={
                "error": "extraction_failed",
                # For the person who uploaded the file.
                "message": e.message,
                # For whoever has to debug it. Never the primary thing shown.
                "detail": e.detail[:500],
                "partial": {"raw_response_chars": len(e.raw)},
            },
        ) from e
    finally:
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir()

    return ExplainResponse(
        **explained.model_dump(),
        warnings=warnings,
        dropped=[Dropped(language=lang, text=t, reason=r) for lang, t, r in dropped],
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
