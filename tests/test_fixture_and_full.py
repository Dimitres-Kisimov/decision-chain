"""Fixture integrity + full-dataset tests (the latter skip without raw data)."""

from __future__ import annotations

import hashlib

from chain import ingest, paths, reconcile
from chain.__main__ import run_report
from tests.conftest import requires_raw_data


def test_fixture_file_hash_matches_expected(expected):
    sha = hashlib.sha256(paths.FIXTURE_CSV.read_bytes()).hexdigest()
    assert sha == expected["sha256"], "fixture file drifted from expected.json"


def test_fixture_contains_the_documented_mess(raw_fixture, expected):
    assert raw_fixture["Invoice"].str.startswith("C").sum() > 0
    assert ingest.is_non_product(raw_fixture["StockCode"]).sum() > 0
    assert (raw_fixture["Price"] <= 0).sum() > 0
    assert raw_fixture["CustomerID"].isna().sum() > 0
    assert raw_fixture.duplicated(subset=ingest.DEDUP_COLUMNS).sum() > 0
    assert len(raw_fixture) == expected["rows"]


def test_fixture_report_runs_end_to_end(capsys):
    assert run_report(fixture=True) is True
    out = capsys.readouterr().out
    assert "ALL IDENTITY CHECKS PASSED" in out
    assert "[real]" in out and "[derived]" in out


@requires_raw_data
def test_fixture_regeneration_is_deterministic(expected):
    import sys

    sys.path.insert(0, str(paths.ROOT / "scripts"))
    import make_fixture

    regenerated = make_fixture.build_fixture(ingest.load_raw())
    assert len(regenerated) == expected["rows"]
    csv_bytes = regenerated.to_csv(index=False, lineterminator="\n").encode("utf-8")
    assert hashlib.sha256(csv_bytes).hexdigest() == expected["sha256"]


@requires_raw_data
def test_full_data_revenue_identity_to_the_penny():
    cleaned = ingest.clean(ingest.load_raw())
    assert abs(cleaned.revenue_gbp - reconcile.PUBLISHED_REVENUE_GBP) <= reconcile.PENNY
