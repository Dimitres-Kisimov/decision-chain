"""Stage 6 on the fixture: the four P1 identity checks + ledger discipline."""

from __future__ import annotations

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
