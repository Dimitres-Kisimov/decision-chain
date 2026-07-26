"""Shared fixtures.

All tests run on tests/fixtures/sample.csv — real rows drawn deterministically
(seed 42) from the full dataset by scripts/make_fixture.py, stratified to
include cancellations, non-product codes, zero prices, missing CustomerIDs,
real duplicate groups AND ten dense-history SKUs so the forecast stage has
real rolling-origin folds. Tests that need the full ~1M-row dataset skip
themselves when no raw data is on disk.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from chain import forecast as forecast_stage
from chain import ingest, paths
from chain.contracts import CleanedTransactions, DemandForecast, InvoiceStream, WeeklyDemand

N_SKUS_FIXTURE = 25

requires_raw_data = pytest.mark.skipif(
    paths.find_raw_xlsx() is None and not paths.INTERIM_PARQUET.exists(),
    reason="full raw dataset not on disk (fixture-based CI run)",
)


@pytest.fixture(scope="session")
def raw_fixture() -> pd.DataFrame:
    return ingest.load_fixture()


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads(paths.FIXTURE_EXPECTED.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def stage0(raw_fixture: pd.DataFrame) -> tuple[CleanedTransactions, WeeklyDemand, InvoiceStream]:
    return ingest.run(raw_fixture, n_skus=N_SKUS_FIXTURE)


@pytest.fixture(scope="session")
def stage1(stage0) -> DemandForecast:
    _, demand, _ = stage0
    return forecast_stage.run(demand)
