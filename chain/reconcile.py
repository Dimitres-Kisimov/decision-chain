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
    InvoiceStream,
    LedgerEntry,
    Provenance,
    WeeklyDemand,
)

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
