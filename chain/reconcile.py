"""Stage 6 — reconcile: the ledger and the machine-checked cross-stage identities.

Every stage registers the numbers it stands on into a :class:`Ledger`. The
identity checks then compare numbers ACROSS stages (and across repositories),
each computed by an independent code path, and print both sides. A silo can
be internally consistent and still wrong at the seams; these checks live at
the seams.

Phase 1 identities:

(a) cross-repo revenue    cleaned gross revenue from THIS repo's pipeline ==
                          the number published by my retail-analytics-real
                          repo (GBP 19,643,861.62, rounded to 19,643,862 in
                          its README). Same raw file + same documented
                          pipeline must give the same number to the penny.
(b) demand conservation   sum of weekly demand Units (stage 0 aggregation) ==
                          sum of cleaned line Quantities for the tracked SKUs
                          on the same week window (computed directly from the
                          sales frame, not via the demand object).
(c) line conservation     invoice-stream line count == cleaned sales line
                          count for the tracked SKUs on the same window.
(d) forecast coverage     every StockCode in the forecast output exists in
                          the weekly-demand SKU set (no orphan forecasts).

Phase 2 identities (stages 2-3):

(e) replenishment cover   the replenishment plan carries EVERY forecasted SKU
                          and nothing else (no orphans in either direction).
(f) pick conservation     sum of picked lines across all pick tours == the
                          invoice-stream line count (nothing lost between
                          stage 0 and stage 3).
(g) same-invoice eval     the three slotting variants are evaluated on the
                          IDENTICAL invoice set: same invoice count and same
                          line count across random / abc / optimal.
(h) provenance audit      the travel numbers must carry synthetic-assigned
                          provenance (the geometry is invented) while the
                          demand/velocity inputs stay labelled real — the
                          ledger is checked tag by tag.

Phase 3 identities (stages 4-5, all on the SAME representative window):

(i) window pick conservation   DES picked lines == the invoice-stream line
                               count for the window (independent recount
                               straight from the stream frame).
(j) carton conservation        cartons shipped (per-day aggregation) ==
                               cartons packed (per-order packing) — the
                               aggregation invents/loses nothing.
(k) drop conservation          drops counted from the ROUTE STRUCTURES
                               (CVRP node lists + peeled full-load orders)
                               == orders the DES shipped: none lost, none
                               duplicated between stage 4a and 4b.
(l) ledger additivity          the costing summary's total cost == the sum
                               of the four registered cost lines, to the
                               cent.
(m) window revenue             the ledger's window revenue == the cleaned-
                               data revenue for the same window, to the
                               penny, recomputed via chain/ingest.py's
                               window filter (costing uses its own).

On the committed fixture, identity (a) compares against the value recorded in
tests/fixtures/expected.json at fixture-generation time (the full-data
constant is only reachable with the real dataset on disk).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from chain import ingest
from chain.contracts import (
    CheckResult,
    CleanedTransactions,
    DemandForecast,
    FulfilmentLog,
    InvoiceStream,
    LedgerEntry,
    Provenance,
    ReplenishmentPlan,
    WeeklyDemand,
)
from chain.costing import CostingResult
from chain.deliver import DeliveryResult
from chain.fulfil import window_lines
from chain.warehouse import VARIANTS, SlottingComparison, WarehouseResult

# Published by my retail-analytics-real repo (README: "GBP 19,643,862" of
# revenue analyzed; exact pipeline value 19,643,861.62). Identity (a) target.
PUBLISHED_REVENUE_GBP = 19_643_861.62
PENNY = 0.005


@dataclass
class Ledger:
    """Cross-stage register: every number a stage stands on, with unit + provenance."""

    entries: dict[str, LedgerEntry] = field(default_factory=dict)

    def register(
        self,
        key: str,
        value: float,
        unit: str,
        provenance: Provenance,
        note: str = "",
    ) -> LedgerEntry:
        if key in self.entries:
            raise ValueError(f"ledger key already registered: {key}")
        entry = LedgerEntry(key=key, value=float(value), unit=unit, provenance=provenance, note=note)
        self.entries[key] = entry
        return entry

    def value(self, key: str) -> float:
        return self.entries[key].value

    def check(
        self,
        name: str,
        lhs_key: str,
        rhs_key: str,
        tolerance: float = 0.0,
        note: str = "",
    ) -> CheckResult:
        """Compare two registered entries; units must agree or the check fails outright."""
        lhs, rhs = self.entries[lhs_key], self.entries[rhs_key]
        unit_ok = lhs.unit == rhs.unit
        passed = unit_ok and abs(lhs.value - rhs.value) <= tolerance
        return CheckResult(
            name=name,
            lhs_label=lhs_key,
            lhs=lhs.value,
            rhs_label=rhs_key,
            rhs=rhs.value,
            tolerance=tolerance,
            passed=passed,
            unit=lhs.unit if unit_ok else f"UNIT MISMATCH {lhs.unit}!={rhs.unit}",
            note=note,
        )


def register_stage0(
    ledger: Ledger,
    cleaned: CleanedTransactions,
    demand: WeeklyDemand,
    stream: InvoiceStream,
) -> None:
    """Stage 0 registers its numbers — each side of an identity from its own code path."""
    ledger.register(
        "ingest/revenue_gbp",
        cleaned.revenue_gbp,
        "GBP",
        cleaned.provenance,
        "cleaned gross product revenue, full pipeline",
    )
    ledger.register(
        "ingest/demand_units",
        float(demand.demand["Units"].sum()),
        "units",
        demand.provenance,
        "sum of weekly demand over tracked SKUs",
    )
    # independent recomputation straight from the sales frame (NOT via demand)
    lines = ingest.sales_in_week_window(cleaned, demand.skus, demand.weeks)
    ledger.register(
        "ingest/line_units",
        float(lines["Quantity"].sum()),
        "units",
        cleaned.provenance,
        "sum of line quantities, tracked SKUs, same week window",
    )
    ledger.register(
        "ingest/line_count",
        float(len(lines)),
        "lines",
        cleaned.provenance,
        "cleaned sales line count, tracked SKUs, same week window",
    )
    ledger.register(
        "invoice_stream/line_count",
        float(stream.n_lines),
        "lines",
        stream.provenance,
        "invoice-stream lines (future pick lists)",
    )


def register_stage1(ledger: Ledger, forecast: DemandForecast) -> None:
    ledger.register(
        "forecast/rows",
        float(len(forecast.forecasts)),
        "rows",
        forecast.provenance,
        "forecast rows (SKU x future week)",
    )
    ledger.register(
        "forecast/skus",
        float(forecast.forecasts["StockCode"].nunique()) if len(forecast.forecasts) else 0.0,
        "skus",
        forecast.provenance,
        "distinct SKUs with a forecast",
    )


def register_stage2(ledger: Ledger, plan: ReplenishmentPlan) -> None:
    ledger.register(
        "inventory/plan_skus",
        float(plan.plan["StockCode"].nunique()) if len(plan.plan) else 0.0,
        "skus",
        plan.provenance,
        "SKUs with a replenishment plan",
    )
    ledger.register(
        "inventory/safety_stock_units",
        float(plan.plan["SafetyStock"].sum()) if len(plan.plan) else 0.0,
        "units",
        plan.provenance,
        "total safety stock (stands on synthetic lead times)",
    )


def register_stage3(ledger: Ledger, result: WarehouseResult) -> None:
    """Stage 3 registers travel per variant (synthetic-capped) and its real inputs."""
    comparison = result.comparison
    ledger.register(
        "warehouse/velocity_lines",
        float(comparison.velocity.sum()),
        "lines",
        comparison.velocity_provenance,
        "pick velocity input: invoice lines per SKU, summed (real demand)",
    )
    ledger.register(
        "warehouse/picked_lines_total",
        float(result.workload.pick_lists["Lines"].sum()),
        "lines",
        result.workload.provenance,
        "lines picked across all pick tours (deployed variant)",
    )
    for variant in VARIANTS:
        df = comparison.pick_lists[variant]
        ledger.register(
            f"warehouse/travel_mean_{variant}",
            comparison.mean_travel(variant),
            "m/invoice",
            comparison.provenance,
            f"mean pick travel per real invoice, {variant} slotting (synthetic geometry)",
        )
        ledger.register(
            f"warehouse/invoices_{variant}",
            float(df["Invoice"].nunique()),
            "invoices",
            comparison.provenance,
            f"invoices evaluated under {variant} slotting",
        )
        ledger.register(
            f"warehouse/lines_{variant}",
            float(df["Lines"].sum()),
            "lines",
            comparison.provenance,
            f"lines picked under {variant} slotting",
        )


def register_stage4(
    ledger: Ledger,
    stream: InvoiceStream,
    fulfilment: FulfilmentLog,
    delivery: DeliveryResult,
) -> None:
    """Stages 4a/4b register window numbers; each identity side has its own code path."""
    # Independent recount of the window's lines, straight from the stream frame.
    ledger.register(
        "window/stream_lines",
        float(len(window_lines(stream, fulfilment.window_weeks))),
        "lines",
        stream.provenance,
        f"invoice-stream lines inside the {len(fulfilment.window_weeks)}-week window",
    )
    ledger.register(
        "fulfil/picked_lines",
        float(fulfilment.orders["Lines"].sum()),
        "lines",
        fulfilment.provenance,
        "lines the DES picked (per-order log)",
    )
    ledger.register(
        "fulfil/orders",
        float(len(fulfilment.orders)),
        "orders",
        fulfilment.provenance,
        "orders the DES shipped in the window",
    )
    ledger.register(
        "fulfil/cartons_packed",
        float(fulfilment.orders["Cartons"].sum()),
        "cartons",
        fulfilment.provenance,
        "cartons packed, summed over the per-order log",
    )
    ledger.register(
        "fulfil/cartons_shipped",
        float(fulfilment.per_day["Cartons"].sum()),
        "cartons",
        fulfilment.provenance,
        "cartons shipped, summed over the per-day aggregation",
    )
    ledger.register(
        "fulfil/labour_hours",
        fulfilment.labour_hours,
        "hours",
        fulfilment.provenance,
        "DES picking labour (synthetic crew/geometry)",
    )
    ledger.register(
        "transport/routed_drops",
        float(delivery.routed_drops),
        "orders",
        delivery.plan.provenance,
        "drops counted from the route structures (CVRP nodes + peeled full loads)",
    )
    ledger.register(
        "transport/cvrp_km",
        delivery.total_cvrp_km,
        "km",
        delivery.plan.provenance,
        "CVRP route km over the window (invented coordinates)",
    )
    ledger.register(
        "transport/cw_km",
        delivery.total_cw_km,
        "km",
        delivery.plan.provenance,
        "Clarke-Wright baseline km, identical instances",
    )


def register_stage5(ledger: Ledger, costing: CostingResult) -> None:
    """Stage 5 registers each ledger line under its own key, provenance attached."""
    for line in costing.lines:
        key = "costing/" + line.item.replace(" ", "_")
        ledger.register(key, line.gbp, "GBP", line.provenance, line.basis)


def check_revenue_identity(
    ledger: Ledger, expected_gbp: float = PUBLISHED_REVENUE_GBP
) -> CheckResult:
    """(a) cross-repo: this pipeline's revenue vs the published number, to the penny."""
    ledger.entries.setdefault(
        "published/revenue_gbp",
        LedgerEntry(
            key="published/revenue_gbp",
            value=float(expected_gbp),
            unit="GBP",
            provenance=Provenance.REAL,
            note="published by retail-analytics-real (README) / fixture expected.json",
        ),
    )
    return ledger.check(
        "(a) cross-repo revenue",
        "ingest/revenue_gbp",
        "published/revenue_gbp",
        tolerance=PENNY,
        note="same pipeline, same raw file, same number",
    )


def check_demand_conservation(ledger: Ledger) -> CheckResult:
    """(b) weekly aggregation conserves units."""
    return ledger.check(
        "(b) demand conservation",
        "ingest/demand_units",
        "ingest/line_units",
        tolerance=1e-6,
        note="weekly sum vs line-level sum, tracked SKUs, same window",
    )


def check_line_conservation(ledger: Ledger) -> CheckResult:
    """(c) the invoice stream carries every cleaned sales line for tracked SKUs."""
    return ledger.check(
        "(c) line conservation",
        "invoice_stream/line_count",
        "ingest/line_count",
        tolerance=0.0,
        note="pick-list source vs cleaned sales lines",
    )


def check_forecast_coverage(forecast: DemandForecast, demand: WeeklyDemand) -> CheckResult:
    """(d) every forecast SKU exists in the demand set (no orphan forecasts)."""
    forecast_skus = set(forecast.forecasts["StockCode"]) if len(forecast.forecasts) else set()
    known = set(demand.skus)
    orphans = forecast_skus - known
    return CheckResult(
        name="(d) forecast coverage",
        lhs_label="forecast SKUs known to demand",
        lhs=float(len(forecast_skus - orphans)),
        rhs_label="forecast SKUs total",
        rhs=float(len(forecast_skus)),
        tolerance=0.0,
        passed=len(orphans) == 0,
        unit="skus",
        note=f"orphans: {sorted(orphans)[:5] if orphans else 'none'}",
    )


def run_phase1_checks(
    ledger: Ledger,
    forecast: DemandForecast,
    demand: WeeklyDemand,
    expected_revenue_gbp: float = PUBLISHED_REVENUE_GBP,
) -> list[CheckResult]:
    return [
        check_revenue_identity(ledger, expected_revenue_gbp),
        check_demand_conservation(ledger),
        check_line_conservation(ledger),
        check_forecast_coverage(forecast, demand),
    ]


def check_replenishment_coverage(plan: ReplenishmentPlan, forecast: DemandForecast) -> CheckResult:
    """(e) plan SKUs == forecast SKUs, both directions (no orphans, no gaps)."""
    plan_skus = set(plan.plan["StockCode"]) if len(plan.plan) else set()
    fc_skus = set(forecast.forecasts["StockCode"]) if len(forecast.forecasts) else set()
    missing = fc_skus - plan_skus  # forecasted but not planned
    orphans = plan_skus - fc_skus  # planned but never forecasted
    return CheckResult(
        name="(e) replenishment coverage",
        lhs_label="planned SKUs",
        lhs=float(len(plan_skus)),
        rhs_label="forecasted SKUs",
        rhs=float(len(fc_skus)),
        tolerance=0.0,
        passed=not missing and not orphans,
        unit="skus",
        note=(
            f"missing: {sorted(missing)[:5] if missing else 'none'}; "
            f"orphans: {sorted(orphans)[:5] if orphans else 'none'}"
        ),
    )


def check_pick_conservation(ledger: Ledger) -> CheckResult:
    """(f) nothing lost between stage 0 and stage 3: picked lines == stream lines."""
    return ledger.check(
        "(f) pick conservation",
        "warehouse/picked_lines_total",
        "invoice_stream/line_count",
        tolerance=0.0,
        note="pick-tour lines vs invoice-stream lines",
    )


def check_same_invoice_eval(comparison: SlottingComparison) -> CheckResult:
    """(g) all three slottings were measured on the identical invoice set."""
    inv_counts = {v: int(comparison.pick_lists[v]["Invoice"].nunique()) for v in VARIANTS}
    line_counts = {v: int(comparison.pick_lists[v]["Lines"].sum()) for v in VARIANTS}
    same_invoices = len(set(inv_counts.values())) == 1
    same_lines = len(set(line_counts.values())) == 1
    return CheckResult(
        name="(g) same-invoice evaluation",
        lhs_label="min lines across variants",
        lhs=float(min(line_counts.values())),
        rhs_label="max lines across variants",
        rhs=float(max(line_counts.values())),
        tolerance=0.0,
        passed=same_invoices and same_lines,
        unit="lines",
        note=f"invoices per variant: {inv_counts}; lines per variant: {line_counts}",
    )


# Ledger keys identity (h) audits, with the tag each MUST carry.
PROVENANCE_AUDIT: dict[str, Provenance] = {
    "ingest/demand_units": Provenance.REAL,
    "warehouse/velocity_lines": Provenance.REAL,
    "warehouse/travel_mean_random": Provenance.SYNTHETIC_ASSIGNED,
    "warehouse/travel_mean_abc": Provenance.SYNTHETIC_ASSIGNED,
    "warehouse/travel_mean_optimal": Provenance.SYNTHETIC_ASSIGNED,
    "inventory/safety_stock_units": Provenance.SYNTHETIC_ASSIGNED,
}


def check_provenance_audit(ledger: Ledger) -> CheckResult:
    """(h) the labels are right: travel/stock synthetic-capped, demand inputs real."""
    wrong = []
    correct = 0
    for key, required in PROVENANCE_AUDIT.items():
        entry = ledger.entries.get(key)
        if entry is not None and entry.provenance is required:
            correct += 1
        else:
            got = entry.provenance.value if entry is not None else "MISSING"
            wrong.append(f"{key}: {got} != {required.value}")
    return CheckResult(
        name="(h) provenance audit",
        lhs_label="correctly labelled entries",
        lhs=float(correct),
        rhs_label="entries audited",
        rhs=float(len(PROVENANCE_AUDIT)),
        tolerance=0.0,
        passed=not wrong,
        unit="entries",
        note="; ".join(wrong) if wrong else "travel synthetic-assigned, demand/velocity real",
    )


def run_phase2_checks(
    ledger: Ledger,
    plan: ReplenishmentPlan,
    forecast: DemandForecast,
    warehouse_result: WarehouseResult,
) -> list[CheckResult]:
    return [
        check_replenishment_coverage(plan, forecast),
        check_pick_conservation(ledger),
        check_same_invoice_eval(warehouse_result.comparison),
        check_provenance_audit(ledger),
    ]


def check_window_pick_conservation(ledger: Ledger) -> CheckResult:
    """(i) the DES picked exactly the window's invoice-stream lines."""
    return ledger.check(
        "(i) window pick conservation",
        "fulfil/picked_lines",
        "window/stream_lines",
        tolerance=0.0,
        note="DES per-order log vs independent stream recount, same window",
    )


def check_carton_conservation(ledger: Ledger) -> CheckResult:
    """(j) cartons shipped (per-day) == cartons packed (per-order)."""
    return ledger.check(
        "(j) carton conservation",
        "fulfil/cartons_shipped",
        "fulfil/cartons_packed",
        tolerance=0.0,
        note="per-day aggregation vs per-order packing log",
    )


def check_drop_conservation(ledger: Ledger) -> CheckResult:
    """(k) routed drops (from the route structures) == orders the DES shipped."""
    return ledger.check(
        "(k) drop conservation",
        "transport/routed_drops",
        "fulfil/orders",
        tolerance=0.0,
        note="CVRP nodes + peeled full loads vs shipped orders: none lost/duplicated",
    )


def check_ledger_additivity(ledger: Ledger) -> CheckResult:
    """(l) costing total == sum of the four registered cost lines, to the cent."""
    component_sum = sum(
        ledger.value(f"costing/{item}") for item in ("labour", "transport", "holding", "facility")
    )
    ledger.register(
        "costing/component_sum",
        component_sum,
        "GBP",
        Provenance.SYNTHETIC_ASSIGNED,
        "labour + transport + holding + facility, re-added here",
    )
    return ledger.check(
        "(l) ledger additivity",
        "costing/total_cost",
        "costing/component_sum",
        tolerance=PENNY,
        note="summary total vs sum of the four cost lines, to the cent",
    )


def check_window_revenue(
    ledger: Ledger,
    cleaned: CleanedTransactions,
    demand: WeeklyDemand,
    fulfilment: FulfilmentLog,
) -> CheckResult:
    """(m) ledger window revenue == cleaned-data revenue, same window, to the penny.

    The rhs goes through chain/ingest.py's window filter — a different code
    path from costing's own filter — so agreement is a real cross-check.
    """
    lines = ingest.sales_in_week_window(cleaned, demand.skus, fulfilment.window_weeks)
    ledger.register(
        "ingest/window_revenue",
        float(lines["Revenue"].sum()),
        "GBP",
        cleaned.provenance,
        "cleaned revenue, tracked SKUs, same window (ingest's filter)",
    )
    return ledger.check(
        "(m) window revenue",
        "costing/window_revenue",
        "ingest/window_revenue",
        tolerance=PENNY,
        note="costing's filter vs ingest's filter, same window, to the penny",
    )


def run_phase3_checks(
    ledger: Ledger,
    cleaned: CleanedTransactions,
    demand: WeeklyDemand,
    fulfilment: FulfilmentLog,
) -> list[CheckResult]:
    return [
        check_window_pick_conservation(ledger),
        check_carton_conservation(ledger),
        check_drop_conservation(ledger),
        check_ledger_additivity(ledger),
        check_window_revenue(ledger, cleaned, demand, fulfilment),
    ]


def print_checks(checks: list[CheckResult]) -> bool:
    all_passed = True
    for check in checks:
        print(f"  {check.line()}")
        if check.note:
            print(f"         {check.note}")
        all_passed &= check.passed
    return all_passed


def ledger_table(ledger: Ledger) -> pd.DataFrame:
    rows = [
        {
            "key": e.key,
            "value": e.value,
            "unit": e.unit,
            "provenance": e.provenance.tag(),
            "note": e.note,
        }
        for e in ledger.entries.values()
    ]
    return pd.DataFrame(rows, columns=["key", "value", "unit", "provenance", "note"])
