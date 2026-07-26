"""Stage 0 on the fixture: cleaning behavior, determinism, stage-output shape."""

from __future__ import annotations

import pandas as pd

from chain import ingest
from tests.conftest import N_SKUS_FIXTURE


def test_cleaning_removes_the_documented_mess(stage0):
    cleaned, _, _ = stage0
    sales = cleaned.sales
    assert not sales["Invoice"].str.startswith("C").any()
    assert not ingest.is_non_product(sales["StockCode"]).any()
    assert (sales["Price"] > 0).all()
    assert (sales["Quantity"] > 0).all()
    assert not sales.duplicated(subset=ingest.DEDUP_COLUMNS).any()
    # missing CustomerID rows are flagged, never dropped
    assert (~sales["KnownCustomer"]).sum() > 0
    assert sales["Revenue"].equals(sales["Quantity"].astype("float64") * sales["Price"])


def test_cleaning_matches_expected_json(stage0, expected):
    cleaned, _, _ = stage0
    assert len(cleaned.sales) == expected["cleaned_sales_rows"]
    assert len(cleaned.returns) == expected["returns_rows"]
    assert round(cleaned.revenue_gbp, 2) == expected["cleaned_revenue_gbp"]


def test_ingest_is_deterministic(raw_fixture):
    a_cleaned, a_demand, a_stream = ingest.run(raw_fixture, n_skus=N_SKUS_FIXTURE)
    b_cleaned, b_demand, b_stream = ingest.run(raw_fixture.copy(), n_skus=N_SKUS_FIXTURE)
    pd.testing.assert_frame_equal(a_cleaned.sales, b_cleaned.sales)
    pd.testing.assert_frame_equal(a_demand.demand, b_demand.demand)
    pd.testing.assert_frame_equal(a_stream.lines, b_stream.lines)
    assert a_demand.skus == b_demand.skus


def test_tracked_skus_are_top_by_revenue(stage0):
    cleaned, demand, _ = stage0
    assert len(demand.skus) == N_SKUS_FIXTURE
    by_revenue = cleaned.sales.groupby("StockCode", observed=True)["Revenue"].sum()
    tracked_min = min(by_revenue[sku] for sku in demand.skus)
    untracked = by_revenue.drop(index=demand.skus)
    assert untracked.max() <= tracked_min


def test_weekly_demand_series_shape(stage0):
    _, demand, _ = stage0
    assert set(demand.demand.columns) == {"StockCode", "Week", "Units"}
    assert (demand.demand["Units"] >= 0).all()
    # every demand week lies inside the declared (partial-edge-free) index
    assert demand.demand["Week"].isin(demand.weeks).all()
    # series() trims leading zeros and stays on the weekly grid
    sku = demand.skus[0]
    series = demand.series(sku)
    assert series.iloc[0] != 0
    assert series.index.isin(demand.weeks).all()


def test_invoice_stream_lines_reference_tracked_skus(stage0):
    _, demand, stream = stage0
    assert stream.lines["StockCode"].isin(demand.skus).all()
    assert (stream.lines["Quantity"] > 0).all()
    assert stream.n_invoices <= stream.n_lines
