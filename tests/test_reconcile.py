"""Stage 6 on the fixture: identity checks a-h + ledger discipline + FAIL paths."""

from __future__ import annotations

import dataclasses

import pytest

from chain import reconcile
from chain.contracts import Provenance


@pytest.fixture()
def ledger(stage0, stage1) -> reconcile.Ledger:
    cleaned, demand, stream = stage0
    led = reconcile.Ledger()
    reconcile.register_stage0(led, cleaned, demand, stream)
    reconcile.register_stage1(led, stage1)
    return led


@pytest.fixture()
def ledger2(ledger, stage2, stage3) -> reconcile.Ledger:
    reconcile.register_stage2(ledger, stage2)
    reconcile.register_stage3(ledger, stage3)
    return ledger


def test_identity_a_revenue_matches_expected(ledger, expected):
    check = reconcile.check_revenue_identity(ledger, expected["cleaned_revenue_gbp"])
    assert check.passed, check.line()
    assert abs(check.lhs - check.rhs) <= reconcile.PENNY


def test_identity_b_demand_conservation(ledger):
    check = reconcile.check_demand_conservation(ledger)
    assert check.passed, check.line()


def test_identity_c_line_conservation(ledger):
    check = reconcile.check_line_conservation(ledger)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_d_forecast_coverage(stage0, stage1):
    _, demand, _ = stage0
    check = reconcile.check_forecast_coverage(stage1, demand)
    assert check.passed, check.line()


def test_identity_e_replenishment_coverage(stage1, stage2):
    check = reconcile.check_replenishment_coverage(stage2, stage1)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_e_fails_on_dropped_sku(stage1, stage2):
    crippled = dataclasses.replace(stage2, plan=stage2.plan.iloc[1:])  # lose one SKU
    check = reconcile.check_replenishment_coverage(crippled, stage1)
    assert not check.passed
    assert "missing" in check.note


def test_identity_f_pick_conservation(ledger2):
    check = reconcile.check_pick_conservation(ledger2)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_f_fails_on_lost_line(ledger2):
    ledger2.entries["warehouse/picked_lines_total"].value -= 1.0  # lose a pick
    check = reconcile.check_pick_conservation(ledger2)
    assert not check.passed


def test_identity_g_same_invoice_evaluation(stage3):
    check = reconcile.check_same_invoice_eval(stage3.comparison)
    assert check.passed, check.line()


def test_identity_g_fails_on_truncated_variant(stage3):
    pick_lists = dict(stage3.comparison.pick_lists)
    pick_lists["abc"] = pick_lists["abc"].iloc[:-3]  # evaluate abc on fewer invoices
    crippled = dataclasses.replace(stage3.comparison, pick_lists=pick_lists)
    check = reconcile.check_same_invoice_eval(crippled)
    assert not check.passed


def test_identity_h_provenance_audit(ledger2):
    check = reconcile.check_provenance_audit(ledger2)
    assert check.passed, check.line()
    # the audit really pins the boundary: travel synthetic, inputs real
    assert ledger2.entries["warehouse/travel_mean_optimal"].provenance is (
        Provenance.SYNTHETIC_ASSIGNED
    )
    assert ledger2.entries["warehouse/velocity_lines"].provenance is Provenance.REAL


def test_identity_h_fails_on_mislabelled_travel(ledger2):
    # someone upgrades a synthetic-geometry number to 'real' -> audit must fail
    ledger2.entries["warehouse/travel_mean_optimal"].provenance = Provenance.REAL
    check = reconcile.check_provenance_audit(ledger2)
    assert not check.passed
    assert "travel_mean_optimal" in check.note


def test_phase2_checks_all_pass_on_fixture(ledger2, stage1, stage2, stage3):
    checks = reconcile.run_phase2_checks(ledger2, stage2, stage1, stage3)
    assert len(checks) == 4
    assert all(c.passed for c in checks), [c.line() for c in checks]


def test_broken_ledger_fails_loudly(ledger):
    ledger.entries["ingest/demand_units"].value += 1.0  # corrupt one side
    check = reconcile.check_demand_conservation(ledger)
    assert not check.passed
    assert "FAIL" in check.line()


def test_unit_mismatch_fails():
    led = reconcile.Ledger()
    led.register("a/x", 1.0, "GBP", Provenance.REAL)
    led.register("b/x", 1.0, "units", Provenance.REAL)
    check = led.check("unit clash", "a/x", "b/x", tolerance=10.0)
    assert not check.passed
    assert "UNIT MISMATCH" in check.unit


def test_ledger_rejects_duplicate_keys():
    led = reconcile.Ledger()
    led.register("a/x", 1.0, "GBP", Provenance.REAL)
    with pytest.raises(ValueError):
        led.register("a/x", 2.0, "GBP", Provenance.REAL)
