"""Upload guards.

All of these reject before a model is ever called, so they run with no API key and
cost nothing. Rejecting junk at the door is the cheapest validation there is.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lab_explainer.api import app  # noqa: E402

client = TestClient(app)


def test_health_reports_the_model_in_use():
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["model"]


def test_index_is_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "Lab Report Explainer" in r.text


def test_non_pdf_is_rejected_before_any_model_call():
    r = client.post("/v1/reports/explain", files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_empty_upload_is_rejected():
    r = client.post("/v1/reports/explain", files={"file": ("x.pdf", b"", "application/pdf")})
    assert r.status_code == 400


def test_oversized_upload_is_rejected():
    from lab_explainer.api import MAX_BYTES

    big = b"%PDF" + b"0" * MAX_BYTES
    r = client.post("/v1/reports/explain", files={"file": ("x.pdf", big, "application/pdf")})
    assert r.status_code == 413
