"""Scenario / shock what-if tests (scenario.py).

The heart of the repo is the reconciliation harness; the heart of THESE tests is
that it stays green under a shock. Each test reuses the session-scoped fixture
chain (no extra CVRP solve) and asserts: the perturbation is pure and scoped, it
traces to the ledger exactly, the physical boundary does not move on a demand
shock, and ALL 13 identities still hold under every perturbed run.
"""

from __future__ import annotations

import pytest

import scenario as scen
from chain import costing
from chain.contracts import Provenance


@pytest.fixture(scope="module")
def baseline(stage0, stage1, layers, stage2, stage3, stage4, stage5, stage6, expected):
    cleaned, demand, stream = stage0
    return scen.Baseline(
        source="fixture",
        expected_revenue_gbp=expected["cleaned_revenue_gbp"],
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
    )


def _delta(result: scen.ScenarioResult, metric: str) -> scen.StageDelta:
    for d in result.deltas:
        if d.metric == metric:
            return d
    raise KeyError(metric)


# --------------------------------------------------------------------------- #
# Baseline sanity + the perturbation itself
# --------------------------------------------------------------------------- #
def test_baseline_identities_all_pass(baseline):
    _, checks = scen.build_ledger_and_checks(
        baseline.cleaned, baseline.demand, baseline.stream, baseline.fc, baseline.plan,
        baseline.wh, baseline.ful, baseline.dl, baseline.cs, baseline.expected_revenue_gbp,
    )
    assert len(checks) == 13
    assert all(c.passed for c in checks), [c.line() for c in checks if not c.passed]


def test_apply_demand_surge_is_pure_and_scoped(baseline):
    target = scen.most_populous_class(baseline.fc)
    before_units = baseline.fc.forecasts["Units"].copy()
    fc2 = scen.apply_demand_surge(baseline.fc, target, 1.20)
    # the input frame is NOT mutated
    assert baseline.fc.forecasts["Units"].equals(before_units)
    # SKUs / rows preserved (forecast coverage identity stays intact)
    assert set(fc2.forecasts["StockCode"]) == set(baseline.fc.forecasts["StockCode"])
    assert len(fc2.forecasts) == len(baseline.fc.forecasts)
    # only the target class moved; other classes are byte-identical
    other = baseline.fc.forecasts["Class"] != target
    assert fc2.forecasts.loc[other, "Units"].equals(baseline.fc.forecasts.loc[other, "Units"])
    assert fc2.forecasts.loc[other, "Sigma"].equals(baseline.fc.forecasts.loc[other, "Sigma"])


def test_demand_surge_scales_target_class_exactly(baseline):
    target = scen.most_populous_class(baseline.fc)
    fc2 = scen.apply_demand_surge(baseline.fc, target, 1.20)
    base_units = scen._fc_units_class(baseline.fc, target)
    pert_units = scen._fc_units_class(fc2, target)
    assert pert_units == pytest.approx(1.20 * base_units, rel=1e-12)


# --------------------------------------------------------------------------- #
# Scenario 1 -- demand surge traces to the ledger, boundary intact, identities hold
# --------------------------------------------------------------------------- #
def test_demand_surge_traces_to_ledger(baseline):
    res = scen.run_demand_surge(baseline)
    # the shock reaches the ledger through holding, and only through holding
    holding = _delta(res, "holding cost")
    total = _delta(res, "total cost")
    assert holding.delta > 0
    assert total.delta == pytest.approx(holding.delta, abs=1e-6)
    # the physical cost lines and the real revenue do NOT move
    assert _delta(res, "transport cost").delta == pytest.approx(0.0, abs=1e-9)
    assert _delta(res, "labour cost").delta == pytest.approx(0.0, abs=1e-9)
    assert _delta(res, "window revenue").delta == pytest.approx(0.0, abs=1e-9)
    # the safety-stock buffer rose (holding is a charge on it)
    assert _delta(res, "safety stock (total)").delta > 0


def test_demand_surge_safety_stock_of_surged_skus_scales(baseline):
    target = scen.most_populous_class(baseline.fc)
    fc2 = scen.apply_demand_surge(baseline.fc, target, 1.20)
    from chain import inventory

    plan2 = inventory.run(fc2, baseline.layers)
    surged = set(
        baseline.fc.forecasts.loc[baseline.fc.forecasts["Class"] == target, "StockCode"]
    )
    base_ss = baseline.plan.plan.set_index("StockCode")["SafetyStock"]
    pert_ss = plan2.plan.set_index("StockCode")["SafetyStock"]
    for sku in surged:
        if base_ss.get(sku, 0.0) > 0:
            assert pert_ss[sku] == pytest.approx(1.20 * base_ss[sku], rel=1e-9)


def test_demand_surge_all_13_identities_hold(baseline):
    res = scen.run_demand_surge(baseline)
    assert len(res.perturbed_checks) == 13
    assert res.identities_hold
    assert res.identities_passed == 13
    names = " ".join(c.name for c in res.perturbed_checks)
    for letter in "abcdefghijklm":
        assert f"({letter})" in names, f"identity ({letter}) missing under perturbation"


def test_demand_surge_preserves_provenance_boundary(baseline):
    res = scen.run_demand_surge(baseline)
    assert res.input_provenance is Provenance.DERIVED  # forecast is derived, not real
    assert _delta(res, "window revenue").provenance is Provenance.REAL
    for metric in ("holding cost", "transport cost", "total cost"):
        assert _delta(res, metric).provenance is Provenance.SYNTHETIC_ASSIGNED


# --------------------------------------------------------------------------- #
# Scenario 2 -- cost-rate shock
# --------------------------------------------------------------------------- #
def test_cost_rate_shock_traces_and_holds(baseline):
    res = scen.run_cost_rate_shock(baseline, factor=1.20)
    transport = _delta(res, "transport cost")
    total = _delta(res, "total cost")
    assert transport.pct == pytest.approx(20.0, abs=1e-6)
    assert total.delta == pytest.approx(transport.delta, abs=1e-6)  # only transport moved
    assert _delta(res, "holding cost").delta == pytest.approx(0.0, abs=1e-9)
    assert _delta(res, "CVRP route km").delta == pytest.approx(0.0, abs=1e-9)  # km unchanged
    assert _delta(res, "window revenue").delta == pytest.approx(0.0, abs=1e-9)
    assert len(res.perturbed_checks) == 13
    assert res.identities_hold


def test_cost_rate_shock_restores_module_constant(baseline):
    original = costing.TRANSPORT_RATE_GBP_PER_KM
    scen.run_cost_rate_shock(baseline, factor=1.20)
    assert costing.TRANSPORT_RATE_GBP_PER_KM == original  # no leaked monkeypatch


# --------------------------------------------------------------------------- #
# Deliverables are deterministic
# --------------------------------------------------------------------------- #
def test_emit_is_byte_deterministic(baseline, tmp_path):
    results = scen.run_all(baseline)
    csv1 = scen.write_csv(results, tmp_path / "a.csv").read_bytes()
    csv2 = scen.write_csv(results, tmp_path / "b.csv").read_bytes()
    assert csv1 == csv2
    csv1.decode("ascii")  # committed deliverable stays ASCII/diffable
    md1 = scen.write_markdown(results, "src", tmp_path / "a.md").read_bytes()
    md2 = scen.write_markdown(results, "src", tmp_path / "b.md").read_bytes()
    assert md1 == md2


def test_run_all_covers_both_kinds(baseline):
    results = scen.run_all(baseline)
    assert {r.kind for r in results} == {"demand", "cost-rate"}
    assert all(r.identities_hold for r in results)
