"""Per-order cost-to-serve: the ledger spread over every real order -- and
reassembled to the cent (identity (p)).

Identity (l) proves the ledger total is the sum of its four cost lines;
identity (n) proves each line prices the physical driver another stage booked.
Neither says anything about ORDERS -- yet cost-to-serve only becomes a decision
tool when the one aggregate number is spread over the real orders it was
incurred for. That allocation is exactly where practical costing systems break:
pounds go missing in the spread, or phantom pounds appear, and nobody notices
because only the total is ever checked. This module closes that seam.

**Identity (p) -- order-level allocation conservation.** Stage 5 already
allocates each cost line over the window's real orders under published,
labelled rules (labour by the order's own DES pick minutes; transport by the
order's carton share of its delivery day's CVRP km; facility split equally per
order) and keeps holding on the SKU plane (a stock property of the stage-2
plan, not an order property). Identity (p) machine-checks that this two-plane
spread loses nothing and invents nothing:

    per-order labour   sums back to the ledger labour line     (to the cent)
    per-order transport sums back to the ledger transport line (to the cent)
    per-order facility  sums back to the ledger facility line  (to the cent)
    per-SKU holding     sums back to the ledger holding line   (to the cent)
    order plane + SKU plane == ledger total cost               (to the cent)
    per-order REAL revenue == the ledger's real window revenue (to the penny)

plus order-set conservation: exactly one economics row per order the DES
shipped -- none lost, none duplicated, no NaNs smuggled in. All 13 cross-stage
identities (a-m) are re-checked on the same run.

**The readout (the cost-to-serve distribution).** With the spread proven
exact, the distribution is trustworthy: decile table (orders ranked by
modelled cost-to-serve), concentration (top-decile share, Gini), the orders
whose modelled cost-to-serve exceeds their own real revenue under the labelled
rates ("model-uncovered" -- a property of the INVENTED cost model, never a
profit claim), and the classic cost-to-serve whale curve (cumulative
model-implied coverage gap, best-covered orders first) whose endpoint equals
window revenue minus delivered cost EXACTLY -- the curve itself reconciles.

Honesty, unchanged from the rest of the chain: revenue is REAL; every cost
rate, hour, km and safety-stock unit is SYNTHETIC-ASSIGNED (invented,
labelled); the facility equal-split is itself a labelled allocation choice
(small orders look expensive under any per-order spread of a fixed charge).
"Model-uncovered" therefore describes the shape of cost-to-serve under stated
assumptions -- which order profiles are expensive to serve -- and is NEVER a
statement that a real order was unprofitable. This is a cost-structure view,
never a profit claim.

    python order_costs.py            # fixture path, emit deliverables, print the distribution
    python order_costs.py --full     # full dataset (slow: builds the CVRP baseline once)
    python order_costs.py --no-emit  # print only, do not write deliverables

Exit code is non-zero if identity (p) does not hold or any of the 13
identities fails on the run.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from chain import paths
from chain.contracts import CheckResult, Provenance
from chain.costing import CostingResult
from chain.reconcile import PENNY
from scenario import Baseline, build_baseline, build_ledger_and_checks

N_DECILES = 10

ORDER_COSTS_CSV = paths.DELIVERABLES_DIR / "order_costs.csv"
ORDER_COSTS_MD = paths.DELIVERABLES_DIR / "order_costs.md"

# Per-order columns identity (p) audits for NaNs (a NaN is a silently lost pound).
_AUDITED_COLUMNS: tuple[str, ...] = ("Revenue", "LabourCost", "TransportCost", "FacilityCost")


# --------------------------------------------------------------------------- #
# The per-order economics frame (stage 5's own allocation, extended)
# --------------------------------------------------------------------------- #
def order_frame(cs: CostingResult) -> pd.DataFrame:
    """Stage 5's per-invoice allocation plus the consumer-computed columns.

    DeliveredCost is the order-attributed modelled cost (labour + transport +
    facility; holding stays on the SKU plane, where stage 5 books it). Gap is
    the MODEL-IMPLIED coverage gap, real revenue minus modelled delivered cost
    -- labelled model-implied, never a margin. Returns a NEW frame sorted by
    invoice (deterministic); the contract frame is not mutated.
    """
    frame = cs.cost.per_invoice.copy()
    frame["DeliveredCost"] = frame["LabourCost"] + frame["TransportCost"] + frame["FacilityCost"]
    frame["Gap"] = frame["Revenue"] - frame["DeliveredCost"]
    return frame.sort_values("Invoice", kind="stable").reset_index(drop=True)


def holding_alloc_gbp(cs: CostingResult) -> float:
    """The SKU-plane holding allocation, summed (stage 5's per-SKU frame)."""
    return float(cs.cost.per_sku["HoldingCost"].sum())


@dataclass(frozen=True)
class LedgerTotals:
    """The registered stage-5 ledger lines the per-order spread must reassemble."""

    labour_gbp: float
    transport_gbp: float
    facility_gbp: float
    holding_gbp: float
    total_gbp: float
    window_revenue_gbp: float

    @classmethod
    def from_costing(cls, cs: CostingResult) -> LedgerTotals:
        return cls(
            labour_gbp=float(cs.line("labour").gbp),
            transport_gbp=float(cs.line("transport").gbp),
            facility_gbp=float(cs.line("facility").gbp),
            holding_gbp=float(cs.line("holding").gbp),
            total_gbp=float(cs.line("total cost").gbp),
            window_revenue_gbp=float(cs.line("window revenue").gbp),
        )


# --------------------------------------------------------------------------- #
# Identity (p) -- order-level allocation conservation
# --------------------------------------------------------------------------- #
def check_order_allocation(
    frame: pd.DataFrame,
    holding_gbp: float,
    totals: LedgerTotals,
    shipped_invoices: set[str],
    tol: float = PENNY,
) -> CheckResult:
    """(p) the two-plane spread of the ledger reassembles it, to the cent.

    ``passed`` requires: (1) exactly one economics row per DES-shipped order
    (none lost, none phantom) and no NaN in any audited column; (2) each
    order-plane cost column sums back to its registered ledger line to the
    cent, and the SKU-plane holding allocation to the holding line; (3) order
    plane + SKU plane == the ledger total cost to the cent; (4) the per-order
    REAL revenue sums back to the ledger's window revenue to the penny.
    lhs/rhs print the reassembled vs registered total; the note reports any
    violation.
    """
    violations: list[str] = []

    have = set(frame["Invoice"].astype(str))
    missing = shipped_invoices - have
    phantom = have - shipped_invoices
    if missing:
        violations.append(f"orders lost in the spread: {sorted(missing)[:3]}")
    if phantom:
        violations.append(f"phantom orders in the spread: {sorted(phantom)[:3]}")
    if len(frame) != len(have):
        violations.append(f"duplicated order rows: {len(frame)} rows != {len(have)} orders")
    for column in _AUDITED_COLUMNS:
        n_nan = int(frame[column].isna().sum())
        if n_nan:
            violations.append(f"{column}: {n_nan} NaN row(s)")

    sums = {c: float(frame[c].fillna(0.0).sum()) for c in _AUDITED_COLUMNS}
    for column, registered, label in (
        ("LabourCost", totals.labour_gbp, "labour"),
        ("TransportCost", totals.transport_gbp, "transport"),
        ("FacilityCost", totals.facility_gbp, "facility"),
    ):
        if abs(sums[column] - registered) > tol:
            violations.append(
                f"{label}: order plane {sums[column]:,.4f} != ledger {registered:,.4f}"
            )
    if abs(holding_gbp - totals.holding_gbp) > tol:
        violations.append(
            f"holding: SKU plane {holding_gbp:,.4f} != ledger {totals.holding_gbp:,.4f}"
        )

    delivered_sum = sums["LabourCost"] + sums["TransportCost"] + sums["FacilityCost"]
    reassembled = delivered_sum + holding_gbp
    if abs(reassembled - totals.total_gbp) > tol:
        violations.append(
            f"total: reassembled {reassembled:,.4f} != ledger {totals.total_gbp:,.4f}"
        )
    if abs(sums["Revenue"] - totals.window_revenue_gbp) > tol:
        violations.append(
            f"revenue: order plane {sums['Revenue']:,.4f} != "
            f"ledger {totals.window_revenue_gbp:,.4f}"
        )

    return CheckResult(
        name="(p) order-level allocation conservation",
        lhs_label="order plane + SKU plane (reassembled)",
        lhs=reassembled,
        rhs_label="ledger total cost",
        rhs=totals.total_gbp,
        tolerance=tol,
        passed=not violations,
        unit="GBP",
        note=(
            "; ".join(violations)
            if violations
            else (
                "every cost line survives being spread over the real orders and "
                "reassembles to the cent; real revenue to the penny; "
                "one row per shipped order"
            )
        ),
    )


# --------------------------------------------------------------------------- #
# Distribution readouts (deterministic; only meaningful once (p) holds)
# --------------------------------------------------------------------------- #
def gini(values: np.ndarray) -> float:
    """Gini concentration of a non-negative distribution (0 equal, ->1 concentrated)."""
    v = np.sort(np.asarray(values, dtype=float))
    n = len(v)
    total = float(v.sum())
    if n == 0 or total <= 0.0:
        return 0.0
    ranks = np.arange(1, n + 1)
    return float(2.0 * float((ranks * v).sum()) / (n * total) - (n + 1) / n)


@dataclass
class DistributionStats:
    """The shape of modelled per-order cost-to-serve, plus model-coverage counts."""

    n_orders: int
    delivered_total_gbp: float
    revenue_total_gbp: float
    mean_gbp: float
    median_gbp: float
    p90_gbp: float
    min_gbp: float
    max_gbp: float
    top_decile_cost_share: float
    gini_delivered: float
    uncovered_orders: int
    uncovered_revenue_gbp: float
    uncovered_delivered_gbp: float

    @property
    def uncovered_share(self) -> float:
        return self.uncovered_orders / self.n_orders if self.n_orders else 0.0


def distribution_stats(frame: pd.DataFrame) -> DistributionStats:
    """Summary statistics of the delivered (order-attributed) cost distribution."""
    cost = frame["DeliveredCost"].to_numpy(dtype=float)
    n = len(cost)
    if n == 0:
        return DistributionStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)
    uncovered = frame.loc[frame["DeliveredCost"] > frame["Revenue"]]
    ranked = np.sort(cost)[::-1]
    top = ranked[: max(1, n // N_DECILES)]
    total = float(cost.sum())
    return DistributionStats(
        n_orders=n,
        delivered_total_gbp=total,
        revenue_total_gbp=float(frame["Revenue"].sum()),
        mean_gbp=float(cost.mean()),
        median_gbp=float(np.median(cost)),
        p90_gbp=float(np.quantile(cost, 0.9)),
        min_gbp=float(cost.min()),
        max_gbp=float(cost.max()),
        top_decile_cost_share=float(top.sum()) / total if total else 0.0,
        gini_delivered=gini(cost),
        uncovered_orders=len(uncovered),
        uncovered_revenue_gbp=float(uncovered["Revenue"].sum()),
        uncovered_delivered_gbp=float(uncovered["DeliveredCost"].sum()),
    )


@dataclass
class WhaleCurve:
    """The cost-to-serve whale: cumulative model-implied coverage gap.

    Orders are sorted best-covered first (largest Gap first, invoice
    tie-break); the running sum climbs to a peak and the expensive tail erodes
    it. The endpoint equals window revenue minus delivered cost EXACTLY, so
    the curve reconciles to the same ledger it was built from.
    """

    cumulative_gbp: np.ndarray = field(repr=False)
    n_orders: int = 0
    peak_gbp: float = 0.0
    peak_orders: int = 0
    end_gbp: float = 0.0

    @property
    def peak_orders_share(self) -> float:
        return self.peak_orders / self.n_orders if self.n_orders else 0.0

    @property
    def erosion_gbp(self) -> float:
        """What the costliest tail takes back off the peak (model-implied)."""
        return self.peak_gbp - self.end_gbp


def whale_curve(frame: pd.DataFrame) -> WhaleCurve:
    """Cumulative model-implied gap, best-covered orders first (deterministic)."""
    if not len(frame):
        return WhaleCurve(cumulative_gbp=np.empty(0))
    ordered = frame.sort_values(["Gap", "Invoice"], ascending=[False, True], kind="stable")
    cumulative = ordered["Gap"].to_numpy(dtype=float).cumsum()
    peak_idx = int(np.argmax(cumulative))
    return WhaleCurve(
        cumulative_gbp=cumulative,
        n_orders=len(cumulative),
        peak_gbp=float(cumulative[peak_idx]),
        peak_orders=peak_idx + 1,
        end_gbp=float(cumulative[-1]),
    )


@dataclass
class DecileRow:
    """One cost-to-serve decile (1 = costliest orders), reconciling to the total."""

    decile: int
    orders: int
    delivered_gbp: float
    revenue_gbp: float
    gap_gbp: float
    uncovered_orders: int
    cost_share: float
    cum_cost_share: float

    @property
    def mean_delivered_gbp(self) -> float:
        return self.delivered_gbp / self.orders if self.orders else 0.0


def decile_table(frame: pd.DataFrame, n_deciles: int = N_DECILES) -> list[DecileRow]:
    """Orders ranked by modelled cost-to-serve, split into deciles (costliest first).

    Deterministic (invoice tie-break); the decile sums reconcile back to the
    frame totals to the cent -- the decomposition of the decomposition still
    adds up, and the tests assert it.
    """
    ordered = frame.sort_values(
        ["DeliveredCost", "Invoice"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    groups = np.array_split(np.arange(len(ordered)), n_deciles)
    total = float(ordered["DeliveredCost"].sum())
    rows: list[DecileRow] = []
    cum = 0.0
    for i, idx in enumerate(groups, start=1):
        part = ordered.iloc[idx]
        delivered = float(part["DeliveredCost"].sum())
        share = delivered / total if total else 0.0
        cum += share
        rows.append(
            DecileRow(
                decile=i,
                orders=len(part),
                delivered_gbp=delivered,
                revenue_gbp=float(part["Revenue"].sum()),
                gap_gbp=float(part["Gap"].sum()),
                uncovered_orders=int((part["DeliveredCost"] > part["Revenue"]).sum()),
                cost_share=share,
                cum_cost_share=cum,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# The full analysis bundle
# --------------------------------------------------------------------------- #
@dataclass
class OrderCosts:
    """Per-order cost-to-serve: identity (p), the distribution, whale and deciles."""

    source: str
    frame: pd.DataFrame = field(repr=False)
    totals: LedgerTotals = field(repr=False)
    identity_p: CheckResult = field(repr=False)
    identities_passed: int
    identities_total: int
    stats: DistributionStats = field(repr=False)
    whale: WhaleCurve = field(repr=False)
    deciles: list[DecileRow] = field(repr=False)

    @property
    def identities_hold(self) -> bool:
        return self.identities_total > 0 and self.identities_passed == self.identities_total

    def all_hold(self) -> bool:
        return self.identity_p.passed and self.identities_hold


def analyse(base: Baseline) -> OrderCosts:
    """Spread the ledger over the real orders, prove (p), read the distribution."""
    cs: CostingResult = base.cs  # type: ignore[assignment]
    frame = order_frame(cs)
    totals = LedgerTotals.from_costing(cs)
    shipped = set(base.ful.orders["Invoice"].astype(str))  # type: ignore[attr-defined]
    identity_p = check_order_allocation(frame, holding_alloc_gbp(cs), totals, shipped)
    _, checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, base.fc, base.plan,
        base.wh, base.ful, base.dl, base.cs, base.expected_revenue_gbp,
    )
    return OrderCosts(
        source=base.source,
        frame=frame,
        totals=totals,
        identity_p=identity_p,
        identities_passed=sum(1 for c in checks if c.passed),
        identities_total=len(checks),
        stats=distribution_stats(frame),
        whale=whale_curve(frame),
        deciles=decile_table(frame),
    )


# --------------------------------------------------------------------------- #
# Formatting + emit (deterministic)
# --------------------------------------------------------------------------- #
def _decile_rows(analysis: OrderCosts) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for r in analysis.deciles:
        rows.append(
            {
                "decile": str(r.decile),
                "orders": str(r.orders),
                "delivered_cost_gbp": f"{r.delivered_gbp:.4f}",
                "revenue_gbp": f"{r.revenue_gbp:.4f}",
                "model_implied_gap_gbp": f"{r.gap_gbp:.4f}",
                "uncovered_orders": str(r.uncovered_orders),
                "cost_share_pct": f"{100 * r.cost_share:.4f}",
                "cum_cost_share_pct": f"{100 * r.cum_cost_share:.4f}",
                "mean_delivered_gbp": f"{r.mean_delivered_gbp:.4f}",
                "cost_provenance": Provenance.SYNTHETIC_ASSIGNED.value,
                "revenue_provenance": Provenance.REAL.value,
            }
        )
    return rows


def write_csv(analysis: OrderCosts, path: Path = ORDER_COSTS_CSV) -> Path:
    """Deterministic CSV (LF newlines, fixed field order)."""
    rows = _decile_rows(analysis)
    fields = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_markdown(analysis: OrderCosts, source: str, path: Path = ORDER_COSTS_MD) -> Path:
    """Deterministic Markdown report the README references."""
    s = analysis.stats
    w = analysis.whale
    p = analysis.identity_p
    lines: list[str] = []
    lines.append("# Per-order cost-to-serve -- the ledger spread over every real order")
    lines.append("")
    lines.append(
        "Stage 5's published allocation rules spread each cost line over the window's "
        "real orders (labour by own pick minutes; transport by carton share of the "
        "delivery day's km; facility equally per order; holding stays on the SKU plane). "
        "Identity (p) machine-checks that the spread loses nothing and invents nothing. "
        "Revenue is REAL; every cost rate is INVENTED (synthetic-assigned, labelled); "
        "'model-uncovered' is a property of the invented cost model, NEVER a profit "
        "claim. Deterministic, so this file regenerates byte-for-byte."
    )
    lines.append("")
    lines.append(f"Source: **{source}**. Regenerate with `python order_costs.py`.")
    lines.append("")
    lines.append(
        f"**Identity (p): {'PASS' if p.passed else 'FAIL'}** -- order plane + SKU plane "
        f"(reassembled) = {p.lhs:,.2f} GBP vs ledger total cost = {p.rhs:,.2f} GBP "
        f"(tolerance {p.tolerance} GBP). {analysis.identities_passed}/"
        f"{analysis.identities_total} cross-stage identities hold on the same run."
    )
    lines.append("")
    lines.append("## The distribution (modelled delivered cost per order)")
    lines.append("")
    lines.append("| measure | value |")
    lines.append("|---|---:|")
    lines.append(f"| orders | {s.n_orders:,} |")
    lines.append(f"| delivered cost (order plane) | {s.delivered_total_gbp:,.2f} GBP |")
    lines.append(f"| holding (SKU plane, not order-attributed) | "
                 f"{analysis.totals.holding_gbp:,.2f} GBP |")
    lines.append(f"| window revenue (REAL) | {s.revenue_total_gbp:,.2f} GBP |")
    lines.append(f"| mean / median per order | {s.mean_gbp:,.2f} / {s.median_gbp:,.2f} GBP |")
    lines.append(f"| p90 / max per order | {s.p90_gbp:,.2f} / {s.max_gbp:,.2f} GBP |")
    lines.append(f"| top-decile share of delivered cost | {100 * s.top_decile_cost_share:.1f}% |")
    lines.append(f"| Gini (delivered cost across orders) | {s.gini_delivered:.3f} |")
    lines.append(
        f"| model-uncovered orders (cost > own revenue, under INVENTED rates) | "
        f"{s.uncovered_orders:,} of {s.n_orders:,} ({100 * s.uncovered_share:.1f}%) |"
    )
    lines.append("")
    lines.append("## Cost-to-serve whale curve (model-implied, reconciling)")
    lines.append("")
    lines.append(
        f"Best-covered orders first, the cumulative model-implied gap peaks at "
        f"**{w.peak_gbp:,.2f} GBP** after {w.peak_orders:,} orders "
        f"({100 * w.peak_orders_share:.1f}% of the book) and ends at "
        f"**{w.end_gbp:,.2f} GBP** = window revenue - delivered cost, exactly; the "
        f"costliest tail takes back {w.erosion_gbp:,.2f} GBP of the peak. Under the "
        "labelled rates this is the SHAPE of cost-to-serve, not a profit statement."
    )
    lines.append("")
    lines.append("## Decile table (orders ranked by modelled cost-to-serve, costliest first)")
    lines.append("")
    lines.append(
        "| decile | orders | delivered (GBP) | revenue (GBP) | gap (GBP) | "
        "uncovered | cost share | cum share | mean/order (GBP) |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in analysis.deciles:
        lines.append(
            f"| {r.decile} | {r.orders:,} | {r.delivered_gbp:,.2f} | {r.revenue_gbp:,.2f} | "
            f"{r.gap_gbp:,.2f} | {r.uncovered_orders:,} | {100 * r.cost_share:.1f}% | "
            f"{100 * r.cum_cost_share:.1f}% | {r.mean_delivered_gbp:,.2f} |"
        )
    lines.append("")
    lines.append("### Honest reading")
    lines.append("")
    lines.append(
        "- Identity (p) is what makes the table trustworthy: the per-order numbers are "
        "not a parallel calculation that could drift from the ledger -- they ARE the "
        "ledger, spread and reassembled to the cent, on the same run where all 13 "
        "cross-stage identities hold."
    )
    lines.append(
        "- Every cost is SYNTHETIC-ASSIGNED (invented rates on invented km/hours/units) "
        "and the facility equal-split is itself a labelled allocation choice -- small "
        "orders look expensive under any per-order spread of a fixed charge. Revenue is "
        "real. 'Model-uncovered' describes which order profiles are expensive to serve "
        "under the stated assumptions; it is never a claim that a real order lost money."
    )
    lines.append(
        "- Holding is deliberately NOT forced onto orders: it is a property of the "
        "stage-2 stock plan, so it stays on the SKU plane and identity (p) checks both "
        "planes jointly against the ledger total."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def print_report(analysis: OrderCosts, source: str) -> bool:
    s = analysis.stats
    w = analysis.whale
    print("=" * 78)
    print("decision-chain -- per-order cost-to-serve (the ledger, spread and reassembled)")
    print("=" * 78)
    print(f"source: {source}")
    print("stage 5's published allocation rules spread the ledger over the real orders;")
    print("identity (p) proves the spread reassembles to the cent on both planes.")
    print()
    print(f"  orders                     {s.n_orders:>14,}")
    print(f"  delivered cost (orders)    {s.delivered_total_gbp:>14,.2f} GBP  "
          f"{Provenance.SYNTHETIC_ASSIGNED.tag()}")
    print(f"  holding (SKU plane)        {analysis.totals.holding_gbp:>14,.2f} GBP  "
          f"{Provenance.SYNTHETIC_ASSIGNED.tag()}")
    print(f"  window revenue             {s.revenue_total_gbp:>14,.2f} GBP  "
          f"{Provenance.REAL.tag()}")
    print(f"  mean / median / p90 / max  {s.mean_gbp:,.2f} / {s.median_gbp:,.2f} / "
          f"{s.p90_gbp:,.2f} / {s.max_gbp:,.2f} GBP per order")
    print(f"  top-decile cost share      {100 * s.top_decile_cost_share:>13.1f}%")
    print(f"  Gini (delivered cost)      {s.gini_delivered:>14.3f}")
    print(f"  model-uncovered orders     {s.uncovered_orders:>14,}  "
          f"({100 * s.uncovered_share:.1f}% of orders; INVENTED rates, not a profit claim)")
    print(f"  whale: peak {w.peak_gbp:,.2f} GBP after {w.peak_orders:,} orders "
          f"({100 * w.peak_orders_share:.1f}%), ends {w.end_gbp:,.2f} GBP "
          f"(= revenue - delivered, exactly)")
    print()
    print(f"  {analysis.identity_p.line()}")
    print(f"         {analysis.identity_p.note}")
    print(
        f"  cross-stage identities: {analysis.identities_passed}/"
        f"{analysis.identities_total} PASS on the same run"
    )
    print()
    print("-" * 78)
    verdict = (
        "the ledger survives being spread over every real order and reassembles exactly"
        if analysis.all_hold()
        else "ALLOCATION CONSERVATION OR AN IDENTITY FAILED"
    )
    print(f"result: {verdict}")
    print("-" * 78)
    return analysis.all_hold()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="order_costs", description=__doc__)
    parser.add_argument(
        "--full", action="store_true",
        help="run on the full UCI dataset instead of the fixture (slow: builds the CVRP baseline)",
    )
    parser.add_argument(
        "--no-emit", action="store_true", help="print only; do not write deliverables"
    )
    args = parser.parse_args(argv)

    base = build_baseline(fixture=not args.full)
    source = (
        "committed real-row fixture (deterministic CI path)"
        if base.source == "fixture"
        else "full UCI Online Retail II dataset"
    )
    analysis = analyse(base)
    ok = print_report(analysis, source)
    if not args.no_emit:
        # Reference the module globals at call time so callers/tests can redirect them.
        csv_path = write_csv(analysis, ORDER_COSTS_CSV)
        md_path = write_markdown(analysis, source, ORDER_COSTS_MD)
        print(f"emitted: {csv_path}")
        print(f"emitted: {md_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
