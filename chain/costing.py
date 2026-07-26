"""Stage 5 — costing: the cost-to-serve ledger for the simulated window.

THIS IS A COST-STRUCTURE VIEW, NOT A PROFITABILITY CLAIM. Revenue is real;
every cost rate below is INVENTED (synthetic-assigned, labelled, seeded
upstream) — so margin statements would be fiction and none are made. What the
ledger honestly shows is the SHAPE of cost-to-serve: which activities the
chain's physical work turns into money, against the real revenue those same
orders earned, with every line carrying its provenance tag.

The four cost lines and their bases:

* labour     DES labour hours (stage 4a) x LABOUR_RATE_GBP_PER_HOUR;
* transport  CVRP route km (stage 4b)   x TRANSPORT_RATE_GBP_PER_KM;
* holding    phase-2 safety stock units x HOLDING_RATE x window weeks
             (the buffer is held all window long — a carrying charge on the
             stage-2 plan, not on simulated stock movements);
* facility   FACILITY_FIXED_GBP_PER_WEEK x window weeks (labelled fixed).

Split of inputs, stated explicitly: REAL — order composition, timestamps and
window revenue; DERIVED — nothing enters the ledger at derived strength (all
computed lines touch a synthetic input and are capped down); SYNTHETIC-
ASSIGNED — every rate, every hour (synthetic crew/geometry), every km
(invented coordinates), every safety-stock unit (invented lead times).

Allocation for the per-invoice view (each rule deterministic, sums preserved):
labour by each order's own pick minutes; transport by carton share of its
delivery day's km; facility equally per order; holding sits per SKU (it is a
stock property, not an order property).

Reconciliation: identity (l) checks the summary total equals the sum of the
four registered cost lines to the cent; identity (m) checks the ledger's
window revenue equals the cleaned-data revenue for the same window to the
penny, recomputed through an independent code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from chain.contracts import (
    CleanedTransactions,
    CostToServe,
    FulfilmentLog,
    Provenance,
    ReplenishmentPlan,
)
from chain.deliver import DeliveryResult

# Synthetic-assigned cost rates (INVENTED, labelled; GBP).
LABOUR_RATE_GBP_PER_HOUR = 14.50
TRANSPORT_RATE_GBP_PER_KM = 0.85
HOLDING_RATE_GBP_PER_UNIT_WEEK = 0.02
FACILITY_FIXED_GBP_PER_WEEK = 2_500.00


@dataclass
class LedgerLine:
    """One printed line of the cost-to-serve ledger, provenance attached."""

    item: str
    gbp: float
    basis: str
    provenance: Provenance

    def formatted(self) -> str:
        return f"{self.item:<22} {self.gbp:>14,.2f} GBP  {self.provenance.tag():<21} {self.basis}"


@dataclass
class CostingResult:
    """Stage-5 bundle: the CostToServe contract + the printable ledger."""

    cost: CostToServe
    lines: list[LedgerLine]
    window_weeks: pd.DatetimeIndex = field(repr=False)

    def line(self, item: str) -> LedgerLine:
        for entry in self.lines:
            if entry.item == item:
                return entry
        raise KeyError(item)

    @property
    def total_cost_gbp(self) -> float:
        return self.line("total cost").gbp

    @property
    def window_revenue_gbp(self) -> float:
        return self.line("window revenue").gbp


def window_revenue_lines(
    cleaned: CleanedTransactions, skus: list[str], window: pd.DatetimeIndex
) -> pd.DataFrame:
    """REAL revenue lines for tracked SKUs in the window (costing's own filter).

    Deliberately its own code path — identity (m) recomputes the same number
    through chain/ingest.py's window filter and the two must agree.
    """
    sales = cleaned.sales
    week_end = sales["InvoiceDate"].dt.to_period("W-SUN").dt.end_time.dt.normalize()
    mask = sales["StockCode"].isin(skus) & week_end.isin(window)
    return sales.loc[mask].copy()


def run(
    cleaned: CleanedTransactions,
    skus: list[str],
    plan: ReplenishmentPlan,
    fulfilment: FulfilmentLog,
    delivery: DeliveryResult,
) -> CostingResult:
    """Build the ledger and the CostToServe contract for the simulated window."""
    window = fulfilment.window_weeks
    n_weeks = len(window)
    revenue_lines = window_revenue_lines(cleaned, skus, window)
    revenue_by_invoice = revenue_lines.groupby("Invoice", observed=True)["Revenue"].sum()
    revenue_by_sku = revenue_lines.groupby("StockCode", observed=True)["Revenue"].sum()
    units_by_sku = revenue_lines.groupby("StockCode", observed=True)["Quantity"].sum()

    # ---- per-invoice frame ------------------------------------------------
    orders = fulfilment.orders
    n_orders = len(orders)
    labour_total = fulfilment.labour_hours * LABOUR_RATE_GBP_PER_HOUR
    transport_total = delivery.total_cvrp_km * TRANSPORT_RATE_GBP_PER_KM
    facility_total = FACILITY_FIXED_GBP_PER_WEEK * n_weeks

    day_km = delivery.per_day.set_index("Day")["CvrpKm"]
    day_cartons = delivery.per_day.set_index("Day")["Cartons"]

    per_invoice = pd.DataFrame(
        {
            "Invoice": orders["Invoice"].to_numpy(),
            "Day": orders["Day"].to_numpy(),
            "Revenue": orders["Invoice"].map(revenue_by_invoice).to_numpy(),
            "LabourCost": (orders["PickMinutes"] / 60.0 * LABOUR_RATE_GBP_PER_HOUR).to_numpy(),
            "TransportCost": (
                orders["Cartons"]
                / orders["Day"].map(day_cartons).to_numpy()
                * orders["Day"].map(day_km).to_numpy()
                * TRANSPORT_RATE_GBP_PER_KM
            ).to_numpy(),
            "FacilityCost": facility_total / n_orders if n_orders else 0.0,
        }
    )

    # ---- per-SKU frame (holding is a stock property) ----------------------
    ss = plan.plan.set_index("StockCode")["SafetyStock"] if len(plan.plan) else pd.Series(dtype=float)
    per_sku = pd.DataFrame({"StockCode": sorted(skus)})
    per_sku["Revenue"] = per_sku["StockCode"].map(revenue_by_sku).fillna(0.0)
    per_sku["Units"] = per_sku["StockCode"].map(units_by_sku).fillna(0.0)
    per_sku["SafetyStock"] = per_sku["StockCode"].map(ss).fillna(0.0)  # 0: no stage-2 plan
    per_sku["HoldingCost"] = per_sku["SafetyStock"] * HOLDING_RATE_GBP_PER_UNIT_WEEK * n_weeks
    holding_total = float(per_sku["HoldingCost"].sum())

    total_cost = labour_total + transport_total + holding_total + facility_total
    window_revenue = float(revenue_by_invoice.sum())

    synth = Provenance.SYNTHETIC_ASSIGNED
    lines = [
        LedgerLine(
            "labour", labour_total,
            f"{fulfilment.labour_hours:,.1f} DES hours x {LABOUR_RATE_GBP_PER_HOUR:.2f}/h (rate INVENTED)",
            synth,
        ),
        LedgerLine(
            "transport", transport_total,
            f"{delivery.total_cvrp_km:,.1f} CVRP km x {TRANSPORT_RATE_GBP_PER_KM:.2f}/km (km + rate INVENTED)",
            synth,
        ),
        LedgerLine(
            "holding", holding_total,
            f"{float(per_sku['SafetyStock'].sum()):,.0f} SS units x {HOLDING_RATE_GBP_PER_UNIT_WEEK:.2f}/unit-week x {n_weeks}w",
            synth,
        ),
        LedgerLine(
            "facility", facility_total,
            f"{FACILITY_FIXED_GBP_PER_WEEK:,.0f}/week fixed x {n_weeks}w (INVENTED)",
            synth,
        ),
        LedgerLine(
            "total cost", total_cost,
            "sum of the four lines above (identity (l))",
            synth,
        ),
        LedgerLine(
            "window revenue", window_revenue,
            f"real revenue of the same {n_weeks}-week window (identity (m))",
            Provenance.REAL,
        ),
    ]

    cost = CostToServe(
        per_invoice=per_invoice,
        per_sku=per_sku,
        column_provenance={
            "Revenue": Provenance.REAL,
            "Units": Provenance.REAL,
            "LabourCost": synth,
            "TransportCost": synth,
            "HoldingCost": synth,
            "FacilityCost": synth,
            "SafetyStock": synth,
        },
        assumptions={
            "window_weeks": n_weeks,
            "labour_rate_gbp_per_hour": LABOUR_RATE_GBP_PER_HOUR,
            "transport_rate_gbp_per_km": TRANSPORT_RATE_GBP_PER_KM,
            "holding_rate_gbp_per_unit_week": HOLDING_RATE_GBP_PER_UNIT_WEEK,
            "facility_fixed_gbp_per_week": FACILITY_FIXED_GBP_PER_WEEK,
            "rates": f"{synth.value} (INVENTED; this is a cost-structure view, no profit claims)",
            "allocation": "labour: own pick minutes; transport: carton share of day km; facility: equal per order",
            "holding_scope": "stage-2 safety stock; tracked SKUs without a plan carry 0",
        },
    )
    return CostingResult(cost=cost, lines=lines, window_weeks=window)
