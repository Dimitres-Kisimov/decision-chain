"""Project paths, resolved relative to the repository root.

The raw UCI Online Retail II workbook is NOT committed and is looked up in two
places, in order:

1. ``data/raw/online_retail_II.xlsx`` inside this repository (populated by
   ``python scripts/download_data.py``), and
2. the sibling checkout ``../retail-analytics-real/data/raw/`` — the repo that
   first cleaned this dataset. Reading the same physical file both repos use is
   part of the cross-repo consistency story.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"
DATA_INTERIM = ROOT / "data" / "interim"

RAW_XLSX = DATA_RAW / "online_retail_II.xlsx"
SIBLING_RAW_XLSX = ROOT.parent / "retail-analytics-real" / "data" / "raw" / "online_retail_II.xlsx"

INTERIM_PARQUET = DATA_INTERIM / "combined_raw.parquet"
INTERIM_CSV = DATA_INTERIM / "combined_raw.csv.gz"

FIXTURE_CSV = ROOT / "tests" / "fixtures" / "sample.csv"
FIXTURE_EXPECTED = ROOT / "tests" / "fixtures" / "expected.json"

ARTIFACTS_DIR = ROOT / "artifacts"
ARTIFACT_JSON = ARTIFACTS_DIR / "full_run.json"

DELIVERABLES_DIR = ROOT / "deliverables"
DELIVERABLE_PDF = DELIVERABLES_DIR / "chain_report.pdf"
DELIVERABLE_XLSX = DELIVERABLES_DIR / "chain_ledger.xlsx"


def find_raw_xlsx() -> Path | None:
    """First existing raw-workbook location, or None if neither exists."""
    for candidate in (RAW_XLSX, SIBLING_RAW_XLSX):
        if candidate.exists():
            return candidate
    return None
