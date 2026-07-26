"""Stage 6 on the fixture: identities i-m + deliberate-corruption FAIL paths."""

from __future__ import annotations

import pytest

from chain import reconcile
from chain.contracts import Provenance


@pytest.fixture()
def ledger3(stage0, stage1, stage2, stage3, stage4, stage5, stage6) -> reconcile.Ledger:
    cleaned, demand, stream = stage0
    led = reconcile.Ledger()
    reconcile.register_stage0(led, cleaned, demand, stream)
    reconcile.register_stage1(led, stage1)
    reconcile.register_stage2(led, stage2)
    reconcile.register_stage3(led, stage3)
    reconcile.register_stage4(led, stream, stage4, stage5)
    reconcile.register_stage5(led, stage6)
    return led


def test_identity_i_window_pick_conservation(ledger3):
    check = reconcile.check_window_pick_conservation(ledger3)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_i_fails_on_lost_pick(ledger3):
    ledger3.entries["fulfil/picked_lines"].value -= 1.0  # the DES loses a line
    check = reconcile.check_window_pick_conservation(ledger3)
    assert not check.passed
    assert "FAIL" in check.line()


def test_identity_j_carton_conservation(ledger3):
    check = reconcile.check_carton_conservation(ledger3)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_j_fails_on_phantom_carton(ledger3):
    ledger3.entries["fulfil/cartons_shipped"].value += 1.0  # aggregation invents one
    check = reconcile.check_carton_conservation(ledger3)
    assert not check.passed


def test_identity_k_drop_conservation(ledger3):
    check = reconcile.check_drop_conservation(ledger3)
    assert check.passed, check.line()
    assert check.lhs == check.rhs


def test_identity_k_fails_on_dropped_route_node(stage0, stage4, stage5):
    # structural corruption: delete a customer node from a real CVRP route
    _, _, stream = stage0
    crippled_day = None
    for day in stage5.days:
        for route in day.cvrp_routes:
            if len(route) > 2:
                crippled_day = day
                break
        if crippled_day:
            break
    assert crippled_day is not None
    route = next(r for r in crippled_day.cvrp_routes if len(r) > 2)
    removed = route.pop(1)  # a drop silently vanishes from the route
    try:
        led = reconcile.Ledger()
        reconcile.register_stage4(led, stream, stage4, stage5)
        check = reconcile.check_drop_conservation(led)
        assert not check.passed
    finally:
        route.insert(1, removed)  # restore the session-scoped fixture


def test_identity_l_ledger_additivity(ledger3):
    check = reconcile.check_ledger_additivity(ledger3)
    assert check.passed, check.line()


def test_identity_l_fails_on_totals_drift(ledger3):
    ledger3.entries["costing/total_cost"].value += 0.01  # off by one cent
    check = reconcile.check_ledger_additivity(ledger3)
    assert not check.passed


def test_identity_m_window_revenue(ledger3, stage0, stage4):
    cleaned, demand, _ = stage0
    check = reconcile.check_window_revenue(ledger3, cleaned, demand, stage4)
    assert check.passed, check.line()
    assert abs(check.lhs - check.rhs) <= reconcile.PENNY


def test_identity_m_fails_on_revenue_drift(ledger3, stage0, stage4):
    cleaned, demand, _ = stage0
    ledger3.entries["costing/window_revenue"].value += 0.01  # one penny of drift
    check = reconcile.check_window_revenue(ledger3, cleaned, demand, stage4)
    assert not check.passed


def test_phase3_checks_all_pass_on_fixture(ledger3, stage0, stage4):
    cleaned, demand, _ = stage0
    checks = reconcile.run_phase3_checks(ledger3, cleaned, demand, stage4)
    assert len(checks) == 5
    assert all(c.passed for c in checks), [c.line() for c in checks]


def test_every_p3_ledger_entry_carries_provenance_and_units(ledger3):
    p3_keys = [
        k for k in ledger3.entries
        if k.startswith(("window/", "fulfil/", "transport/", "costing/"))
    ]
    assert len(p3_keys) >= 14
    for key in p3_keys:
        entry = ledger3.entries[key]
        assert isinstance(entry.provenance, Provenance), key
        assert entry.unit, key
    # the boundary itself: revenue stays real, physical work synthetic-capped
    assert ledger3.entries["costing/window_revenue"].provenance is Provenance.REAL
    for key in ("fulfil/labour_hours", "transport/cvrp_km", "costing/total_cost"):
        assert ledger3.entries[key].provenance is Provenance.SYNTHETIC_ASSIGNED, key
