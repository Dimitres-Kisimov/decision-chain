"""Stage 4a on the fixture: window selection, DES determinism, FFD packing."""

from __future__ import annotations

from chain import fulfil
from chain.contracts import Provenance
from chain.fulfil import CARTON_MAX_KG, CARTON_VOLUME_CM3, pack_invoice


# --------------------------------------------------------------------------- #
# Representative window
# --------------------------------------------------------------------------- #
def test_window_contains_the_peak_week(stage0):
    _, demand, stream = stage0
    window = fulfil.select_window(stream, demand.weeks)
    per_week = stream.lines.groupby("Week", observed=True).size()
    peak_week = per_week.reindex(demand.weeks, fill_value=0).idxmax()
    assert peak_week in window
    assert len(window) == min(fulfil.WINDOW_WEEKS, len(demand.weeks))


def test_window_is_contiguous_and_deterministic(stage0):
    _, demand, stream = stage0
    a = fulfil.select_window(stream, demand.weeks)
    b = fulfil.select_window(stream, demand.weeks)
    assert list(a) == list(b)
    positions = [demand.weeks.get_loc(w) for w in a]
    assert positions == list(range(positions[0], positions[0] + len(a)))


# --------------------------------------------------------------------------- #
# DES
# --------------------------------------------------------------------------- #
def test_des_is_deterministic(stage0, stage3, layers, stage4):
    _, demand, stream = stage0
    again = fulfil.simulate(stream, stage3.workload, layers, demand.weeks)
    assert again.orders.equals(stage4.orders)
    assert again.per_day.equals(stage4.per_day)


def test_des_covers_exactly_the_window_invoices(stage0, stage4):
    _, _, stream = stage0
    lines = fulfil.window_lines(stream, stage4.window_weeks)
    assert len(stage4.orders) == lines["Invoice"].nunique()
    assert int(stage4.orders["Lines"].sum()) == len(lines)
    assert int(stage4.orders["Units"].sum()) == int(lines["Quantity"].sum())


def test_des_labour_is_busy_time_and_orders_never_start_early(stage4):
    orders = stage4.orders
    # labour hours == the pick minutes actually worked, no invention
    assert abs(stage4.labour_hours - float(orders["PickMinutes"].sum()) / 60.0) < 1e-9
    # a picker cannot finish before start + service, nor start before arrival
    assert (orders["FinishMin"] - orders["StartMin"] - orders["PickMinutes"] > -1e-9).all()
    assert (orders["WaitMin"] > -1e-9).all()


def test_fulfilment_provenance_is_synthetic_capped(stage4):
    assert stage4.provenance is Provenance.SYNTHETIC_ASSIGNED
    assert "n_pickers" in stage4.assumptions
    assert str(stage4.assumptions["arrivals"]).startswith("real")


# --------------------------------------------------------------------------- #
# FFD packing (hand-checked instances)
# --------------------------------------------------------------------------- #
def test_packing_respects_volume():
    half = CARTON_VOLUME_CM3 / 2.0
    # five half-carton units -> 2 + 2 + 1 = 3 cartons
    assert pack_invoice([(half, 1.0, 5)]) == 3


def test_packing_respects_weight():
    # tiny volume but 10 kg per unit against a 20 kg cap -> 2 per carton
    assert pack_invoice([(10.0, CARTON_MAX_KG / 2.0, 5)]) == 3


def test_packing_oversize_units_ship_alone():
    assert pack_invoice([(CARTON_VOLUME_CM3 * 2.0, 1.0, 3)]) == 3
    assert pack_invoice([(10.0, CARTON_MAX_KG + 1.0, 2)]) == 2


def test_packing_ffd_consolidates_small_into_large_leftover():
    # one 60% carton + many 1% units: the small units fill the 40% gap first
    big = CARTON_VOLUME_CM3 * 0.6
    small = CARTON_VOLUME_CM3 * 0.01
    assert pack_invoice([(big, 1.0, 1), (small, 0.01, 40)]) == 1


def test_packed_weight_matches_synthetic_weights(stage0, stage4, layers):
    _, _, stream = stage0
    lines = fulfil.window_lines(stream, stage4.window_weeks)
    wt = layers.sku_physical.set_index("StockCode")["WeightKg"]
    expected = float((lines["Quantity"] * lines["StockCode"].map(wt)).sum())
    assert abs(float(stage4.orders["WeightKg"].sum()) - expected) < 1e-6
