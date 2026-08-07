"""Inter-stage sensitivity tests (sensitivity.py) -- identity (o) and containment.

The reconciliation harness is the heart of the repo; the heart of THESE tests is
that a forecast error is provably CONTAINED. Each test reuses the session-scoped
fixture chain (no extra CVRP solve -- ``analyse`` re-runs only inventory + costing
on the reused physical stages) and asserts, with hand-checked expectations on the
committed real-row fixture: the whole-book surge is pure and scoped; the safety
stock scales linearly; the physical delivered-cost lines and the real revenue do
NOT move; identity (o) holds (total moves only by the holding line); the
elasticity of total equals the holding share; the single-class dilution law
reconstructs the measured elasticity; every corruption has a FAIL path; all 13
identities hold at every grid factor; and the deliverables are byte-deterministic.

Hand-checked fixture constants (python sensitivity.py on tests/fixtures/sample.csv):
  base holding 467.185180 GBP, base total 45,920.633108 GBP  -> holding share 0.01017375
  base safety stock 2,919.907377 u ; total @ x1.20 = 46,014.070144 GBP
  most-populous class = erratic, carrying 0.71888625 of total safety stock
"""

from __future__ import annotations

import pytest

import sensitivity as sens
from chain import artifact as artifact_mod
from chain import paths
from chain.reconcile import PENNY

# Hand-checked on the committed fixture (see module docstring).
BASE_HOLDING = 467.185180
BASE_TOTAL = 45920.633108
BASE_SAFETY_STOCK = 2919.907377
HOLDING_SHARE = 0.0101737530
TOTAL_AT_120 = 46014.070144
ERRATIC_SS_FRACTION = 0.7188862454
COMMITTED_FULL_RUN_HOLDING_SHARE = 0.0580064184  # holding/total on artifacts/full_run.json


@pytest.fixture(scope="module")
def baseline(stage0, stage1, layers, stage2, stage3, stage4, stage5, stage6, expected):
    """A scenario.Baseline built from the session fixtures (no fresh CVRP solve)."""
    cleaned, demand, stream = stage0
    return sens.Baseline(
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


@pytest.fixture(scope="module")
def analysis(baseline) -> sens.Sensitivity:
    return sens.analyse(baseline)


# --------------------------------------------------------------------------- #
# The perturbation itself
# --------------------------------------------------------------------------- #
def test_apply_book_surge_is_pure_and_scoped(baseline):
    before_units = baseline.fc.forecasts["Units"].copy()
    before_sigma = baseline.fc.forecasts["Sigma"].copy()
    fc2 = sens.apply_book_surge(baseline.fc, 1.20)
    # the input frame is NOT mutated
    assert baseline.fc.forecasts["Units"].equals(before_units)
    assert baseline.fc.forecasts["Sigma"].equals(before_sigma)
    # SKUs / rows preserved (forecast coverage identity stays intact)
    assert set(fc2.forecasts["StockCode"]) == set(baseline.fc.forecasts["StockCode"])
    assert len(fc2.forecasts) == len(baseline.fc.forecasts)
    # EVERY SKU scaled (whole book), both point and uncertainty, by the factor exactly
    assert fc2.forecasts["Units"].to_numpy() == pytest.approx(
        1.20 * before_units.to_numpy(), rel=1e-12
    )
    assert fc2.forecasts["Sigma"].to_numpy() == pytest.approx(
        1.20 * before_sigma.to_numpy(), rel=1e-12
    )


# --------------------------------------------------------------------------- #
# The grid + linear propagation
# --------------------------------------------------------------------------- #
def test_grid_covers_the_factor_ladder_with_a_base(analysis):
    assert [p.factor for p in analysis.grid] == list(sens.FACTORS)
    assert analysis.base.factor == sens.BASE_FACTOR
    # hand-checked base ledger numbers on the fixture
    assert analysis.base.holding_gbp == pytest.approx(BASE_HOLDING, abs=1e-3)
    assert analysis.base.total_gbp == pytest.approx(BASE_TOTAL, abs=1e-3)


def test_safety_stock_scales_linearly_with_the_surge(analysis):
    base_ss = analysis.base.total_ss
    assert base_ss == pytest.approx(BASE_SAFETY_STOCK, abs=1e-3)
    for p in analysis.grid:
        assert p.total_ss == pytest.approx(p.factor * base_ss, rel=1e-9)


def test_surge_moves_only_the_holding_line(analysis):
    """The physical delivered-cost lines and the real revenue are invariant to
    the cent; total moves by exactly the holding delta (identity (o)'s content)."""
    base = analysis.base
    for p in analysis.grid:
        if p.factor == sens.BASE_FACTOR:
            continue
        # invariant: the surge cannot reach the immutable real invoice stream
        assert p.transport_gbp == pytest.approx(base.transport_gbp, abs=PENNY)
        assert p.labour_gbp == pytest.approx(base.labour_gbp, abs=PENNY)
        assert p.facility_gbp == pytest.approx(base.facility_gbp, abs=PENNY)
        assert p.window_revenue_gbp == pytest.approx(base.window_revenue_gbp, abs=PENNY)
        # holding DID move, and total tracks it exactly
        assert p.holding_gbp != pytest.approx(base.holding_gbp, abs=PENNY)
        predicted_total = base.total_gbp + (p.factor - base.factor) * base.holding_gbp
        assert p.total_gbp == pytest.approx(predicted_total, abs=PENNY)


# --------------------------------------------------------------------------- #
# Identity (o)
# --------------------------------------------------------------------------- #
def test_identity_o_holds_on_fixture(analysis):
    o = analysis.identity_o
    assert o.name == "(o) forecast-error containment"
    assert o.unit == "GBP"
    assert o.passed, o.note
    assert o.lhs == pytest.approx(TOTAL_AT_120, abs=1e-2)
    assert abs(o.lhs - o.rhs) <= PENNY


def test_elasticity_of_total_equals_holding_share(analysis):
    share = analysis.holding_share
    assert share == pytest.approx(HOLDING_SHARE, abs=1e-6)
    # the ANALYTIC elasticity the module reports is the share
    assert analysis.elasticity_of_total == pytest.approx(share, abs=1e-12)
    # ... and the MEASURED arc-elasticity equals it at every off-base grid factor
    for p in analysis.grid:
        e = sens.arc_elasticity(analysis.base.total_gbp, p.total_gbp, p.factor)
        if p.factor == sens.BASE_FACTOR:
            assert e is None
        else:
            assert e == pytest.approx(share, abs=1e-9)


def test_all_13_identities_hold_at_every_grid_factor(analysis):
    for p in analysis.grid:
        assert p.identities_total == 13, p.factor
        assert p.identities_hold, f"factor x{p.factor}: {p.identities_passed}/13"
    assert analysis.all_hold()


# --------------------------------------------------------------------------- #
# Dilution corollary -- a single-class surge is contained further
# --------------------------------------------------------------------------- #
def test_dilution_corollary_reconstructs_the_measured_elasticity(analysis):
    d = analysis.dilution
    assert d.target_class == "erratic"
    assert d.class_ss_fraction == pytest.approx(ERRATIC_SS_FRACTION, abs=1e-6)
    # measured single-class elasticity == holding share x class SS fraction, exactly
    assert d.predicted_elasticity == pytest.approx(
        d.holding_share * d.class_ss_fraction, abs=1e-12
    )
    assert d.measured_elasticity == pytest.approx(d.predicted_elasticity, abs=1e-9)
    # a minority-buffer class dilutes the pound impact below the whole-book elasticity
    assert d.measured_elasticity < analysis.holding_share
    assert d.identities_hold
    assert d.holds


# --------------------------------------------------------------------------- #
# Deliberate-corruption FAIL paths for identity (o)
# --------------------------------------------------------------------------- #
def test_identity_o_fails_if_a_physical_line_moves(analysis):
    """If a delivered-cost line moved under a forecast surge, containment is false."""
    import dataclasses

    grid = [dataclasses.replace(p) for p in analysis.grid]
    # pretend the x1.20 point's transport rose (it must not, on a forecast shock)
    top = max(grid, key=lambda p: p.factor)
    top.transport_gbp = top.transport_gbp + 100.0
    top.total_gbp = top.total_gbp + 100.0  # keep additivity (l)-style consistency
    check = sens.check_forecast_containment(analysis.base, grid)
    assert not check.passed
    assert "transport" in check.note


def test_identity_o_fails_if_total_is_nonlinear(analysis):
    import dataclasses

    grid = [dataclasses.replace(p) for p in analysis.grid]
    top = max(grid, key=lambda p: p.factor)
    top.total_gbp = top.total_gbp + 1.0  # a one-pound drift off the linear prediction
    check = sens.check_forecast_containment(analysis.base, grid)
    assert not check.passed
    assert "total" in check.note


def test_identity_o_fails_if_an_identity_breaks(analysis):
    import dataclasses

    grid = [dataclasses.replace(p) for p in analysis.grid]
    top = max(grid, key=lambda p: p.factor)
    top.identities_passed = top.identities_total - 1  # one seam broke under the shock
    check = sens.check_forecast_containment(analysis.base, grid)
    assert not check.passed
    assert "identities" in check.note


# --------------------------------------------------------------------------- #
# The committed full-run artifact predicts the same containment
# --------------------------------------------------------------------------- #
def test_committed_artifact_predicts_the_containment():
    """Identity (o) is analytic in the holding share, which the committed full-run
    artifact carries directly -- so the containment is stated on the receipt too,
    without re-running the 51-minute chain. (o) is additive to the recorded 13."""
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    vals = {row["key"]: float(row["value"]) for row in artifact["ledger"]}
    share = vals["costing/holding"] / vals["costing/total_cost"]
    assert 0.0 < share < 1.0
    assert share == pytest.approx(COMMITTED_FULL_RUN_HOLDING_SHARE, abs=1e-6)
    # the artifact records exactly the 13 a-m identities; (o) is additive, not stored
    assert artifact["identities_total"] == 13
    assert "(o) forecast-error containment" not in {i["name"] for i in artifact["identities"]}


# --------------------------------------------------------------------------- #
# Deterministic emit + CLI
# --------------------------------------------------------------------------- #
def test_emit_is_byte_deterministic(analysis, tmp_path):
    csv1 = sens.write_csv(analysis, tmp_path / "a.csv").read_bytes()
    csv2 = sens.write_csv(analysis, tmp_path / "b.csv").read_bytes()
    assert csv1 == csv2
    csv1.decode("ascii")  # committed deliverable stays ASCII/diffable
    md1 = sens.write_markdown(analysis, "src", tmp_path / "a.md").read_bytes()
    md2 = sens.write_markdown(analysis, "src", tmp_path / "b.md").read_bytes()
    assert md1 == md2
    md1.decode("ascii")


def test_cli_runs_on_fixture_and_holds(baseline, tmp_path, monkeypatch, capsys):
    # reuse the already-built session baseline (no fresh CVRP solve in the test)
    monkeypatch.setattr(sens, "build_baseline", lambda fixture=True: baseline)
    monkeypatch.setattr(sens, "SENSITIVITY_CSV", tmp_path / "sensitivity.csv")
    monkeypatch.setattr(sens, "SENSITIVITY_MD", tmp_path / "sensitivity.md")
    rc = sens.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(o) forecast-error containment" in out
    assert "[PASS]" in out
    assert (tmp_path / "sensitivity.csv").exists()
    assert (tmp_path / "sensitivity.md").exists()
