"""Stage 2: hand-checked base-stock math, sqrt-law behavior, coverage, labelling."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from chain import inventory, synthetic
from chain.contracts import DemandForecast, Provenance
from chain.synthetic import SyntheticLayers


def _forecast(rows: list[dict]) -> DemandForecast:
    """Crafted DemandForecast: rows = [{sku, mu, sigma, cls}]; constant mu per week."""
    fc_rows = []
    for r in rows:
        for i in range(4):
            fc_rows.append(
                {
                    "StockCode": r["sku"],
                    "Week": pd.Timestamp("2012-01-01") + pd.Timedelta(weeks=i),
                    "Units": r["mu"],
                    "Sigma": r["sigma"],
                    "Model": "naive",
                    "Class": r.get("cls", "smooth"),
                }
            )
    return DemandForecast(
        forecasts=pd.DataFrame(fc_rows),
        cv=pd.DataFrame(),
        class_winners=pd.DataFrame(),
        horizon_weeks=4,
    )


def _layers(lead_by_sku: dict[str, int]) -> SyntheticLayers:
    """Crafted layers with exact lead times (still labelled synthetic)."""
    leads = pd.DataFrame(
        [{"StockCode": s, "Class": "smooth", "LeadTimeWeeks": lt} for s, lt in lead_by_sku.items()]
    )
    return SyntheticLayers(
        sku_physical=pd.DataFrame(),
        lead_times=leads,
        geometry=synthetic.make_geometry(len(lead_by_sku)),
        seed=42,
    )


def test_base_stock_hand_check():
    """mu=10, sigma=2, L=3, review=1 -> P=4; z(.95)=1.6449.

    SS = z * sqrt(4) * 2 = 6.5794; S = 40 + SS; ROP = 30 + z * sqrt(3) * 2.
    """
    plan = inventory.run(
        _forecast([{"sku": "X", "mu": 10.0, "sigma": 2.0}]), _layers({"X": 3})
    ).plan
    z = inventory.z_score(0.95)
    row = plan.iloc[0]
    assert row["SafetyStock"] == pytest.approx(z * 2.0 * 2.0, rel=1e-9)
    assert row["OrderUpTo"] == pytest.approx(40.0 + z * 4.0, rel=1e-9)
    assert row["ReorderPoint"] == pytest.approx(30.0 + z * math.sqrt(3) * 2.0, rel=1e-9)
    assert row["OrderQty"] == row["OrderUpTo"]  # documented zero on-hand start


def test_sqrt_law_quadruple_lead_doubles_buffer():
    for t in (1.0, 2.5, 6.0):
        assert inventory.safety_stock(3.0, 4 * t, 0.95) == pytest.approx(
            2 * inventory.safety_stock(3.0, t, 0.95), rel=1e-12
        )


def test_higher_service_level_costs_more_buffer():
    fc = _forecast([{"sku": "X", "mu": 10.0, "sigma": 2.0}])
    lay = _layers({"X": 3})
    stocks = [
        float(inventory.run(fc, lay, service_level=sl).plan["SafetyStock"].iloc[0])
        for sl in (0.90, 0.95, 0.99)
    ]
    assert stocks[0] < stocks[1] < stocks[2]


def test_wide_sigma_means_wide_buffer():
    """Same demand, 10x the measured uncertainty -> exactly 10x the safety stock."""
    plan = inventory.run(
        _forecast(
            [
                {"sku": "CALM", "mu": 10.0, "sigma": 1.0},
                {"sku": "WILD", "mu": 10.0, "sigma": 10.0, "cls": "lumpy"},
            ]
        ),
        _layers({"CALM": 3, "WILD": 3}),
    ).plan.set_index("StockCode")
    assert plan.loc["WILD", "SafetyStock"] == pytest.approx(
        10 * plan.loc["CALM", "SafetyStock"], rel=1e-9
    )
    summary = inventory.class_summary(
        inventory.run(
            _forecast(
                [
                    {"sku": "CALM", "mu": 10.0, "sigma": 1.0},
                    {"sku": "WILD", "mu": 10.0, "sigma": 10.0, "cls": "lumpy"},
                ]
            ),
            _layers({"CALM": 3, "WILD": 3}),
        )
    ).set_index("Class")
    assert summary.loc["lumpy", "SafetyStockWeeks"] > summary.loc["smooth", "SafetyStockWeeks"]


def test_fixture_plan_covers_forecast_and_is_labelled(stage1, stage2):
    assert set(stage2.plan["StockCode"]) == set(stage1.forecasts["StockCode"])
    assert stage2.provenance is Provenance.SYNTHETIC_ASSIGNED  # derive(DERIVED, SYNTH)
    assert "synthetic-assigned" in str(stage2.assumptions["lead_times"])
    assert stage2.assumptions["service_level"] == inventory.SERVICE_LEVEL
    assert (stage2.plan["SafetyStock"] >= 0).all()
    assert (stage2.plan["OrderUpTo"] >= stage2.plan["SafetyStock"]).all()
