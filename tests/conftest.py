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

from chain import artifact as artifact_mod
from chain import (
    costing,
    deliver,
    fulfil,
    ingest,
    inventory,
    paths,
    reconcile,
    synthetic,
    warehouse,
)
from chain import forecast as forecast_stage
from chain.contracts import (
    CleanedTransactions,
    DemandForecast,
    FulfilmentLog,
    InvoiceStream,
    ReplenishmentPlan,
    WeeklyDemand,
)
from chain.costing import CostingResult
from chain.deliver import DeliveryResult
from chain.synthetic import SyntheticLayers
from chain.warehouse import WarehouseResult

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


@pytest.fixture(scope="session")
def layers(stage0, stage1) -> SyntheticLayers:
    cleaned, demand, _ = stage0
    descriptions = synthetic.modal_descriptions(cleaned.sales, demand.skus)
    classes = (
        stage1.forecasts.drop_duplicates("StockCode").set_index("StockCode")["Class"].to_dict()
    )
    return synthetic.build(demand.skus, descriptions, classes)


@pytest.fixture(scope="session")
def stage2(stage1, layers) -> ReplenishmentPlan:
    return inventory.run(stage1, layers)


@pytest.fixture(scope="session")
def stage3(stage0, layers) -> WarehouseResult:
    _, _, stream = stage0
    return warehouse.run(stream, layers)


@pytest.fixture(scope="session")
def stage4(stage0, stage3, layers) -> FulfilmentLog:
    _, demand, stream = stage0
    return fulfil.simulate(stream, stage3.workload, layers, demand.weeks)


@pytest.fixture(scope="session")
def stage5(stage0, stage4) -> DeliveryResult:
    cleaned, _, _ = stage0
    return deliver.run(cleaned, stage4)


@pytest.fixture(scope="session")
def stage6(stage0, stage2, stage4, stage5) -> CostingResult:
    cleaned, demand, _ = stage0
    return costing.run(cleaned, demand.skus, stage2, stage4, stage5)


@pytest.fixture(scope="session")
def full_ledger_and_checks(stage0, stage1, stage2, stage3, stage4, stage5, stage6, expected):
    """The complete stage-6 registration + all 13 identity checks (fixture data)."""
    cleaned, demand, stream = stage0
    ledger = reconcile.Ledger()
    reconcile.register_stage0(ledger, cleaned, demand, stream)
    reconcile.register_stage1(ledger, stage1)
    reconcile.register_stage2(ledger, stage2)
    reconcile.register_stage3(ledger, stage3)
    reconcile.register_stage4(ledger, stream, stage4, stage5)
    reconcile.register_stage5(ledger, stage6)
    checks = reconcile.run_phase1_checks(ledger, stage1, demand, expected["cleaned_revenue_gbp"])
    checks += reconcile.run_phase2_checks(ledger, stage2, stage1, stage3)
    checks += reconcile.run_phase3_checks(ledger, cleaned, demand, stage4)
    return ledger, checks


@pytest.fixture(scope="session")
def fixture_artifact(
    raw_fixture, stage0, stage1, layers, stage2, stage3, stage4, stage5, stage6,
    full_ledger_and_checks,
) -> dict:
    """A run artifact built from the fixture chain (what --save-artifact serializes)."""
    cleaned, demand, stream = stage0
    ledger, checks = full_ledger_and_checks
    return artifact_mod.build(
        source="fixture",
        raw_rows=len(raw_fixture),
        cleaned=cleaned,
        demand=demand,
        stream=stream,
        fc=stage1,
        layers=layers,
        plan=stage2,
        wh=stage3,
        ful=stage4,
        dl=stage5,
        cs=stage6,
        ledger=ledger,
        checks=checks,
    )


@pytest.fixture(scope="session")
def fixture_artifact_path(fixture_artifact, tmp_path_factory):
    """The fixture artifact saved to disk (what the dashboard/exports consume)."""
    path = tmp_path_factory.mktemp("artifact") / "fixture_run.json"
    return artifact_mod.save(fixture_artifact, path)
