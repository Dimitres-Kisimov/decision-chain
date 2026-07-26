"""Stage 4b — transport: shipped orders aggregated into delivery days and routed.

Real WHO and WHEN, synthetic WHERE. Every order the DES shipped (stage 4a)
becomes a drop on its ship day; drops are routed from a single depot with a
capacitated fleet, per delivery day, over the same representative window.

Synthetic customer geography (seeded, labelled): each REAL customer ID gets
one fixed coordinate, drawn from a generator seeded by a stable digest of the
customer key — the same customer lands on the same point in every run,
regardless of which other customers appear. Orders with a missing CustomerID
(flagged, never dropped, in stage 0) become per-invoice ``guest:<invoice>``
customers. The real destination country sets only the distance band: United
Kingdom drops fall in a 5-120 km disc around the depot, export drops in a
150-300 km band (a stand-in for the haul to a consolidation hub). This is a
LABELLED simplification — all destinations are collapsed onto one 2D plane;
nothing here claims to be actual geography.

Two solvers per delivery day, measured honestly on the identical instance:

* Clarke-Wright parallel savings (1964) — adapted from my route-optimizer
  repo (routeopt/heuristic.py: clarke_wright) — the classic construction
  baseline;
* OR-Tools CVRP — RoutingModel with a capacity dimension, PATH_CHEAPEST_ARC
  first solution + guided local search, adapted from my route-optimizer repo
  (routeopt/solver.py: solve_cvrp) with one deliberate change: the stop rule
  is a SOLUTION LIMIT, not a wall-clock limit, so the same instance always
  yields the same routes and the same km on any machine (deterministic —
  wall-clock limits are not).

Orders whose carton demand exceeds one vehicle ship their full vehicle-loads
as direct out-and-back trips first (standard full-truckload peeling); the
remainder joins the CVRP. Both solvers get the identical peeled instance and
the identical direct-trip km, so the comparison stays fair.

Provenance: SYNTHETIC_ASSIGNED — the drop list and day structure are real,
every kilometre stands on invented coordinates. Weakest input wins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from chain.contracts import CleanedTransactions, FulfilmentLog, Provenance, TransportPlan

SEED = 42                      # folded into every per-customer digest
UK_RADIUS_KM = (5.0, 120.0)    # synthetic-assigned domestic band
EXPORT_RADIUS_KM = (150.0, 300.0)  # synthetic-assigned export-hub band
VEHICLE_CAPACITY_CARTONS = 80  # synthetic-assigned van capacity
# Deterministic stop rule (NOT wall-clock): chosen at the measured knee of a
# solution-limit sweep on a representative 8-day subset of the full-data
# window (622 orders): limit 100 -> CVRP +4.7% vs Clarke-Wright (loses),
# 300 -> +0.4%, 1000 -> -1.1% (first budget that beats CW), gains collapsing
# per extra solution. Same instance + same limit -> same routes, same km.
SOLUTION_LIMIT = 1000
_SCALE = 100                   # km -> integer arc costs (adapted from routeopt)


# --------------------------------------------------------------------------- #
# Seeded customer geography (synthetic-assigned, labelled)
# --------------------------------------------------------------------------- #
def customer_lookup(cleaned: CleanedTransactions) -> pd.DataFrame:
    """Invoice -> (CustomerKey, Country) from the REAL sales frame.

    Missing CustomerIDs become per-invoice guest keys (stage 0 flags, never
    drops, unknown customers; transport must still deliver their orders).
    """
    first = cleaned.sales.groupby("Invoice", observed=True).first()
    keys = [
        f"cust:{int(cid)}" if pd.notna(cid) else f"guest:{inv}"
        for inv, cid in zip(first.index, first["CustomerID"], strict=True)
    ]
    return pd.DataFrame(
        {"Invoice": first.index.astype(str), "CustomerKey": keys, "Country": first["Country"].to_numpy()}
    ).reset_index(drop=True)


def _digest(key: str, seed: int) -> int:
    """Stable 64-bit digest of a customer key (hash() is not run-stable)."""
    payload = f"{seed}:{key}".encode()
    return int.from_bytes(hashlib.blake2s(payload, digest_size=8).digest(), "big")


def customer_coords(key: str, country: str, seed: int = SEED) -> tuple[float, float]:
    """Seeded coordinate for one customer: uniform in the country's distance band."""
    rng = np.random.default_rng(_digest(key, seed))
    lo, hi = UK_RADIUS_KM if country == "United Kingdom" else EXPORT_RADIUS_KM
    radius = float(np.sqrt(rng.uniform(lo**2, hi**2)))  # area-uniform in the annulus
    angle = float(rng.uniform(0.0, 2.0 * np.pi))
    return radius * np.cos(angle), radius * np.sin(angle)


# --------------------------------------------------------------------------- #
# Per-day CVRP instance + the two solvers (adapted from my route-optimizer repo)
# --------------------------------------------------------------------------- #
@dataclass
class DayRouting:
    """One delivery day, both solvers on the identical peeled instance."""

    day: pd.Timestamp
    drops: int                    # orders delivered (CVRP nodes + full-load-only orders)
    cartons: int
    direct_trips: int             # peeled full-vehicle out-and-back trips
    direct_km: float
    cvrp_km: float                # includes direct_km
    cvrp_vehicles: int            # includes direct trips
    cw_km: float                  # includes direct_km
    cw_vehicles: int
    cvrp_routes: list[list[int]]  # node lists incl. depot 0 at both ends
    node_invoices: list[str]      # node index-1 -> invoice (depot is node 0)
    full_load_only_invoices: list[str]  # orders fully peeled (no CVRP node)


def _route_km(route: list[int], dist: np.ndarray) -> float:
    # adapted from my route-optimizer repo (routeopt/model.py: route_distance)
    return float(sum(dist[route[i], route[i + 1]] for i in range(len(route) - 1)))


def clarke_wright_routes(
    dist: np.ndarray, demands: list[int], capacity: int
) -> list[list[int]]:
    """Clarke-Wright parallel savings, node 0 = depot.

    adapted from my route-optimizer repo (routeopt/heuristic.py:
    clarke_wright), with a deterministic (saving, i, j) sort.
    """
    customers = list(range(1, len(demands)))
    seq: dict[int, list[int]] = {}
    load: dict[int, int] = {}
    route_of: dict[int, int] = {}
    for key, c in enumerate(customers):
        seq[key] = [c]
        load[key] = int(demands[c])
        route_of[c] = key

    savings = []
    for a in range(len(customers)):
        i = customers[a]
        for b in range(a + 1, len(customers)):
            j = customers[b]
            savings.append((float(dist[0, i] + dist[0, j] - dist[i, j]), i, j))
    savings.sort(reverse=True)

    for s, i, j in savings:
        if s <= 0:
            break
        ki, kj = route_of[i], route_of[j]
        if ki == kj:
            continue
        ri, rj = seq[ki], seq[kj]
        if not ((ri[0] == i or ri[-1] == i) and (rj[0] == j or rj[-1] == j)):
            continue
        if load[ki] + load[kj] > capacity:
            continue
        if ri[0] == i:
            ri.reverse()
        if rj[-1] == j:
            rj.reverse()
        seq[ki] = ri + rj
        load[ki] += load[kj]
        for c in rj:
            route_of[c] = ki
        del seq[kj], load[kj]

    return [[0, *r, 0] for r in seq.values()]


def solve_cvrp_day(
    dist: np.ndarray,
    demands: list[int],
    capacity: int,
    solution_limit: int = SOLUTION_LIMIT,
) -> list[list[int]]:
    """OR-Tools CVRP for one day; deterministic via the solution-limit stop rule.

    adapted from my route-optimizer repo (routeopt/solver.py: solve_cvrp),
    swapping its wall-clock limit for ``params.solution_limit`` so identical
    instances give identical routes on any machine. Falls back to
    Clarke-Wright if the solver returns nothing (never observed; belt and
    braces for the identity checks downstream).
    """
    n_nodes = len(demands)
    if n_nodes <= 1:
        return []
    total = sum(demands)
    n_vehicles = max(1, -(-total // capacity) + 2)  # ceil + slack vehicles
    idist = np.round(dist * _SCALE).astype(np.int64)

    manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_index: int, to_index: int) -> int:
        return int(idist[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])

    transit = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    def demand_cb(from_index: int) -> int:
        return int(demands[manager.IndexToNode(from_index)])

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [int(capacity)] * n_vehicles, True, "Capacity"
    )

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.solution_limit = int(solution_limit)  # deterministic; NOT wall-clock

    solution = routing.SolveWithParameters(params)
    if solution is None:  # pragma: no cover - defensive fallback
        return clarke_wright_routes(dist, demands, capacity)

    routes: list[list[int]] = []
    for v in range(n_vehicles):
        index = routing.Start(v)
        route = [manager.IndexToNode(index)]
        while not routing.IsEnd(index):
            index = solution.Value(routing.NextVar(index))
            route.append(manager.IndexToNode(index))
        if len(route) > 2:
            routes.append(route)
    return routes


# --------------------------------------------------------------------------- #
# Stage runner
# --------------------------------------------------------------------------- #
@dataclass
class DeliveryResult:
    """Stage-4b bundle: the TransportPlan contract + the per-day comparison."""

    plan: TransportPlan
    per_day: pd.DataFrame
    days: list[DayRouting] = field(repr=False)

    @property
    def total_cvrp_km(self) -> float:
        return float(self.per_day["CvrpKm"].sum())

    @property
    def total_cw_km(self) -> float:
        return float(self.per_day["CwKm"].sum())

    @property
    def routed_drops(self) -> int:
        """Drops counted from the ROUTE STRUCTURES (identity (k)'s lhs)."""
        total = 0
        for day in self.days:
            in_routes = sum(len(r) - 2 for r in day.cvrp_routes)
            total += in_routes + len(day.full_load_only_invoices)
        return total


def run(
    cleaned: CleanedTransactions,
    fulfilment: FulfilmentLog,
    capacity: int = VEHICLE_CAPACITY_CARTONS,
    solution_limit: int = SOLUTION_LIMIT,
    seed: int = SEED,
) -> DeliveryResult:
    """Route every shipped order, per delivery day, with both solvers."""
    lookup = customer_lookup(cleaned).set_index("Invoice")
    orders = fulfilment.orders

    shipment_rows = []
    day_rows = []
    days: list[DayRouting] = []

    for day, group in orders.groupby("Day", observed=True, sort=True):
        group = group.sort_values("Invoice", kind="stable")  # deterministic node order
        node_invoices: list[str] = []
        full_only: list[str] = []
        coords = [(0.0, 0.0)]
        demands = [0]
        direct_trips = 0
        direct_km = 0.0
        per_order: dict[str, dict[str, object]] = {}

        for _, order in group.iterrows():
            invoice = str(order["Invoice"])
            info = lookup.loc[invoice]
            key, country = str(info["CustomerKey"]), str(info["Country"])
            x, y = customer_coords(key, country, seed=seed)
            depot_dist = float(np.hypot(x, y))
            cartons = max(1, int(order["Cartons"]))  # every order occupies a drop
            full, rem = divmod(cartons, capacity)
            direct_trips += full
            direct_km += 2.0 * depot_dist * full
            per_order[invoice] = {
                "CustomerKey": key, "Country": country, "XKm": x, "YKm": y,
                "Cartons": cartons, "DirectTrips": full,
            }
            if rem > 0 or full == 0:
                node_invoices.append(invoice)
                coords.append((x, y))
                demands.append(rem if full > 0 else cartons)
            else:
                full_only.append(invoice)

        pts = np.asarray(coords)
        diff = pts[:, None, :] - pts[None, :, :]
        dist = np.sqrt((diff**2).sum(axis=2))

        cvrp_routes = solve_cvrp_day(dist, demands, capacity, solution_limit)
        cw_routes = clarke_wright_routes(dist, demands, capacity)
        cvrp_km = sum(_route_km(r, dist) for r in cvrp_routes) + direct_km
        cw_km = sum(_route_km(r, dist) for r in cw_routes) + direct_km

        vehicle_of: dict[str, int] = {}
        for v, route in enumerate(cvrp_routes):
            for node in route[1:-1]:
                vehicle_of[node_invoices[node - 1]] = v

        for invoice, info in per_order.items():
            shipment_rows.append(
                {
                    "Invoice": invoice,
                    "Day": day,
                    "CustomerKey": info["CustomerKey"],
                    "Country": info["Country"],
                    "XKm": info["XKm"],
                    "YKm": info["YKm"],
                    "Cartons": info["Cartons"],
                    "DirectTrips": info["DirectTrips"],
                    "Vehicle": vehicle_of.get(invoice, -1),  # -1: full loads only
                }
            )

        days.append(
            DayRouting(
                day=day,
                drops=len(group),
                cartons=int(group["Cartons"].sum()),
                direct_trips=direct_trips,
                direct_km=direct_km,
                cvrp_km=cvrp_km,
                cvrp_vehicles=len(cvrp_routes) + direct_trips,
                cw_km=cw_km,
                cw_vehicles=len(cw_routes) + direct_trips,
                cvrp_routes=cvrp_routes,
                node_invoices=node_invoices,
                full_load_only_invoices=full_only,
            )
        )
        day_rows.append(
            {
                "Day": day,
                "Drops": len(group),
                "Cartons": int(group["Cartons"].sum()),
                "DirectTrips": direct_trips,
                "CvrpKm": cvrp_km,
                "CvrpVehicles": len(cvrp_routes) + direct_trips,
                "CwKm": cw_km,
                "CwVehicles": len(cw_routes) + direct_trips,
            }
        )

    shipments = pd.DataFrame(
        shipment_rows,
        columns=[
            "Invoice", "Day", "CustomerKey", "Country", "XKm", "YKm",
            "Cartons", "DirectTrips", "Vehicle",
        ],
    )
    per_day = pd.DataFrame(
        day_rows,
        columns=["Day", "Drops", "Cartons", "DirectTrips", "CvrpKm", "CvrpVehicles", "CwKm", "CwVehicles"],
    )

    plan = TransportPlan(
        shipments=shipments,
        assumptions={
            "geography": "seeded per-customer coordinates on one 2D plane (INVENTED)",
            "uk_band_km": UK_RADIUS_KM,
            "export_band_km": EXPORT_RADIUS_KM,
            "country_role": "real country sets the distance band only",
            "vehicle_capacity_cartons": capacity,
            "solver": "OR-Tools CVRP, PATH_CHEAPEST_ARC + GLS, solution-limit stop",
            "solution_limit": solution_limit,
            "baseline": "Clarke-Wright parallel savings (adapted from routeopt)",
            "full_load_rule": "full-vehicle loads peeled as direct out-and-back trips",
            "seed": seed,
            "drops": f"{Provenance.REAL.value} (shipped real orders)",
            "km": f"{Provenance.SYNTHETIC_ASSIGNED.value} (invented coordinates)",
        },
        provenance=Provenance.combine(cleaned.provenance, fulfilment.provenance),
    )
    return DeliveryResult(plan=plan, per_day=per_day, days=days)
