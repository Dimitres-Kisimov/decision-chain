"""THE CENTERPIECE: typed I/O contracts for every stage of the decision chain.

The chain has seven stages. Every stage consumes and produces the contract
objects defined here — later phases implement stages against these types, and
the reconciliation harness (stage 6) checks identities BETWEEN stages, so a
stage cannot quietly change what a number means.

    stage 0  ingest      raw transactions            -> CleanedTransactions,
                                                        WeeklyDemand, InvoiceStream
    stage 1  forecast    WeeklyDemand                -> DemandForecast
    stage 2  inventory   DemandForecast              -> ReplenishmentPlan     [phase 2]
    stage 3  warehouse   InvoiceStream + Replenish.  -> WarehouseWorkload     [phase 2/3]
    stage 4  transport   WarehouseWorkload           -> TransportPlan         [phase 3]
    stage 5  costing     all upstream                -> CostToServe           [phase 3/4]
    stage 6  reconcile   Ledger entries from 0-5     -> identity checks (PASS/FAIL)

Provenance discipline
---------------------
Every contract object and every ledger entry carries a :class:`Provenance` tag:

* ``real``               — observed in the UCI Online Retail II dataset (or a
                           lossless aggregation of observed fields: sums, counts).
* ``synthetic-assigned`` — invented and labelled as such (SKU dims/weights,
                           warehouse geometry, cost rates — phases 2+). Seeded,
                           documented, never presented as data.
* ``derived``            — computed by a model or an assumption-bearing
                           transformation (forecasts, safety stocks, routes).

THE RULE: any derived quantity inherits the WEAKEST provenance of its inputs
(strength: real > derived > synthetic-assigned), and a computation is never
stronger than ``derived`` — see :meth:`Provenance.derive`. Formatted outputs
(reports, exports) must print the tag next to the number.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TypedDict

import pandas as pd


class Provenance(enum.Enum):
    """Where a number comes from. Ordering: REAL > DERIVED > SYNTHETIC_ASSIGNED."""

    REAL = "real"
    DERIVED = "derived"
    SYNTHETIC_ASSIGNED = "synthetic-assigned"

    @property
    def strength(self) -> int:
        return {"real": 3, "derived": 2, "synthetic-assigned": 1}[self.value]

    @classmethod
    def combine(cls, *tags: Provenance) -> Provenance:
        """Weakest provenance among ``tags`` (the chain is as honest as its weakest link)."""
        if not tags:
            raise ValueError("combine() needs at least one provenance tag")
        return min(tags, key=lambda t: t.strength)

    @classmethod
    def derive(cls, *tags: Provenance) -> Provenance:
        """Provenance of a COMPUTED quantity: weakest input, capped at DERIVED.

        A model output built from purely real inputs is ``derived`` (it is a
        computation, not an observation); anything touching a synthetic input
        is ``synthetic-assigned`` all the way downstream.
        """
        weakest = cls.combine(*tags)
        return weakest if weakest.strength < cls.DERIVED.strength else cls.DERIVED

    def tag(self) -> str:
        """The ASCII marker formatted outputs must carry, e.g. ``[real]``."""
        return f"[{self.value}]"


# --------------------------------------------------------------------------- #
# Stage 0 — ingest  (IMPLEMENTED in phase 1: chain/ingest.py)
# --------------------------------------------------------------------------- #
class CleanStep(TypedDict):
    """One row of the cleaning log: what a step cost, in rows."""

    step: str
    rows_in: int
    rows_out: int
    rows_removed: int
    pct_removed: float
    note: str


@dataclass
class CleanedTransactions:
    """Stage 0 output #1: the cleaned transaction base.

    In:  raw workbook rows (Invoice, StockCode, Description, Quantity,
         InvoiceDate, Price, CustomerID, Country) — units: quantity in units,
         price in GBP per unit.
    Out: ``sales`` (revenue-bearing rows, Revenue = Quantity * Price in GBP),
         ``returns`` (cancellation rows, product codes only), and the step log.

    Provenance: REAL — every row is an observed transaction; the pipeline only
    removes/flags rows and derives arithmetic columns (Revenue, Date, ISOWeek).
    """

    sales: pd.DataFrame
    returns: pd.DataFrame
    steps: list[CleanStep]
    provenance: Provenance = Provenance.REAL

    @property
    def revenue_gbp(self) -> float:
        """Total cleaned gross product revenue in GBP (the cross-repo identity)."""
        return float(self.sales["Revenue"].sum())


@dataclass
class WeeklyDemand:
    """Stage 0 output #2: weekly unit demand for the tracked SKUs.

    In:  ``CleanedTransactions.sales``.
    Out: ``demand`` — long frame (StockCode, Week [W-SUN period end], Units),
         zero-filled over ``weeks`` from each SKU's first selling week; the
         tracked SKUs are the top N by cleaned gross revenue (documented in
         chain/ingest.py). Units: units/week.

    Provenance: REAL — a lossless aggregation (sum of observed quantities);
    reconciliation identity (b) checks it against the line-level sum.
    """

    demand: pd.DataFrame
    skus: list[str]
    weeks: pd.DatetimeIndex
    provenance: Provenance = Provenance.REAL

    def series(self, sku: str) -> pd.Series:
        """Weekly units for one SKU from its first selling week (zero-filled)."""
        wide = self.demand.loc[self.demand["StockCode"] == sku].set_index("Week")["Units"]
        full = wide.reindex(self.weeks, fill_value=0.0).astype(float)
        first = full.ne(0).idxmax()
        return full.loc[first:]


@dataclass
class InvoiceStream:
    """Stage 0 output #3: the invoice/line stream — the future pick lists.

    In:  ``CleanedTransactions.sales`` restricted to the tracked SKUs.
    Out: ``lines`` — one row per invoice line (Invoice, StockCode, Quantity,
         InvoiceDate, Week). Stage 3 (warehouse) turns invoices into pick
         lists; stage 4 turns them into shipments. Units: units per line.

    Provenance: REAL — observed order composition, untouched.
    """

    lines: pd.DataFrame
    provenance: Provenance = Provenance.REAL

    @property
    def n_lines(self) -> int:
        return len(self.lines)

    @property
    def n_invoices(self) -> int:
        return int(self.lines["Invoice"].nunique())


# --------------------------------------------------------------------------- #
# Stage 1 — forecast  (IMPLEMENTED in phase 1: chain/forecast.py)
# --------------------------------------------------------------------------- #
@dataclass
class DemandForecast:
    """Stage 1 output: per-SKU weekly unit forecasts with uncertainty.

    In:  :class:`WeeklyDemand`.
    Out: ``forecasts`` — one row per (StockCode, Week) over the next horizon:
         Units (point forecast, units/week), Sigma (uncertainty, units/week —
         the std of that SKU's rolling-origin CV errors under the winning
         model), Model (the per-class winner used), Class (smooth |
         intermittent | lumpy). ``cv`` — one row per (StockCode, model, fold)
         with its MASE; ``class_winners`` — the honest per-class scoreboard.

    Consumed by stage 2 (inventory): Units drives the order-up-to level,
    Sigma drives safety stock.

    Provenance: DERIVED = Provenance.derive(REAL) — model output, not data.
    """

    forecasts: pd.DataFrame
    cv: pd.DataFrame
    class_winners: pd.DataFrame
    horizon_weeks: int
    provenance: Provenance = Provenance.DERIVED


# --------------------------------------------------------------------------- #
# Stage 2 — inventory  (PHASE 2 — contract declared now, implemented later)
# --------------------------------------------------------------------------- #
@dataclass
class ReplenishmentPlan:
    """Stage 2 output: what to reorder, when, and how much buffer to hold.

    In:  :class:`DemandForecast` (Units, Sigma) + synthetic-assigned lead
         times and service-level targets (phase 2, seeded and labelled).
    Out: ``plan`` — per SKU: ReorderPoint [units], SafetyStock [units],
         OrderQty [units], OrderWeek. ``assumptions`` — the labelled synthetic
         inputs used (lead time weeks, target service level, seed).

    Provenance: SYNTHETIC_ASSIGNED = Provenance.derive(DERIVED,
    SYNTHETIC_ASSIGNED) — real demand history, but the plan stands on
    invented lead times and MUST say so.
    """

    plan: pd.DataFrame
    assumptions: dict[str, object]
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED


# --------------------------------------------------------------------------- #
# Stage 3 — warehouse  (PHASE 2/3 — contract declared now)
# --------------------------------------------------------------------------- #
@dataclass
class WarehouseWorkload:
    """Stage 3 output: invoices turned into pick work inside a synthetic warehouse.

    In:  :class:`InvoiceStream` (real order composition) + synthetic-assigned
         warehouse geometry and SKU slotting (phase 2/3, seeded and labelled).
    Out: ``pick_lists`` — per invoice: lines, units, pick-route distance [m],
         pick time [min]. ``slotting`` — SKU -> location. ``assumptions`` —
         the labelled synthetic geometry (aisles, slot pitch, walking speed).

    Reconciliation ties it back: sum of picked units per SKU must equal the
    invoice-stream units per SKU (identity, phase 2).

    Provenance: SYNTHETIC_ASSIGNED — real WHAT (order lines), synthetic WHERE.
    """

    pick_lists: pd.DataFrame
    slotting: pd.DataFrame
    assumptions: dict[str, object]
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED


# --------------------------------------------------------------------------- #
# Stage 4 — transport  (PHASE 3 — contract declared now)
# --------------------------------------------------------------------------- #
@dataclass
class TransportPlan:
    """Stage 4 output: shipments consolidated and routed to customers.

    In:  :class:`WarehouseWorkload` pick lists + real destination countries
         from the invoice stream + synthetic-assigned geography (depot and
         customer coordinates within country, phase 3, seeded and labelled).
    Out: ``shipments`` — per invoice: Weight [kg, from synthetic SKU weights],
         Route, Distance [km], VehicleLoad share. ``assumptions`` — the
         labelled synthetic geography and fleet parameters.

    Reconciliation ties it back: every shipped invoice exists in the invoice
    stream; shipped units == picked units (identity, phase 3).

    Provenance: SYNTHETIC_ASSIGNED.
    """

    shipments: pd.DataFrame
    assumptions: dict[str, object]
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED


# --------------------------------------------------------------------------- #
# Stage 5 — costing  (PHASE 3/4 — contract declared now)
# --------------------------------------------------------------------------- #
@dataclass
class CostToServe:
    """Stage 5 output: cost-to-serve per SKU and per invoice.

    In:  everything upstream + synthetic-assigned cost rates (GBP per pick,
         per km, per unit-week of holding — phase 3/4, seeded and labelled).
    Out: ``per_invoice`` and ``per_sku`` frames — Revenue [GBP, real] against
         Handling/Transport/Holding cost [GBP, synthetic-assigned], margin.

    Reconciliation ties it back: sum of per-invoice revenue == cleaned
    revenue of the tracked SKUs — the same GBP number stage 0 registered.

    Provenance: SYNTHETIC_ASSIGNED for costs; the frames carry a per-column
    provenance dict because revenue columns remain REAL.
    """

    per_invoice: pd.DataFrame
    per_sku: pd.DataFrame
    column_provenance: dict[str, Provenance]
    assumptions: dict[str, object]
    provenance: Provenance = Provenance.SYNTHETIC_ASSIGNED


# --------------------------------------------------------------------------- #
# Stage 6 — reconcile  (IMPLEMENTED in phase 1: chain/reconcile.py)
# --------------------------------------------------------------------------- #
@dataclass
class LedgerEntry:
    """One number a stage registers for cross-stage checking.

    ``key`` is namespaced ``stage/name`` (e.g. ``ingest/revenue_gbp``);
    ``unit`` is a plain string (``GBP``, ``units``, ``lines``); ``provenance``
    follows the inheritance rule above.
    """

    key: str
    value: float
    unit: str
    provenance: Provenance
    note: str = ""


@dataclass
class CheckResult:
    """Outcome of one identity check: the two numbers, side by side, no rounding tricks."""

    name: str
    lhs_label: str
    lhs: float
    rhs_label: str
    rhs: float
    tolerance: float
    passed: bool
    unit: str = ""
    note: str = ""

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.name}: {self.lhs_label} = {self.lhs:,.2f} "
            f"vs {self.rhs_label} = {self.rhs:,.2f} {self.unit} (tol {self.tolerance})"
        )


STAGES: dict[int, str] = {
    0: "ingest",
    1: "forecast",
    2: "inventory",
    3: "warehouse",
    4: "transport",
    5: "costing",
    6: "reconcile",
}
