"""Inter-stage sensitivity: how a forecast error propagates to the final ledger.

The scenario tool (`scenario.py`) proves the ledger still reconciles at ONE
perturbed point (a x1.20 surge on one class). This tool asks the next question
a single-silo forecast demo can never answer: **what is the pound sensitivity of
the whole delivered cost-to-serve to a forecast error, once the forecast is
wired through inventory, fulfilment, transport and costing?** It sweeps a
proportional forecast-demand error across a deterministic grid, re-runs only the
stages that error feeds -- reusing the EXISTING stage engines verbatim (never
re-inventing one) -- and traces the pound impact stage by stage to the
reconciliation ledger, re-checking all 13 cross-stage identities at every point.

The finding is a NEW cross-stage identity the a-n suite does not make:

**Identity (o) -- forecast-error containment.** Under a x``f`` proportional
whole-book forecast surge, the modelled total cost-to-serve equals

    total(f) == base_total + (f - 1) x base_holding      (to the cent)

i.e. the surge moves ONLY the holding line. Labour, transport, facility and the
real window revenue are invariant -- because the physical fulfilment (picks,
cartons, CVRP km, labour hours) stands on the IMMUTABLE real invoice stream, and
the revenue is real. Algebraically this says the arc-elasticity of total cost to
a proportional forecast surge equals the **holding cost share exactly**:

    d(total)% / d(forecast)%  ==  base_holding / base_total

The propagation is EXACT (safety stock is linear in the forecast sigma via the
square-root law, and the holding line is linear in safety stock -- no rounding),
so the identity holds for every grid factor, up and down, and is verified by
actually re-running the inventory + costing engines, not just asserted.

Corollary (dilution). A surge confined to ONE demand class is contained further:
its elasticity of total is the holding share x that class's share of total safety
stock -- so a shock to a class that carries a minority of the buffer moves the
final pound even less. Both numbers are reported as measured.

Honesty is unchanged from the rest of the chain: the forecast is a DERIVED input
(a demand-scale error, labelled derived, never claiming to touch real data);
every cost rate is INVENTED (synthetic-assigned, labelled); revenue is real. The
containment is itself the honest point -- a forecast error simply cannot reach
the delivered-cost lines of an already-real order book, and the reconciliation
proves it lands only on the planning-side holding charge. This is a
cost-structure sensitivity view, never a profit claim.

    python sensitivity.py            # fixture path, emit deliverables, print the ladder
    python sensitivity.py --full     # full dataset (slow: builds the CVRP baseline once)
    python sensitivity.py --no-emit  # print only, do not write deliverables

Exit code is non-zero if identity (o) does not hold, if any physical line moves
under a forecast surge, or if any of the 13 identities fails at any grid point.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

from chain import costing, inventory, paths
from chain.contracts import CheckResult, DemandForecast, Provenance
from chain.reconcile import PENNY
from scenario import (
    Baseline,
    apply_demand_surge,
    build_baseline,
    build_ledger_and_checks,
    most_populous_class,
)

# Deterministic proportional forecast-surge grid; 1.00 is the un-perturbed base.
# Two-sided (shrink and surge) so the sensitivity is a slope, not a single point.
FACTORS: tuple[float, ...] = (0.80, 0.90, 1.00, 1.10, 1.20)
BASE_FACTOR: float = 1.00
CLASS_SURGE_FACTOR: float = 1.20  # the corollary point (single-class dilution)

SENSITIVITY_CSV = paths.DELIVERABLES_DIR / "sensitivity.csv"
SENSITIVITY_MD = paths.DELIVERABLES_DIR / "sensitivity.md"


# --------------------------------------------------------------------------- #
# The perturbation (pure, deterministic, non-mutating) -- whole-book variant
# --------------------------------------------------------------------------- #
def apply_book_surge(fc: DemandForecast, factor: float) -> DemandForecast:
    """A x``factor`` multiplicative forecast surge on the WHOLE book.

    Scales BOTH the point forecast (Units) and its measured uncertainty (Sigma)
    for every SKU -- a proportional, book-wide demand-scale error. Returns a NEW
    DemandForecast; the input frame is not mutated. SKUs, models and classes are
    unchanged, so forecast coverage (identity (d)) is untouched. The class-scoped
    variant is scenario.apply_demand_surge (reused for the dilution corollary).
    """
    frame = fc.forecasts.copy()
    frame["Units"] = frame["Units"] * factor
    frame["Sigma"] = frame["Sigma"] * factor
    return DemandForecast(
        forecasts=frame,
        cv=fc.cv,
        class_winners=fc.class_winners,
        horizon_weeks=fc.horizon_weeks,
        provenance=fc.provenance,
    )


def _cost(cs: object, item: str) -> float:
    return float(cs.line(item).gbp)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# One grid point (one forecast factor, traced to the ledger)
# --------------------------------------------------------------------------- #
@dataclass
class GridPoint:
    """The whole ledger under one forecast factor: the traced cost lines + identities."""

    factor: float
    total_ss: float  # inventory/safety_stock_units under the perturbed plan
    holding_gbp: float
    labour_gbp: float
    transport_gbp: float
    facility_gbp: float
    total_gbp: float
    window_revenue_gbp: float
    identities_passed: int
    identities_total: int

    @property
    def identities_hold(self) -> bool:
        return self.identities_total > 0 and self.identities_passed == self.identities_total


def grid_point(base: Baseline, factor: float) -> GridPoint:
    """Re-run the forecast-fed stages at ``factor`` and trace the ledger.

    Only inventory + costing re-run; the physical stages (warehouse, DES,
    transport) are the baseline's, reused unchanged -- they stand on the
    immutable real invoice stream. All 13 identities are re-checked on the run.
    """
    if factor == BASE_FACTOR:
        fc_used, plan, cs = base.fc, base.plan, base.cs
    else:
        fc_used = apply_book_surge(base.fc, factor)
        plan = inventory.run(fc_used, base.layers)
        cs = costing.run(base.cleaned, base.demand.skus, plan, base.ful, base.dl)

    _, checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, fc_used, plan,
        base.wh, base.ful, base.dl, cs, base.expected_revenue_gbp,
    )
    return GridPoint(
        factor=factor,
        total_ss=float(plan.plan["SafetyStock"].sum()) if len(plan.plan) else 0.0,
        holding_gbp=_cost(cs, "holding"),
        labour_gbp=_cost(cs, "labour"),
        transport_gbp=_cost(cs, "transport"),
        facility_gbp=_cost(cs, "facility"),
        total_gbp=_cost(cs, "total cost"),
        window_revenue_gbp=_cost(cs, "window revenue"),
        identities_passed=sum(1 for c in checks if c.passed),
        identities_total=len(checks),
    )


def build_grid(base: Baseline, factors: tuple[float, ...] = FACTORS) -> list[GridPoint]:
    return [grid_point(base, f) for f in factors]


def base_point(grid: list[GridPoint]) -> GridPoint:
    return next(p for p in grid if p.factor == BASE_FACTOR)


def holding_share(point: GridPoint) -> float:
    """base holding / base total -- the analytic elasticity of total to a surge."""
    return point.holding_gbp / point.total_gbp if point.total_gbp else 0.0


def arc_elasticity(base_val: float, val: float, factor: float) -> float | None:
    """Proportional response d(val)% / d(factor)% relative to the base (None at base)."""
    if factor == BASE_FACTOR or base_val == 0.0:
        return None
    return ((val - base_val) / base_val) / (factor - BASE_FACTOR)


# --------------------------------------------------------------------------- #
# Identity (o) -- forecast-error containment
# --------------------------------------------------------------------------- #
# Ledger lines that a forecast surge MUST NOT move (they stand on the immutable
# real invoice stream / real revenue).
INVARIANT_LINES: tuple[str, ...] = ("labour", "transport", "facility", "window revenue")


def _invariant_value(point: GridPoint, line: str) -> float:
    return {
        "labour": point.labour_gbp,
        "transport": point.transport_gbp,
        "facility": point.facility_gbp,
        "window revenue": point.window_revenue_gbp,
    }[line]


def check_forecast_containment(
    base: GridPoint, grid: list[GridPoint], tol: float = PENNY
) -> CheckResult:
    """(o) a whole-book forecast surge moves only the holding line, to the cent.

    ``passed`` requires, at EVERY off-base grid factor: (1) total ==
    base_total + (f-1) x base_holding to the cent (the surge lands only on
    holding); (2) labour / transport / facility / window revenue are invariant
    to the cent (the delivered-cost lines and the real revenue cannot move);
    (3) all 13 cross-stage identities still hold. lhs/rhs are printed at the top
    surge factor; the note reports any violation.
    """
    offbase = [p for p in grid if p.factor != base.factor]
    violations: list[str] = []
    for p in offbase:
        predicted = base.total_gbp + (p.factor - base.factor) * base.holding_gbp
        if abs(p.total_gbp - predicted) > tol:
            violations.append(
                f"total@x{p.factor:.2f} {p.total_gbp:,.2f} != predicted {predicted:,.2f}"
            )
        for line in INVARIANT_LINES:
            moved = abs(_invariant_value(p, line) - _invariant_value(base, line))
            if moved > tol:
                violations.append(f"{line}@x{p.factor:.2f} moved by {moved:,.4f}")
        if not p.identities_hold:
            violations.append(
                f"identities@x{p.factor:.2f} {p.identities_passed}/{p.identities_total}"
            )

    top = max(offbase, key=lambda p: p.factor) if offbase else base
    predicted_top = base.total_gbp + (top.factor - base.factor) * base.holding_gbp
    return CheckResult(
        name="(o) forecast-error containment",
        lhs_label=f"total cost @ surge x{top.factor:.2f}",
        lhs=top.total_gbp,
        rhs_label="base total + surge x holding line",
        rhs=predicted_top,
        tolerance=tol,
        passed=not violations,
        unit="GBP",
        note=(
            "; ".join(violations)
            if violations
            else (
                "a whole-book forecast surge moves only the holding line; "
                "labour/transport/facility/revenue invariant; "
                "13/13 identities hold at every grid factor"
            )
        ),
    )


# --------------------------------------------------------------------------- #
# Dilution corollary -- a single-class surge is contained further
# --------------------------------------------------------------------------- #
@dataclass
class ClassContainment:
    """A single-class forecast surge: measured vs predicted (share x class SS fraction)."""

    target_class: str
    factor: float
    class_ss: float
    total_ss: float
    holding_share: float
    measured_elasticity: float
    predicted_elasticity: float
    identities_passed: int
    identities_total: int

    @property
    def class_ss_fraction(self) -> float:
        return self.class_ss / self.total_ss if self.total_ss else 0.0

    @property
    def identities_hold(self) -> bool:
        return self.identities_total > 0 and self.identities_passed == self.identities_total

    @property
    def holds(self) -> bool:
        """The dilution law reconstructs the measured elasticity to numerical zero."""
        return (
            self.identities_hold
            and abs(self.measured_elasticity - self.predicted_elasticity) <= 1e-9
        )


def class_containment(
    base: Baseline,
    base_pt: GridPoint,
    target_class: str | None = None,
    factor: float = CLASS_SURGE_FACTOR,
) -> ClassContainment:
    """Surge ONE class (reusing scenario.apply_demand_surge) and measure the dilution."""
    target_class = target_class or most_populous_class(base.fc)
    fc2 = apply_demand_surge(base.fc, target_class, factor)
    plan2 = inventory.run(fc2, base.layers)
    cs2 = costing.run(base.cleaned, base.demand.skus, plan2, base.ful, base.dl)
    _, checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, fc2, plan2,
        base.wh, base.ful, base.dl, cs2, base.expected_revenue_gbp,
    )
    total2 = _cost(cs2, "total cost")
    measured = ((total2 - base_pt.total_gbp) / base_pt.total_gbp) / (factor - BASE_FACTOR)

    surged = set(base.fc.forecasts.loc[base.fc.forecasts["Class"] == target_class, "StockCode"])
    ss = base.plan.plan.set_index("StockCode")["SafetyStock"]
    class_ss = float(ss.loc[ss.index.isin(surged)].sum())
    total_ss = float(base.plan.plan["SafetyStock"].sum())
    share = holding_share(base_pt)
    predicted = share * (class_ss / total_ss if total_ss else 0.0)
    return ClassContainment(
        target_class=target_class,
        factor=factor,
        class_ss=class_ss,
        total_ss=total_ss,
        holding_share=share,
        measured_elasticity=measured,
        predicted_elasticity=predicted,
        identities_passed=sum(1 for c in checks if c.passed),
        identities_total=len(checks),
    )


# --------------------------------------------------------------------------- #
# The full analysis bundle
# --------------------------------------------------------------------------- #
@dataclass
class Sensitivity:
    """The forecast-error sensitivity ladder + identity (o) + the dilution corollary."""

    source: str
    grid: list[GridPoint]
    base: GridPoint
    identity_o: CheckResult
    dilution: ClassContainment

    @property
    def holding_share(self) -> float:
        return holding_share(self.base)

    @property
    def elasticity_of_total(self) -> float:
        """Analytic == measured (linear chain): the elasticity of total to the surge."""
        return self.holding_share

    def all_hold(self) -> bool:
        return (
            self.identity_o.passed
            and all(p.identities_hold for p in self.grid)
            and self.dilution.holds
        )


def analyse(base: Baseline) -> Sensitivity:
    """Sweep the grid, check identity (o), and measure the dilution corollary."""
    grid = build_grid(base)
    base_pt = base_point(grid)
    return Sensitivity(
        source=base.source,
        grid=grid,
        base=base_pt,
        identity_o=check_forecast_containment(base_pt, grid),
        dilution=class_containment(base, base_pt),
    )


# --------------------------------------------------------------------------- #
# Formatting + emit (deterministic)
# --------------------------------------------------------------------------- #
def _rows(sens: Sensitivity) -> list[dict[str, str]]:
    base = sens.base
    rows: list[dict[str, str]] = []
    for p in sens.grid:
        elas = arc_elasticity(base.total_gbp, p.total_gbp, p.factor)
        rows.append(
            {
                "factor": f"{p.factor:.2f}",
                "total_safety_stock_units": f"{p.total_ss:.4f}",
                "holding_gbp": f"{p.holding_gbp:.4f}",
                "labour_gbp": f"{p.labour_gbp:.4f}",
                "transport_gbp": f"{p.transport_gbp:.4f}",
                "facility_gbp": f"{p.facility_gbp:.4f}",
                "total_gbp": f"{p.total_gbp:.4f}",
                "window_revenue_gbp": f"{p.window_revenue_gbp:.4f}",
                "elasticity_of_total": "n/a" if elas is None else f"{elas:.6f}",
                "identities": f"{p.identities_passed}/{p.identities_total}",
                "input_provenance": Provenance.DERIVED.value,
            }
        )
    return rows


def write_csv(sens: Sensitivity, path: Path = SENSITIVITY_CSV) -> Path:
    """Deterministic CSV (LF newlines, fixed field order)."""
    rows = _rows(sens)
    fields = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_markdown(sens: Sensitivity, source: str, path: Path = SENSITIVITY_MD) -> Path:
    """Deterministic Markdown ladder the README references."""
    base = sens.base
    o = sens.identity_o
    d = sens.dilution
    lines: list[str] = []
    lines.append("# Inter-stage sensitivity -- a forecast error, traced to the ledger")
    lines.append("")
    lines.append(
        "A proportional forecast-demand error is swept across a deterministic grid; only "
        "the stages it feeds (inventory -> costing) re-run, reusing the existing engines, "
        "and all 13 cross-stage identities are re-checked at every point. The forecast is "
        "a `derived` input; every cost rate is INVENTED (synthetic-assigned); revenue is "
        "real. Deterministic, so this file regenerates byte-for-byte."
    )
    lines.append("")
    lines.append(f"Source: **{source}**. Regenerate with `python sensitivity.py`.")
    lines.append("")
    lines.append(
        f"**Identity (o): {'PASS' if o.passed else 'FAIL'}** -- "
        f"total cost @ surge x{sens.grid[-1].factor:.2f} = {o.lhs:,.2f} GBP vs "
        f"base total + surge x holding line = {o.rhs:,.2f} GBP (tolerance {o.tolerance} GBP). "
        "A whole-book forecast surge moves ONLY the holding line."
    )
    lines.append("")
    lines.append(
        f"Elasticity of total cost to the forecast surge = **{sens.elasticity_of_total:.4f}** "
        f"(== the holding cost share, {100 * sens.holding_share:.2f}%): a doubling of forecast "
        f"demand raises modelled cost-to-serve by {100 * sens.holding_share:.2f}%, and nothing "
        "else in the ledger moves."
    )
    lines.append("")
    lines.append(
        "| forecast factor | safety stock (u) | holding (GBP) | transport (GBP) | "
        "labour (GBP) | facility (GBP) | total (GBP) | window revenue (GBP) | elasticity of total |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in sens.grid:
        elas = arc_elasticity(base.total_gbp, p.total_gbp, p.factor)
        marker = " (base)" if p.factor == BASE_FACTOR else ""
        lines.append(
            f"| x{p.factor:.2f}{marker} | {p.total_ss:,.1f} | {p.holding_gbp:,.2f} | "
            f"{p.transport_gbp:,.2f} | {p.labour_gbp:,.2f} | {p.facility_gbp:,.2f} | "
            f"{p.total_gbp:,.2f} | {p.window_revenue_gbp:,.2f} | "
            f"{'--' if elas is None else f'{elas:.4f}'} |"
        )
    lines.append("")
    lines.append("### Honest reading")
    lines.append("")
    lines.append(
        "- The physical, delivered-cost part of the chain (labour, transport) and the "
        "real window revenue are **invariant** to a forecast error, to the cent -- they "
        "stand on the immutable real invoice stream. A forecast error is therefore "
        "*contained* to the planning-side holding charge; that containment IS the finding, "
        "and it is what no single-silo forecast demo can show."
    )
    lines.append(
        f"- Dilution corollary: a surge confined to the **{d.target_class}** class "
        f"(which carries {100 * d.class_ss_fraction:.1f}% of total safety stock) has "
        f"elasticity of total {d.measured_elasticity:.4f} = the holding share "
        f"{d.holding_share:.4f} x that {d.class_ss_fraction:.4f} fraction "
        f"(measured == predicted; {d.identities_passed}/{d.identities_total} identities hold)."
    )
    lines.append(
        "- No overclaim: this is a cost-structure sensitivity under labelled synthetic "
        "rates and lead times, not a profit statement; the real vs synthetic-assigned "
        "boundary is exactly the baseline's, and the forecast surge is `derived`, never "
        "presented as real data."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def print_report(sens: Sensitivity, source: str) -> bool:
    print("=" * 78)
    print("decision-chain -- inter-stage sensitivity (a forecast error, traced to the ledger)")
    print("=" * 78)
    print(f"source: {source}")
    print(
        "a proportional forecast surge is swept; only inventory + costing re-run (existing"
    )
    print("engines); all 13 cross-stage identities are re-checked at every grid factor.")
    print()
    header = (
        f"  {'factor':>7} {'safety stock':>14} {'holding':>12} {'transport':>13} "
        f"{'total':>13} {'revenue':>13} {'elasticity':>11}  identities"
    )
    print(header)
    base = sens.base
    for p in sens.grid:
        elas = arc_elasticity(base.total_gbp, p.total_gbp, p.factor)
        elas_str = "  --  " if elas is None else f"{elas:.4f}"
        tag = " (base)" if p.factor == BASE_FACTOR else ""
        print(
            f"  x{p.factor:>5.2f} {p.total_ss:>14,.1f} {p.holding_gbp:>12,.2f} "
            f"{p.transport_gbp:>13,.2f} {p.total_gbp:>13,.2f} {p.window_revenue_gbp:>13,.2f} "
            f"{elas_str:>11}  {p.identities_passed}/{p.identities_total}{tag}"
        )
    print()
    print(
        f"  elasticity of total cost to the surge = {sens.elasticity_of_total:.4f} "
        f"(== holding share {100 * sens.holding_share:.2f}%)"
    )
    print(f"  {sens.identity_o.line()}")
    print(f"         {sens.identity_o.note}")
    d = sens.dilution
    print(
        f"  dilution: {d.target_class} surge x{d.factor:.2f} -> elasticity "
        f"{d.measured_elasticity:.4f} = share {d.holding_share:.4f} x SS-fraction "
        f"{d.class_ss_fraction:.4f} "
        f"({'holds' if d.holds else 'FAIL'}, {d.identities_passed}/{d.identities_total} identities)"
    )
    print()
    print("-" * 78)
    verdict = (
        "forecast error contained to the holding line; every identity holds at every factor"
        if sens.all_hold()
        else "CONTAINMENT OR AN IDENTITY FAILED"
    )
    print(f"result: {verdict}")
    print("-" * 78)
    return sens.all_hold()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="sensitivity", description=__doc__)
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
    sens = analyse(base)
    ok = print_report(sens, source)
    if not args.no_emit:
        # Reference the module globals at call time so callers/tests can redirect them.
        csv_path = write_csv(sens, SENSITIVITY_CSV)
        md_path = write_markdown(sens, source, SENSITIVITY_MD)
        print(f"emitted: {csv_path}")
        print(f"emitted: {md_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
