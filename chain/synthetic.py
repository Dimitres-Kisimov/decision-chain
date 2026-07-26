"""The labelled synthetic layers — every number in this module is INVENTED.

Phase 2 needs three inputs the real dataset simply does not contain: SKU
physical attributes, supplier lead times, and a warehouse to pick in. This
module fabricates them, and the honesty rules apply in full:

* every output is tagged ``Provenance.SYNTHETIC_ASSIGNED`` and flows through
  the weakest-input inheritance rule — anything computed downstream from these
  layers is synthetic-assigned all the way to the report;
* every assignment rule is documented here, next to the code that applies it;
* everything is deterministic under ``SEED`` (42): stable SKU ordering, one
  seeded generator per layer.

Assignment rules
----------------
SKU dims/weights (``sku_physical``): the REAL product description is mapped to
a size class by keyword — descriptions containing a LARGE keyword (JUMBO,
GIANT, CABINET, ...) become ``large``, else a SMALL keyword (MINI, CHARM,
MAGNET, ...) becomes ``small``, else ``medium`` — and dims/weights are then
drawn uniformly from per-class ranges. The description is real data; the
dims are not: plausible-by-class is the claim, nothing stronger.

Supplier lead times (``sku_lead_times``): assigned per DEMAND CLASS (the
Syntetos-Boylan class from stage 1), base weeks + a per-SKU jitter of 0-1
weeks. The rationale (steady movers ~ mainstream suppliers ~ shorter lead
times; lumpy movers ~ specials ~ longer) is an assumption, stated, not data.

Warehouse geometry (``make_geometry``): parallel-aisle racking, adapted from
my logistics-digital-twin repo (logitwin/data.py, ``make_warehouse``). The
dispatch point sits at (0, 0) on the front cross-aisle; slot (aisle a, bay b)
sits at x = a * AISLE_PITCH_M, y = FRONT_OFFSET_M + b * BAY_PITCH_M, and its
one-way dispatch distance is x + y (rectilinear). Capacity is rounded up to
whole aisles and is always >= the tracked SKU count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from chain.contracts import Provenance

SEED = 42

# --------------------------------------------------------------------------- #
# SKU physical attributes: description keyword -> size class -> dims/weights
# --------------------------------------------------------------------------- #
LARGE_KEYWORDS = (
    "JUMBO", "GIANT", "LARGE", "BIG", "CABINET", "CHEST", "DRAWER",
    "DOORMAT", "PARASOL", "STOOL", "CAKESTAND", "HAMPER",
)
SMALL_KEYWORDS = (
    "MINI", "SMALL", "TINY", "CHARM", "KEYRING", "KEY RING", "PENCIL",
    "CARD", "BADGE", "MAGNET", "BUTTON", "EGG", "TRINKET",
)

# Per-class uniform draw ranges: (length_cm, width_cm, height_cm, weight_kg).
SIZE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "small": {"L": (5, 15), "W": (4, 12), "H": (3, 10), "KG": (0.05, 0.40)},
    "medium": {"L": (15, 35), "W": (10, 25), "H": (8, 20), "KG": (0.30, 2.00)},
    "large": {"L": (35, 70), "W": (25, 50), "H": (20, 45), "KG": (1.50, 8.00)},
}

# Supplier lead time base weeks per stage-1 demand class (jitter adds 0-1).
LEAD_TIME_BASE_WEEKS: dict[str, int] = {
    "smooth": 2,
    "erratic": 3,
    "intermittent": 4,
    "lumpy": 4,
    "unclassified": 3,  # tracked SKUs stage 1 could not classify/forecast
}

# Warehouse geometry constants (metres).
AISLE_PITCH_M = 3.0
BAY_PITCH_M = 1.2
FRONT_OFFSET_M = 4.0
BAYS_PER_AISLE = 25


def size_class(description: str) -> str:
    """Map a REAL product description to a synthetic size class (documented rule)."""
    text = str(description).upper()
    if any(k in text for k in LARGE_KEYWORDS):
        return "large"
    if any(k in text for k in SMALL_KEYWORDS):
        return "small"
    return "medium"


def modal_descriptions(sales: pd.DataFrame, skus: list[str]) -> pd.Series:
    """Most frequent description per SKU (real data; deterministic tie-break)."""
    subset = sales.loc[sales["StockCode"].isin(skus)]
    modal = subset.groupby("StockCode", observed=True)["Description"].agg(
        lambda s: sorted(s.dropna().astype(str).mode().tolist())[0]
        if s.notna().any()
        else ""
    )
    return modal.reindex(skus, fill_value="")


def sku_physical(skus: list[str], descriptions: pd.Series, seed: int = SEED) -> pd.DataFrame:
    """Synthetic dims/weights per SKU, drawn by size class (seeded, labelled)."""
    rng = np.random.default_rng(seed)
    rows = []
    for sku in sorted(skus):  # stable order: draws do not depend on caller order
        cls = size_class(descriptions.get(sku, ""))
        r = SIZE_RANGES[cls]
        rows.append(
            {
                "StockCode": sku,
                "SizeClass": cls,
                "LengthCm": round(float(rng.uniform(*r["L"])), 1),
                "WidthCm": round(float(rng.uniform(*r["W"])), 1),
                "HeightCm": round(float(rng.uniform(*r["H"])), 1),
                "WeightKg": round(float(rng.uniform(*r["KG"])), 3),
            }
        )
    return pd.DataFrame(rows, columns=["StockCode", "SizeClass", "LengthCm", "WidthCm", "HeightCm", "WeightKg"])


def sku_lead_times(demand_classes: dict[str, str], seed: int = SEED) -> pd.DataFrame:
    """Synthetic supplier lead time per SKU: class base weeks + 0-1 week jitter."""
    rng = np.random.default_rng(seed + 1)
    rows = []
    for sku in sorted(demand_classes):
        cls = demand_classes[sku]
        base = LEAD_TIME_BASE_WEEKS.get(cls, LEAD_TIME_BASE_WEEKS["unclassified"])
        rows.append(
            {
                "StockCode": sku,
                "Class": cls,
                "LeadTimeWeeks": int(base + rng.integers(0, 2)),
            }
        )
    return pd.DataFrame(rows, columns=["StockCode", "Class", "LeadTimeWeeks"])


# --------------------------------------------------------------------------- #
# Warehouse geometry  (adapted from my logistics-digital-twin repo, data.py)
# --------------------------------------------------------------------------- #
@dataclass
class WarehouseGeometry:
    """Rack slots with dispatch distances. Entirely invented; seeded; labelled."""

    slots: pd.DataFrame  # SlotId, Aisle, Bay, XM, YM, DistanceM (one-way)
    assumptions: dict[str, object] = field(default_factory=dict)
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED

    @property
    def n_slots(self) -> int:
        return len(self.slots)


def make_geometry(min_slots: int, seed: int = SEED) -> WarehouseGeometry:
    """Parallel-aisle rack layout with capacity >= ``min_slots`` (whole aisles).

    adapted from my logistics-digital-twin repo (logitwin/data.py,
    make_warehouse): distance grows with aisle index and bay depth so slotting
    has a real ordering to exploit. Coordinates here are explicit (XM, YM)
    because stage 3 routes pick tours between slots, not just slot -> dispatch.
    """
    if min_slots < 1:
        raise ValueError("min_slots must be >= 1")
    aisles = int(np.ceil(min_slots / BAYS_PER_AISLE))
    rows = []
    sid = 0
    for aisle in range(aisles):
        for bay in range(BAYS_PER_AISLE):
            x = aisle * AISLE_PITCH_M
            y = FRONT_OFFSET_M + bay * BAY_PITCH_M
            rows.append(
                {
                    "SlotId": sid,
                    "Aisle": aisle,
                    "Bay": bay,
                    "XM": round(x, 2),
                    "YM": round(y, 2),
                    "DistanceM": round(x + y, 2),
                }
            )
            sid += 1
    slots = pd.DataFrame(rows, columns=["SlotId", "Aisle", "Bay", "XM", "YM", "DistanceM"])
    return WarehouseGeometry(
        slots=slots,
        assumptions={
            "layer": "warehouse geometry",
            "provenance": Provenance.SYNTHETIC_ASSIGNED.value,
            "seed": seed,
            "aisles": aisles,
            "bays_per_aisle": BAYS_PER_AISLE,
            "aisle_pitch_m": AISLE_PITCH_M,
            "bay_pitch_m": BAY_PITCH_M,
            "front_offset_m": FRONT_OFFSET_M,
            "dispatch_xy_m": (0.0, 0.0),
        },
    )


# --------------------------------------------------------------------------- #
# The bundle stage 2/3 consume
# --------------------------------------------------------------------------- #
@dataclass
class SyntheticLayers:
    """All phase-2 synthetic inputs in one labelled, seeded bundle."""

    sku_physical: pd.DataFrame
    lead_times: pd.DataFrame
    geometry: WarehouseGeometry
    seed: int
    assumptions: dict[str, object] = field(default_factory=dict)
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED

    def lead_time_weeks(self, sku: str) -> int:
        row = self.lead_times.loc[self.lead_times["StockCode"] == sku, "LeadTimeWeeks"]
        if row.empty:
            raise KeyError(f"no synthetic lead time assigned for SKU {sku}")
        return int(row.iloc[0])


def build(
    skus: list[str],
    descriptions: pd.Series,
    demand_classes: dict[str, str],
    seed: int = SEED,
) -> SyntheticLayers:
    """Build every synthetic layer for the tracked SKUs (deterministic, labelled).

    ``demand_classes`` maps SKU -> stage-1 class; tracked SKUs without a class
    (not forecastable) are assigned the 'unclassified' lead-time base.
    """
    classes = {sku: demand_classes.get(sku, "unclassified") for sku in skus}
    physical = sku_physical(skus, descriptions, seed=seed)
    leads = sku_lead_times(classes, seed=seed)
    geometry = make_geometry(min_slots=len(skus), seed=seed)
    return SyntheticLayers(
        sku_physical=physical,
        lead_times=leads,
        geometry=geometry,
        seed=seed,
        assumptions={
            "provenance": Provenance.SYNTHETIC_ASSIGNED.value,
            "seed": seed,
            "size_class_rule": "description keywords -> small/medium/large -> uniform draw",
            "lead_time_rule": "demand class base weeks + 0-1 week per-SKU jitter",
            "lead_time_base_weeks": dict(LEAD_TIME_BASE_WEEKS),
            "geometry": geometry.assumptions,
        },
    )
