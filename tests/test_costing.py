"""Stage 5 on the fixture: costing math to the cent, provenance on every line."""

from __future__ import annotations

from chain.contracts import Provenance
from chain.costing import (
    FACILITY_FIXED_GBP_PER_WEEK,
    HOLDING_RATE_GBP_PER_UNIT_WEEK,
    LABOUR_RATE_GBP_PER_HOUR,
    TRANSPORT_RATE_GBP_PER_KM,
)

CENT = 0.005


def test_ledger_lines_math_to_the_cent(stage4, stage5, stage6):
    n_weeks = len(stage6.window_weeks)
    assert abs(
        stage6.line("labour").gbp - stage4.labour_hours * LABOUR_RATE_GBP_PER_HOUR
    ) < CENT
    assert abs(
        stage6.line("transport").gbp - stage5.total_cvrp_km * TRANSPORT_RATE_GBP_PER_KM
    ) < CENT
    ss_units = float(stage6.cost.per_sku["SafetyStock"].sum())
    assert abs(
        stage6.line("holding").gbp - ss_units * HOLDING_RATE_GBP_PER_UNIT_WEEK * n_weeks
    ) < CENT
    assert abs(
        stage6.line("facility").gbp - FACILITY_FIXED_GBP_PER_WEEK * n_weeks
    ) < CENT
    component_sum = sum(
        stage6.line(item).gbp for item in ("labour", "transport", "holding", "facility")
    )
    assert abs(stage6.total_cost_gbp - component_sum) < CENT


def test_per_invoice_allocations_sum_to_the_ledger_lines(stage6):
    per_invoice = stage6.cost.per_invoice
    assert abs(float(per_invoice["LabourCost"].sum()) - stage6.line("labour").gbp) < CENT
    assert abs(float(per_invoice["TransportCost"].sum()) - stage6.line("transport").gbp) < CENT
    assert abs(float(per_invoice["FacilityCost"].sum()) - stage6.line("facility").gbp) < CENT
    assert abs(
        float(stage6.cost.per_sku["HoldingCost"].sum()) - stage6.line("holding").gbp
    ) < CENT


def test_per_invoice_revenue_equals_window_revenue(stage6):
    # every shipped order is billed; nothing invoiced that was not shipped
    assert stage6.cost.per_invoice["Revenue"].notna().all()
    assert abs(
        float(stage6.cost.per_invoice["Revenue"].sum()) - stage6.window_revenue_gbp
    ) < CENT


def test_every_ledger_line_carries_provenance(stage6):
    for line in stage6.lines:
        assert isinstance(line.provenance, Provenance)
        assert line.provenance.tag() in line.formatted()
    assert stage6.line("window revenue").provenance is Provenance.REAL
    for item in ("labour", "transport", "holding", "facility", "total cost"):
        assert stage6.line(item).provenance is Provenance.SYNTHETIC_ASSIGNED


def test_column_provenance_declares_the_real_synthetic_split(stage6):
    cp = stage6.cost.column_provenance
    assert cp["Revenue"] is Provenance.REAL
    for col in ("LabourCost", "TransportCost", "HoldingCost", "FacilityCost"):
        assert cp[col] is Provenance.SYNTHETIC_ASSIGNED
    # and the assumptions say out loud that no profit claim is being made
    assert "no profit claims" in str(stage6.cost.assumptions["rates"])


def test_unplanned_skus_carry_zero_holding(stage2, stage6):
    planned = set(stage2.plan["StockCode"])
    per_sku = stage6.cost.per_sku
    unplanned = per_sku.loc[~per_sku["StockCode"].isin(planned)]
    assert (unplanned["HoldingCost"] == 0.0).all()
