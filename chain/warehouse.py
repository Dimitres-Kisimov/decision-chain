"""Stage 3 — warehouse: real invoices picked inside the synthetic geometry.

Real WHAT, synthetic WHERE. The tracked SKUs are slotted into the synthetic
rack layout (chain/synthetic.py), and then every REAL invoice from the stage-0
invoice stream becomes a pick tour: start at dispatch, visit the slot of every
line on the invoice (nearest-neighbour ordering), return to dispatch. Pick
velocity is REAL — the number of invoice lines each SKU appears on in the
stream — and the travel metric is the standard rectilinear cross-aisle walk:

    d(dispatch, slot)  = x + y
    d(slot_a, slot_b)  = |y_a - y_b|              if same aisle
                       = |x_a - x_b| + y_a + y_b  otherwise (walk out to the
                         front cross-aisle at y = 0, across, and back in)

Three slottings are compared HONESTLY on the identical invoice set:

* random             seeded permutation — the no-information baseline;
* abc                SKUs by real velocity (desc) into slots by dispatch
                     distance (asc) — classic ABC slotting;
* optimal            linear assignment on cost[i, j] = velocity_i * distance_j
                     (Hungarian; adapted from my logistics-digital-twin repo,
                     logitwin/slotting.py: optimize_slotting).

Known property, reported not hidden: with a scalar distance per slot the
linear-assignment optimum of velocity x distance is exactly velocity-sorted
placement (rearrangement inequality), so 'optimal' can only differ from 'abc'
in how velocity ties are broken — and pick TOURS (multi-line invoices) are
evaluated on top, where the single-visit objective is not the whole story.
Whatever ordering the measurement gives is the ordering the report states.

Provenance: the travel numbers are SYNTHETIC_ASSIGNED (the geometry is
invented) even though the invoice composition and velocities are REAL —
weakest input wins, and identity (h) audits exactly this labelling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from chain.contracts import InvoiceStream, Provenance, WarehouseWorkload
from chain.synthetic import SEED, SyntheticLayers, WarehouseGeometry

WALK_SPEED_MPS = 1.2       # synthetic-assigned picker walking speed
LINE_HANDLE_SECONDS = 10.0  # synthetic-assigned per-line handling time

VARIANTS = ("random", "abc", "optimal")


# --------------------------------------------------------------------------- #
# Velocity (REAL) and the three slottings
# --------------------------------------------------------------------------- #
def velocity_lines(stream: InvoiceStream, skus: list[str]) -> pd.Series:
    """REAL pick velocity: invoice lines per SKU in the stream (0 if never picked)."""
    counts = stream.lines.groupby("StockCode", observed=True).size()
    return counts.reindex(skus, fill_value=0).astype(float)


def slot_random(skus: list[str], geometry: WarehouseGeometry, seed: int = SEED) -> dict[str, int]:
    """No-information baseline: seeded random slot per SKU."""
    rng = np.random.default_rng(seed + 2)
    slot_ids = geometry.slots["SlotId"].to_numpy()
    chosen = rng.permutation(slot_ids)[: len(skus)]
    return {sku: int(slot) for sku, slot in zip(sorted(skus), chosen, strict=True)}


def slot_abc(skus: list[str], geometry: WarehouseGeometry, velocity: pd.Series) -> dict[str, int]:
    """Classic ABC: fastest movers (REAL velocity) into the closest slots."""
    order = sorted(skus, key=lambda s: (-float(velocity[s]), s))
    slots = geometry.slots.sort_values(["DistanceM", "SlotId"], kind="stable")
    return {
        sku: int(slot)
        for sku, slot in zip(order, slots["SlotId"].head(len(order)), strict=True)
    }


def slot_optimal(skus: list[str], geometry: WarehouseGeometry, velocity: pd.Series) -> dict[str, int]:
    """Linear-assignment slotting minimising sum(velocity * dispatch distance).

    adapted from my logistics-digital-twin repo (logitwin/slotting.py,
    optimize_slotting): cost[i, j] = velocity[sku_i] * distance[slot_j],
    solved exactly by the Hungarian algorithm.
    """
    sku_list = sorted(skus)
    slots = geometry.slots
    v = np.array([float(velocity[s]) for s in sku_list])
    d = slots["DistanceM"].to_numpy(dtype=float)
    cost = np.outer(v, d)
    rows, cols = linear_sum_assignment(cost)
    slot_ids = slots["SlotId"].to_numpy()
    return {sku_list[i]: int(slot_ids[j]) for i, j in zip(rows, cols, strict=True)}


# --------------------------------------------------------------------------- #
# Pick-tour evaluation on the REAL invoice stream
# --------------------------------------------------------------------------- #
def _coords(geometry: WarehouseGeometry) -> tuple[np.ndarray, np.ndarray]:
    """(x, y) arrays indexed by SlotId (slots are emitted with SlotId == index)."""
    slots = geometry.slots.sort_values("SlotId")
    return slots["XM"].to_numpy(dtype=float), slots["YM"].to_numpy(dtype=float)


def _dist(a: int, b: int, x: np.ndarray, y: np.ndarray) -> float:
    """Cross-aisle rectilinear distance between two slots (see module docstring)."""
    if x[a] == x[b]:
        return abs(float(y[a] - y[b]))
    return abs(float(x[a] - x[b])) + float(y[a] + y[b])


def tour_distance(slot_ids: list[int], x: np.ndarray, y: np.ndarray) -> float:
    """Nearest-neighbour pick tour: dispatch (0,0) -> slots -> dispatch, metres.

    Deterministic: ties in the nearest-neighbour choice break on lowest SlotId.
    """
    remaining = sorted(set(slot_ids))
    total = 0.0
    current: int | None = None  # None = dispatch
    while remaining:
        best = min(
            remaining,
            key=lambda s: (
                float(x[s] + y[s]) if current is None else _dist(current, s, x, y),
                s,
            ),
        )
        total += float(x[best] + y[best]) if current is None else _dist(current, best, x, y)
        remaining.remove(best)
        current = best
    if current is not None:
        total += float(x[current] + y[current])  # walk back to dispatch
    return total


def evaluate_slotting(
    stream: InvoiceStream, slotting: dict[str, int], geometry: WarehouseGeometry
) -> pd.DataFrame:
    """One pick tour per REAL invoice: lines, units, travel [m], time [min]."""
    x, y = _coords(geometry)
    rows = []
    for invoice, group in stream.lines.groupby("Invoice", observed=True, sort=True):
        slots = [slotting[sku] for sku in group["StockCode"]]
        travel = tour_distance(slots, x, y)
        n_lines = len(group)
        rows.append(
            {
                "Invoice": invoice,
                "Lines": n_lines,
                "Units": float(group["Quantity"].sum()),
                "TravelM": travel,
                "TimeMin": (travel / WALK_SPEED_MPS + n_lines * LINE_HANDLE_SECONDS) / 60.0,
            }
        )
    return pd.DataFrame(rows, columns=["Invoice", "Lines", "Units", "TravelM", "TimeMin"])


@dataclass
class SlottingComparison:
    """The three-way honest comparison, all variants on the identical invoice set."""

    pick_lists: dict[str, pd.DataFrame]   # variant -> per-invoice evaluation
    slottings: dict[str, dict[str, int]]  # variant -> {sku: slot}
    velocity: pd.Series                   # REAL lines per SKU
    velocity_provenance: Provenance = Provenance.REAL
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED  # travel: synthetic geometry

    def mean_travel(self, variant: str) -> float:
        return float(self.pick_lists[variant]["TravelM"].mean())

    def summary(self) -> pd.DataFrame:
        rows = []
        base = self.mean_travel("random")
        for variant in VARIANTS:
            df = self.pick_lists[variant]
            mean = self.mean_travel(variant)
            rows.append(
                {
                    "variant": variant,
                    "invoices": int(df["Invoice"].nunique()),
                    "lines": int(df["Lines"].sum()),
                    "mean_travel_m": mean,
                    "total_travel_m": float(df["TravelM"].sum()),
                    "delta_vs_random_pct": 100.0 * (mean - base) / base if base else 0.0,
                }
            )
        return pd.DataFrame(rows)


@dataclass
class WarehouseResult:
    """Stage-3 bundle: the contract object + the comparison behind it."""

    workload: WarehouseWorkload
    comparison: SlottingComparison = field(repr=False)


def run(stream: InvoiceStream, layers: SyntheticLayers, seed: int = SEED) -> WarehouseResult:
    """Slot, evaluate all three variants on the same invoices, emit the contract.

    The WarehouseWorkload carries the assignment-optimal variant (the layout a
    planner would deploy); the comparison keeps all three for the report.
    """
    geometry = layers.geometry
    skus = layers.lead_times["StockCode"].tolist()
    if geometry.n_slots < len(skus):
        raise ValueError("synthetic geometry has fewer slots than tracked SKUs")
    velocity = velocity_lines(stream, skus)

    slottings = {
        "random": slot_random(skus, geometry, seed=seed),
        "abc": slot_abc(skus, geometry, velocity),
        "optimal": slot_optimal(skus, geometry, velocity),
    }
    pick_lists = {
        variant: evaluate_slotting(stream, slotting, geometry)
        for variant, slotting in slottings.items()
    }
    comparison = SlottingComparison(pick_lists=pick_lists, slottings=slottings, velocity=velocity)

    dist = geometry.slots.set_index("SlotId")["DistanceM"]
    slotting_df = pd.DataFrame(
        {
            "StockCode": sorted(skus),
            "SlotId": [slottings["optimal"][s] for s in sorted(skus)],
        }
    )
    slotting_df["DistanceM"] = slotting_df["SlotId"].map(dist)
    slotting_df["VelocityLines"] = slotting_df["StockCode"].map(velocity)

    workload = WarehouseWorkload(
        pick_lists=pick_lists["optimal"],
        slotting=slotting_df,
        assumptions={
            "variant": "optimal (linear assignment)",
            "geometry": geometry.assumptions,
            "walk_speed_mps": WALK_SPEED_MPS,
            "line_handle_seconds": LINE_HANDLE_SECONDS,
            "velocity": f"{Provenance.REAL.value} (invoice lines per SKU)",
            "travel": f"{Provenance.SYNTHETIC_ASSIGNED.value} (invented geometry)",
            "seed": seed,
        },
        provenance=Provenance.combine(stream.provenance, geometry.provenance),
    )
    return WarehouseResult(workload=workload, comparison=comparison)
