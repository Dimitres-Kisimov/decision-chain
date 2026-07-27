"""Phase-4 tests: executive deliverables (chain/exports.py) from a fixture artifact.

Both deliverables must be non-trivial, structurally correct, quote the
artifact's numbers exactly, and regenerate byte-identically from the same
artifact (fixed metadata, no wall-clock anywhere).
"""

from __future__ import annotations

import pandas as pd
import pytest

from chain import exports

EXPECTED_SHEETS = [
    "Stages", "Identities", "CostToServe", "SlottingComparison", "Ledger", "Assumptions",
]


def test_pdf_builds_nonempty(fixture_artifact, tmp_path):
    pdf = exports.build_pdf(fixture_artifact, tmp_path / "report.pdf")
    assert pdf.exists()
    assert pdf.stat().st_size > 10_000
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_excel_builds_with_expected_sheets(fixture_artifact, tmp_path):
    xlsx = exports.build_excel(fixture_artifact, tmp_path / "ledger.xlsx")
    assert xlsx.stat().st_size > 5_000
    workbook = pd.ExcelFile(xlsx)
    assert workbook.sheet_names == EXPECTED_SHEETS
    identities = pd.read_excel(workbook, "Identities")
    assert len(identities) == 13
    assert set(identities["status"]) == {"PASS"}
    stages = pd.read_excel(workbook, "Stages")
    assert len(stages) == 7


def test_excel_quotes_artifact_numbers_exactly(fixture_artifact, tmp_path):
    xlsx = exports.build_excel(fixture_artifact, tmp_path / "ledger.xlsx")
    identities = pd.read_excel(pd.ExcelFile(xlsx), "Identities")
    by_name = {row["name"]: row for _, row in identities.iterrows()}
    for check in fixture_artifact["identities"]:
        row = by_name[check["name"]]
        # xlsx serializes floats via shortest-repr; agreement to 1e-12 relative
        assert row["lhs"] == pytest.approx(check["lhs"], rel=1e-12)
        assert row["rhs"] == pytest.approx(check["rhs"], rel=1e-12)


def test_deliverables_regenerate_deterministically(fixture_artifact, tmp_path):
    pdf1 = exports.build_pdf(fixture_artifact, tmp_path / "r1.pdf").read_bytes()
    pdf2 = exports.build_pdf(fixture_artifact, tmp_path / "r2.pdf").read_bytes()
    assert pdf1 == pdf2
    xlsx1 = exports.build_excel(fixture_artifact, tmp_path / "l1.xlsx").read_bytes()
    xlsx2 = exports.build_excel(fixture_artifact, tmp_path / "l2.xlsx").read_bytes()
    assert xlsx1 == xlsx2
