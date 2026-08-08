"""Per-order cost-to-serve tests (order_costs.py) -- identity (p) and the distribution.

The heart of these tests is ALLOCATION CONSERVATION: the ledger must survive
being spread over the real orders and reassemble to the cent, on both planes
(order plane: labour/transport/facility; SKU plane: holding). Each test reuses
the session-scoped fixture chain (no extra CVRP solve -- ``analyse`` only reads
stage 5's own frames) and asserts, with hand-checked expectations on the
committed real-row fixture: the frame is pure with one row per shipped order;
every cost column reassembles to its registered ledger line to the cent; the
facility equal-split is exact; identity (p) holds; the whale curve reconciles
(endpoint == revenue - delivered, exactly) and is concave; the decile table
reconciles to the cent; every corruption (a lost order, a phantom order, a
duplicated row, a cent of drift, a NaN, a holding mismatch) has a FAIL path;
all 13 identities hold on the same run; and the deliverables are
byte-deterministic.

Hand-checked fixture constants (python order_costs.py on tests/fixtures/sample.csv):
  320 orders; delivered (order plane) 45,453.447928 GBP; holding (SKU plane)
  467.185180 GBP; total 45,920.633108 GBP; window revenue 14,545.030000 GBP.
  facility 20,000 / 320 orders = 62.50 GBP per order EXACTLY.
  whale: peak 3,124.237367 GBP after 8 orders; end -30,908.417928
  == 14,545.030000 - 45,453.447928 exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import order_costs as oc
from chain import artifact as artifact_mod
from chain import paths
from chain.reconcile import PENNY

# Hand-checked on the committed fixture (see module docstring).
N_ORDERS = 320
DELIVERED_TOTAL = 45453.447928
REVENUE_TOTAL = 14545.030000
BASE_TOTAL = 45920.633108
BASE_HOLDING = 467.185180
FACILITY_PER_ORDER = 62.50  # 20,000.00 GBP / 320 orders, exact
MEAN_GBP = 142.042025
MEDIAN_GBP = 108.915047
P90_GBP = 267.207112
MAX_GBP = 1193.371964
TOP_DECILE_SHARE = 0.27735971
GINI_DELIVERED = 0.30226301
UNCOVERED_ORDERS = 312
WHALE_PEAK = 3124.237367
WHALE_PEAK_ORDERS = 8
WHALE_END = -30908.417928
DECILE1_ORDERS = 32
DECILE1_DELIVERED = 12606.955174
DECILE1_UNCOVERED = 28


@pytest.fixture(scope="module")
def baseline(stage0, stage1, layers, stage2, stage3, stage4, stage5, stage6, expected):
    """A scenario.Baseline built from the session fixtures (no fresh CVRP solve)."""
    cleaned, demand, stream = stage0
    return oc.Baseline(
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
def analysis(baseline) -> oc.OrderCosts:
    return oc.analyse(baseline)


# --------------------------------------------------------------------------- #
# The per-order frame itself
# --------------------------------------------------------------------------- #
def test_order_frame_is_pure_with_one_row_per_shipped_order(baseline, analysis):
    # the stage-5 contract frame is NOT mutated (order_frame works on a copy)
    assert "DeliveredCost" not in baseline.cs.cost.per_invoice.columns
    assert "Gap" not in baseline.cs.cost.per_invoice.columns
    # exactly one economics row per order the DES shipped
    shipped = set(baseline.ful.orders["Invoice"].astype(str))
    assert set(analysis.frame["Invoice"].astype(str)) == shipped
    assert len(analysis.frame) == len(shipped) == N_ORDERS


def test_delivered_cost_and_gap_are_exact_row_identities(analysis):
    frame = analysis.frame
    parts = frame["LabourCost"] + frame["TransportCost"] + frame["FacilityCost"]
    assert frame["DeliveredCost"].to_numpy() == pytest.approx(parts.to_numpy(), rel=1e-12)
    gap = frame["Revenue"] - frame["DeliveredCost"]
    assert frame["Gap"].to_numpy() == pytest.approx(gap.to_numpy(), rel=1e-12)


def test_facility_equal_split_is_exact(analysis):
    """20,000.00 GBP fixed / 320 orders = 62.50 per order, hand-checked, exact."""
    assert analysis.totals.facility_gbp == pytest.approx(20000.0, abs=1e-9)
    facility = analysis.frame["FacilityCost"].to_numpy()
    assert facility == pytest.approx(np.full(N_ORDERS, FACILITY_PER_ORDER), abs=1e-9)


# --------------------------------------------------------------------------- #
# Identity (p) -- allocation conservation, line by line and jointly
# --------------------------------------------------------------------------- #
def test_each_cost_line_reassembles_to_its_ledger_line_to_the_cent(analysis):
    frame, totals = analysis.frame, analysis.totals
    assert float(frame["LabourCost"].sum()) == pytest.approx(totals.labour_gbp, abs=PENNY)
    assert float(frame["TransportCost"].sum()) == pytest.approx(totals.transport_gbp, abs=PENNY)
    assert float(frame["FacilityCost"].sum()) == pytest.approx(totals.facility_gbp, abs=PENNY)
    # the SKU plane carries holding; the two planes jointly reassemble the total
    delivered = float(frame["DeliveredCost"].sum())
    assert delivered + totals.holding_gbp == pytest.approx(totals.total_gbp, abs=PENNY)
    # the REAL revenue survives the spread to the penny
    assert float(frame["Revenue"].sum()) == pytest.approx(totals.window_revenue_gbp, abs=PENNY)


def test_identity_p_holds_on_fixture(analysis):
    p = analysis.identity_p
    assert p.name == "(p) order-level allocation conservation"
    assert p.unit == "GBP"
    assert p.passed, p.note
    assert p.lhs == pytest.approx(BASE_TOTAL, abs=1e-3)
    assert abs(p.lhs - p.rhs) <= PENNY


def test_all_13_identities_hold_on_the_same_run(analysis):
    assert analysis.identities_total == 13
    assert analysis.identities_hold
    assert analysis.all_hold()


# --------------------------------------------------------------------------- #
# The distribution readouts (hand-checked)
# --------------------------------------------------------------------------- #
def test_hand_checked_distribution_stats(analysis):
    s = analysis.stats
    assert s.n_orders == N_ORDERS
    assert s.delivered_total_gbp == pytest.approx(DELIVERED_TOTAL, abs=1e-3)
    # delivered total is EXACTLY total minus the SKU-plane holding
    assert s.delivered_total_gbp == pytest.approx(
        analysis.totals.total_gbp - analysis.totals.holding_gbp, abs=1e-6
    )
    assert s.revenue_total_gbp == pytest.approx(REVENUE_TOTAL, abs=1e-3)
    assert s.mean_gbp == pytest.approx(MEAN_GBP, abs=1e-3)
    assert s.median_gbp == pytest.approx(MEDIAN_GBP, abs=1e-3)
    assert s.p90_gbp == pytest.approx(P90_GBP, abs=1e-3)
    assert s.max_gbp == pytest.approx(MAX_GBP, abs=1e-3)
    # no order can cost less than its equal facility share
    assert s.min_gbp >= FACILITY_PER_ORDER
    assert s.top_decile_cost_share == pytest.approx(TOP_DECILE_SHARE, abs=1e-6)
    assert 0.0 < s.gini_delivered < 1.0
    assert s.gini_delivered == pytest.approx(GINI_DELIVERED, abs=1e-6)


def test_uncovered_orders_are_counted_consistently(analysis):
    s = analysis.stats
    frame = analysis.frame
    recount = frame.loc[frame["DeliveredCost"] > frame["Revenue"]]
    assert s.uncovered_orders == len(recount) == UNCOVERED_ORDERS
    assert s.uncovered_revenue_gbp == pytest.approx(float(recount["Revenue"].sum()), abs=1e-9)
    assert 0.0 < s.uncovered_share <= 1.0


def test_whale_curve_reconciles_and_is_concave(analysis):
    w = analysis.whale
    assert w.n_orders == N_ORDERS
    # the endpoint IS the ledger: revenue - delivered, exactly
    assert w.end_gbp == pytest.approx(
        analysis.stats.revenue_total_gbp - analysis.stats.delivered_total_gbp, abs=1e-6
    )
    assert w.end_gbp == pytest.approx(WHALE_END, abs=1e-3)
    assert w.peak_gbp == pytest.approx(WHALE_PEAK, abs=1e-3)
    assert w.peak_orders == WHALE_PEAK_ORDERS
    assert w.peak_gbp == pytest.approx(float(w.cumulative_gbp.max()), abs=1e-9)
    assert w.erosion_gbp == pytest.approx(w.peak_gbp - w.end_gbp, abs=1e-9)
    # sorted best-covered first, the increments never increase -> concave curve
    increments = np.diff(w.cumulative_gbp, prepend=0.0)
    assert (np.diff(increments) <= 1e-9).all()


def test_decile_table_reconciles_to_the_cent(analysis):
    rows = analysis.deciles
    assert len(rows) == oc.N_DECILES
    assert sum(r.orders for r in rows) == N_ORDERS
    # the decomposition of the decomposition still adds up, to the cent
    assert sum(r.delivered_gbp for r in rows) == pytest.approx(
        analysis.stats.delivered_total_gbp, abs=PENNY
    )
    assert sum(r.revenue_gbp for r in rows) == pytest.approx(
        analysis.stats.revenue_total_gbp, abs=PENNY
    )
    assert sum(r.gap_gbp for r in rows) == pytest.approx(analysis.whale.end_gbp, abs=PENNY)
    # ranked costliest-first: per-decile delivered never increases
    delivered = [r.delivered_gbp for r in rows]
    assert delivered == sorted(delivered, reverse=True)
    # cumulative share climbs to exactly 100%
    shares = [r.cum_cost_share for r in rows]
    assert shares == sorted(shares)
    assert shares[-1] == pytest.approx(1.0, abs=1e-9)
    # hand-checked costliest decile
    top = rows[0]
    assert top.orders == DECILE1_ORDERS
    assert top.delivered_gbp == pytest.approx(DECILE1_DELIVERED, abs=1e-3)
    assert top.uncovered_orders == DECILE1_UNCOVERED
    assert top.cost_share == pytest.approx(TOP_DECILE_SHARE, abs=1e-9)


# --------------------------------------------------------------------------- #
# Edge cases on hand-built frames (whale, deciles, gini)
# --------------------------------------------------------------------------- #
def _tiny_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "Invoice": ["A", "B", "C"],
            "Revenue": [100.0, 50.0, 10.0],
            "LabourCost": [1.0, 2.0, 3.0],
            "TransportCost": [4.0, 5.0, 6.0],
            "FacilityCost": [10.0, 10.0, 10.0],
        }
    )
    frame["DeliveredCost"] = frame["LabourCost"] + frame["TransportCost"] + frame["FacilityCost"]
    frame["Gap"] = frame["Revenue"] - frame["DeliveredCost"]
    return frame


def test_whale_curve_on_a_tiny_hand_built_frame():
    """Gaps 85, 33, -9 -> cumulative 85, 118, 109: peak 118 after 2, end 109."""
    w = oc.whale_curve(_tiny_frame())
    assert w.cumulative_gbp.tolist() == pytest.approx([85.0, 118.0, 109.0], abs=1e-12)
    assert w.peak_gbp == pytest.approx(118.0, abs=1e-12)
    assert w.peak_orders == 2
    assert w.end_gbp == pytest.approx(109.0, abs=1e-12)  # 160 revenue - 51 delivered
    assert w.erosion_gbp == pytest.approx(9.0, abs=1e-12)


def test_deciles_on_a_tiny_hand_built_frame():
    """Three 1-order groups, costliest first: C(19), B(17), A(15); shares 19/17/15 of 51."""
    rows = oc.decile_table(_tiny_frame(), n_deciles=3)
    assert [r.orders for r in rows] == [1, 1, 1]
    assert [r.delivered_gbp for r in rows] == pytest.approx([19.0, 17.0, 15.0], abs=1e-12)
    assert [r.uncovered_orders for r in rows] == [1, 0, 0]  # only C: 19 > 10
    assert rows[0].cost_share == pytest.approx(19.0 / 51.0, abs=1e-12)
    assert rows[-1].cum_cost_share == pytest.approx(1.0, abs=1e-12)


def test_gini_hand_checks_and_degenerates():
    # [15, 17, 19]: 2*(1*15 + 2*17 + 3*19)/(3*51) - 4/3 = 8/153, by hand
    assert oc.gini(np.array([15.0, 17.0, 19.0])) == pytest.approx(8.0 / 153.0, abs=1e-12)
    # [0, 0, 10]: 2*(3*10)/(3*10) - 4/3 = 2/3, by hand (all cost on one order)
    assert oc.gini(np.array([0.0, 0.0, 10.0])) == pytest.approx(2.0 / 3.0, abs=1e-12)
    assert oc.gini(np.array([5.0, 5.0, 5.0])) == pytest.approx(0.0, abs=1e-12)
    assert oc.gini(np.array([])) == 0.0


# --------------------------------------------------------------------------- #
# Deliberate-corruption FAIL paths for identity (p)
# --------------------------------------------------------------------------- #
def _p_check(analysis, frame, holding=None):
    return oc.check_order_allocation(
        frame,
        analysis.totals.holding_gbp if holding is None else holding,
        analysis.totals,
        set(analysis.frame["Invoice"].astype(str)),
    )


def test_identity_p_fails_if_an_order_is_lost(analysis):
    corrupted = analysis.frame.iloc[1:].copy()  # one real order lost in the spread
    check = _p_check(analysis, corrupted)
    assert not check.passed
    assert "lost" in check.note


def test_identity_p_fails_on_a_phantom_order(analysis):
    phantom = analysis.frame.iloc[[0]].copy()
    phantom["Invoice"] = "PHANTOM-1"
    check = _p_check(analysis, pd.concat([analysis.frame, phantom], ignore_index=True))
    assert not check.passed
    assert "phantom" in check.note


def test_identity_p_fails_on_a_duplicated_order_row(analysis):
    duplicated = pd.concat([analysis.frame, analysis.frame.iloc[[0]]], ignore_index=True)
    check = _p_check(analysis, duplicated)
    assert not check.passed
    assert "duplicated" in check.note or "!=" in check.note


def test_identity_p_fails_on_a_cent_of_drift(analysis):
    corrupted = analysis.frame.copy()
    corrupted.loc[corrupted.index[0], "TransportCost"] += 0.02  # two cents on one order
    check = _p_check(analysis, corrupted)
    assert not check.passed
    assert "transport" in check.note


def test_identity_p_fails_on_a_nan_revenue(analysis):
    corrupted = analysis.frame.copy()
    corrupted.loc[corrupted.index[0], "Revenue"] = np.nan
    check = _p_check(analysis, corrupted)
    assert not check.passed
    assert "NaN" in check.note


def test_identity_p_fails_on_a_holding_mismatch(analysis):
    check = _p_check(analysis, analysis.frame, holding=analysis.totals.holding_gbp + 1.0)
    assert not check.passed
    assert "holding" in check.note


# --------------------------------------------------------------------------- #
# The committed artifact is unchanged; (p) is additive
# --------------------------------------------------------------------------- #
def test_committed_artifact_is_unchanged_and_p_is_additive():
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    assert artifact["identities_total"] == 13
    names = {i["name"] for i in artifact["identities"]}
    assert "(p) order-level allocation conservation" not in names


# --------------------------------------------------------------------------- #
# Deterministic emit + CLI
# --------------------------------------------------------------------------- #
def test_emit_is_byte_deterministic(analysis, tmp_path):
    csv1 = oc.write_csv(analysis, tmp_path / "a.csv").read_bytes()
    csv2 = oc.write_csv(analysis, tmp_path / "b.csv").read_bytes()
    assert csv1 == csv2
    csv1.decode("ascii")  # committed deliverable stays ASCII/diffable
    md1 = oc.write_markdown(analysis, "src", tmp_path / "a.md").read_bytes()
    md2 = oc.write_markdown(analysis, "src", tmp_path / "b.md").read_bytes()
    assert md1 == md2
    md1.decode("ascii")


def test_cli_runs_on_fixture_and_holds(baseline, tmp_path, monkeypatch, capsys):
    # reuse the already-built session baseline (no fresh CVRP solve in the test)
    monkeypatch.setattr(oc, "build_baseline", lambda fixture=True: baseline)
    monkeypatch.setattr(oc, "ORDER_COSTS_CSV", tmp_path / "order_costs.csv")
    monkeypatch.setattr(oc, "ORDER_COSTS_MD", tmp_path / "order_costs.md")
    rc = oc.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(p) order-level allocation conservation" in out
    assert "[PASS]" in out
    assert "13/13" in out
    assert (tmp_path / "order_costs.csv").exists()
    assert (tmp_path / "order_costs.md").exists()
