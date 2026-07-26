"""Stage 3: tour-distance hand-checks, the measured slotting ordering, conservation."""

from __future__ import annotations

import pandas as pd
import pytest

from chain import synthetic, warehouse
from chain.contracts import InvoiceStream, Provenance


def _stream(rows: list[tuple[str, str, int]]) -> InvoiceStream:
    """Crafted stream: rows = [(invoice, sku, qty)]."""
    lines = pd.DataFrame(
        [
            {
                "Invoice": inv,
                "StockCode": sku,
                "Quantity": qty,
                "InvoiceDate": pd.Timestamp("2011-06-01"),
                "Week": pd.Timestamp("2011-06-05"),
            }
            for inv, sku, qty in rows
        ]
    )
    return InvoiceStream(lines=lines)


def test_single_line_tour_is_out_and_back():
    geo = synthetic.make_geometry(4)
    slot = geo.slots.iloc[2]  # aisle 0, bay 2
    stream = _stream([("I1", "A", 3)])
    df = warehouse.evaluate_slotting(stream, {"A": int(slot["SlotId"])}, geo)
    assert df["TravelM"].iloc[0] == pytest.approx(2 * (slot["XM"] + slot["YM"]))
    assert df["Lines"].iloc[0] == 1 and df["Units"].iloc[0] == 3


def test_two_line_same_aisle_tour_hand_check():
    """Same aisle, bays b1 < b2: depot -> b1 -> b2 -> depot = 2*x + 2*y2."""
    geo = synthetic.make_geometry(10)
    s1, s2 = geo.slots.iloc[1], geo.slots.iloc[5]
    df = warehouse.evaluate_slotting(
        _stream([("I1", "A", 1), ("I1", "B", 1)]),
        {"A": int(s1["SlotId"]), "B": int(s2["SlotId"])},
        geo,
    )
    assert df["TravelM"].iloc[0] == pytest.approx(2 * s1["XM"] + 2 * s2["YM"])


def test_measured_ordering_random_worst_optimal_matches_abc(stage3):
    """The ordering that actually holds on the fixture: optimal ~= abc < random.

    With one scalar distance per slot the LAP optimum is velocity-sorted
    placement (rearrangement inequality), so optimal can only re-break ties
    against abc; both must beat the random baseline.
    """
    comp = stage3.comparison
    random_m = comp.mean_travel("random")
    abc_m = comp.mean_travel("abc")
    opt_m = comp.mean_travel("optimal")
    assert abc_m < random_m
    assert opt_m < random_m
    assert opt_m == pytest.approx(abc_m, rel=5e-3)  # measured: tie-breaks only


def test_pick_conservation_per_variant(stage0, stage3):
    _, _, stream = stage0
    for variant in warehouse.VARIANTS:
        df = stage3.comparison.pick_lists[variant]
        assert int(df["Lines"].sum()) == stream.n_lines
        assert int(df["Invoice"].nunique()) == stream.n_invoices
    assert int(stage3.workload.pick_lists["Lines"].sum()) == stream.n_lines


def test_slottings_are_injective_and_workload_is_labelled(stage0, stage3):
    _, demand, _ = stage0
    for slotting in stage3.comparison.slottings.values():
        assert set(slotting) == set(demand.skus)
        assert len(set(slotting.values())) == len(slotting)  # one SKU per slot
    assert stage3.workload.provenance is Provenance.SYNTHETIC_ASSIGNED
    assert stage3.comparison.velocity_provenance is Provenance.REAL
    assert "synthetic-assigned" in str(stage3.workload.assumptions["travel"])
    assert "real" in str(stage3.workload.assumptions["velocity"])
