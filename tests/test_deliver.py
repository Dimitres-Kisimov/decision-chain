"""Stage 4b on the fixture: seeded geography, CVRP determinism, honest baseline."""

from __future__ import annotations

import numpy as np

from chain import deliver
from chain.contracts import Provenance
from chain.deliver import EXPORT_RADIUS_KM, UK_RADIUS_KM, customer_coords


# --------------------------------------------------------------------------- #
# Seeded synthetic geography
# --------------------------------------------------------------------------- #
def test_customer_coords_are_stable_and_banded():
    a = customer_coords("cust:12345", "United Kingdom")
    b = customer_coords("cust:12345", "United Kingdom")
    assert a == b  # same customer, same point, every run
    r_uk = float(np.hypot(*a))
    assert UK_RADIUS_KM[0] <= r_uk <= UK_RADIUS_KM[1]
    r_exp = float(np.hypot(*customer_coords("cust:12345", "Germany")))
    assert EXPORT_RADIUS_KM[0] <= r_exp <= EXPORT_RADIUS_KM[1]


def test_different_customers_get_different_points():
    assert customer_coords("cust:1", "United Kingdom") != customer_coords(
        "cust:2", "United Kingdom"
    )


def test_guest_invoices_get_customer_keys(stage0):
    cleaned, _, _ = stage0
    lookup = deliver.customer_lookup(cleaned)
    assert lookup["CustomerKey"].str.match(r"^(cust|guest):").all()
    assert lookup["CustomerKey"].notna().all()


# --------------------------------------------------------------------------- #
# CVRP determinism + the honest baseline comparison
# --------------------------------------------------------------------------- #
def test_cvrp_is_deterministic_same_limit_same_km(stage0, stage4, stage5):
    cleaned, _, _ = stage0
    again = deliver.run(cleaned, stage4)
    assert abs(again.total_cvrp_km - stage5.total_cvrp_km) < 1e-9
    assert again.per_day["CvrpKm"].tolist() == stage5.per_day["CvrpKm"].tolist()
    assert again.per_day["CvrpVehicles"].tolist() == stage5.per_day["CvrpVehicles"].tolist()


def test_cvrp_beats_or_ties_clarke_wright_on_every_fixture_day(stage5):
    # Measured relation on the fixture: CVRP never loses a day to CW.
    # Asserted as measured -- if the geometry ever changes this, the test
    # should be re-measured, not the claim inflated.
    assert (stage5.per_day["CvrpKm"] <= stage5.per_day["CwKm"] + 1e-6).all()
    assert stage5.total_cvrp_km <= stage5.total_cw_km + 1e-6


def test_routes_serve_every_node_exactly_once_within_capacity(stage5):
    for day in stage5.days:
        served = sorted(n for r in day.cvrp_routes for n in r[1:-1])
        assert served == list(range(1, len(day.node_invoices) + 1))
        # rebuild demands from the shipments frame to audit loads
        ship = stage5.plan.shipments
        by_invoice = ship.set_index("Invoice")
        cap = int(stage5.plan.assumptions["vehicle_capacity_cartons"])
        for route in day.cvrp_routes:
            load = 0
            for node in route[1:-1]:
                invoice = day.node_invoices[node - 1]
                cartons = int(by_invoice.loc[invoice, "Cartons"])
                load += cartons % cap if cartons > cap else (cap if cartons == cap else cartons)
            assert load <= cap


def test_every_shipped_order_is_a_drop(stage4, stage5):
    assert set(stage5.plan.shipments["Invoice"]) == set(stage4.orders["Invoice"])
    assert len(stage5.plan.shipments) == len(stage4.orders)
    assert stage5.routed_drops == len(stage4.orders)


def test_transport_plan_provenance_and_labels(stage5):
    plan = stage5.plan
    assert plan.provenance is Provenance.SYNTHETIC_ASSIGNED
    assert "INVENTED" in str(plan.assumptions["geography"])
    assert plan.assumptions["solution_limit"] == deliver.SOLUTION_LIMIT
    assert str(plan.assumptions["km"]).startswith("synthetic-assigned")
