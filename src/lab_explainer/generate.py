"""Render data/specs/report_NN.json to out/report_NN.pdf. See PLAN.md section 6.

Drawn straight onto the canvas, so every value is a real string in the PDF text layer.
The four render styles are a colour, a rotation, or a font size. Nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "data" / "specs"
OUT = ROOT / "out"

LAB = "Meridian Diagnostics"
FOOTER = "Synthetic report generated for testing. Not a real patient."
HEADERS = ("Test", "Result", "Unit", "Reference Range", "Flag")
COLUMNS = (50, 230, 300, 380, 500)  # x positions matching HEADERS
FILLER = [  # dense style only: crowds the table above and below
    "Method: automated haematology analyser, impedance and optical counting. Verified by senior technologist.",
    "Specimen received in acceptable condition. Haemolysis index within laboratory limits.",
    "Reference intervals are for adults and may vary by assay method, laboratory and population.",
    "Results are to be interpreted in the context of clinical findings and prior results.",
    "Values marked H or L lie outside the printed reference interval. Flags are as printed by the laboratory.",
    "Turnaround time: results released after validation. Amended reports supersede this document.",
    "Footnote: critical results are telephoned to the ordering clinician as per laboratory policy.",
    "Footnote: sample stored at 2-8 C for 7 days after reporting; retained specimens available on request.",
    "Disclaimer: this report does not constitute a diagnosis. Please consult the ordering clinician.",
    "Abbreviations: ALT alanine aminotransferase; AST aspartate aminotransferase; TSH thyroid stimulating hormone.",
]


def _num(x: float) -> str:
    return str(float(x))  # keeps the ".0" the reference ranges print with


def _range(low: float | None, high: float | None) -> str:
    if low is not None and high is not None:
        return f"{_num(low)} - {_num(high)}"
    if high is not None:
        return f"< {_num(high)}"  # one-sided ranges are printed the way labs print them
    return f"> {_num(low)}"


def _draw(c: canvas.Canvas, spec: dict) -> None:
    w, h = A4
    style = spec["render"]
    dense = style == "dense"
    c.saveState()
    if style == "rotated":
        c.translate(w / 2, h / 2)
        c.rotate(3)
        c.translate(-w / 2, -h / 2)
    c.setFillGray(0.4 if style == "low_contrast" else 0)

    y = h - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, LAB)
    y -= 14
    c.setFont("Helvetica", 9)
    c.drawString(50, y, "Diagnostic Laboratory Report")
    y -= 24

    for label, text in (
        ("Patient", f"Test Patient {spec['report_id']}"),
        ("Age / Sex", f"{spec['patient_age']} / {spec['patient_sex']}"),
        ("Collected", spec["collected_on"]),
        ("Report ID", spec["report_id"]),
        ("Panel", spec["panel"].upper()),
    ):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, y, label)
        c.setFont("Helvetica", 9)
        c.drawString(130, y, text)
        y -= 13

    size, lead = (7.5, 9) if dense else (10, 18)
    if dense:
        y -= 8
        c.setFont("Helvetica", size)
        for line in FILLER[:5]:
            c.drawString(50, y, line)
            y -= lead

    y -= 14
    c.setFont("Helvetica-Bold", size)
    for x, head in zip(COLUMNS, HEADERS):
        c.drawString(x, y, head)
    y -= 4
    c.line(50, y, 545, y)
    y -= lead

    c.setFont("Helvetica", size)
    for r in spec["results"]:
        cells = (
            r["test_name"],
            _num(r["value"]),
            r["unit"],
            _range(r["ref_low"], r["ref_high"]),
            r["printed_flag"] or "",
        )
        for x, cell in zip(COLUMNS, cells):
            c.drawString(x, y, cell)
        y -= lead

    if dense:
        y -= 8
        for line in FILLER[5:]:
            c.drawString(50, y, line)
            y -= lead

    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, 30, FOOTER)
    c.restoreState()
    c.showPage()


def render(spec_path: Path, out_dir: Path = OUT) -> Path:
    """Render one spec file to out_dir/<spec stem>.pdf and return the PDF path."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{spec_path.stem}.pdf"
    c = canvas.Canvas(str(out_path), pagesize=A4)
    _draw(c, spec)
    c.save()
    return out_path


def main() -> None:
    for spec_path in sorted(SPECS.glob("report_*.json")):
        print(render(spec_path))


if __name__ == "__main__":
    main()
