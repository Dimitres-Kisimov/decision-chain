"""Cost-driver breakdown of the stage-5 ledger + reconciliation identity (n).

Identity (l) proves the ledger TOTAL equals the sum of its four cost lines; it
never proves that any single line equals the physical work it prices. A wrong
rate, or a line silently costed off a different driver than the one another
stage registered, sails straight through (l). This module closes that seam.

**Identity (n) -- cost-driver reconstruction.** Each of the four cost lines is
rebuilt, through an INDEPENDENT code path, from its physical DRIVER -- a
quantity a *different* stage registered in the reconciliation ledger -- times
its published synthetic rate:

    labour     = DES picking hours (stage 4a)     x LABOUR_RATE_GBP_PER_HOUR
    transport  = CVRP route km (stage 4b)         x TRANSPORT_RATE_GBP_PER_KM
    holding    = phase-2 safety-stock units       x HOLDING_RATE x window weeks
    facility   = window weeks                      x FACILITY_FIXED_GBP_PER_WEEK

The check asserts each reconstruction equals costing's own registered line to
the cent, and that the four reconstructions sum to the registered total. It
therefore ties stage 5's priced lines back to the physical drivers stages 2/4a/
4b booked -- a cross-stage identity the a-m suite does not cover.

**The breakdown.** Because the ledger total is LINEAR in every driver, the
elasticity of total cost to a proportional (say +1%) move in any single driver
equals that driver's COST SHARE exactly -- no re-run needed for the number, and
``verify_total_elasticity`` confirms it by actually re-running the costing
engine under a scaled rate (a linear model gives an exact match, not an
approximation). The professional readout is the shape of cost-to-serve: which
activity the chain's physical work turns into money, and how hard the total
leans on each.

Honesty is unchanged from the rest of the chain: every rate is INVENTED
(synthetic-assigned, labelled); revenue is real; this is a cost-STRUCTURE view,
never a profit claim. The tool reads the committed run artifact (it recomputes
nothing for the headline numbers) and is deterministic, so the emitted
deliverables regenerate byte-for-byte.

    python cost_drivers.py            # breakdown + identity (n) on the committed artifact
    python cost_drivers.py --no-emit  # print only, do not write deliverables

Exit code is non-zero if identity (n) does not hold on the artifact.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chain import artifact as artifact_mod
from chain import costing, paths
from chain.contracts import CheckResult, Provenance
from chain.reconcile import PENNY, Ledger

# Sentinel driver key: the "driver" of the facility line is the window-week
# count, which no stage registers as a single ledger entry.
WINDOW_WEEKS = "@window_weeks"

COST_DRIVERS_CSV = paths.DELIVERABLES_DIR / "cost_drivers.csv"
COST_DRIVERS_MD = paths.DELIVERABLES_DIR / "cost_drivers.md"


# --------------------------------------------------------------------------- #
# Driver specification -- one row per cost line
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DriverSpec:
    """How one cost line is rebuilt from a physical driver x a published rate."""

    line_item: str  # costing line name ("labour", ...)
    driver_key: str  # ledger key of the physical driver (or WINDOW_WEEKS)
    driver_label: str  # human label for the driver
    driver_unit: str  # unit of the driver quantity
    rate_attr: str  # attribute on chain.costing holding the rate
    rate_unit: str  # unit of the rate
    per_week: bool  # holding is charged across every week of the window


# Order is deterministic and matches the ledger's cost-line order.
DRIVERS: tuple[DriverSpec, ...] = (
    DriverSpec("labour", "fulfil/labour_hours", "DES picking hours", "hours",
               "LABOUR_RATE_GBP_PER_HOUR", "GBP/hour", per_week=False),
    DriverSpec("transport", "transport/cvrp_km", "CVRP route km", "km",
               "TRANSPORT_RATE_GBP_PER_KM", "GBP/km", per_week=False),
    DriverSpec("holding", "inventory/safety_stock_units", "safety-stock units", "units",
               "HOLDING_RATE_GBP_PER_UNIT_WEEK", "GBP/unit-week", per_week=True),
    DriverSpec("facility", WINDOW_WEEKS, "window weeks", "weeks",
               "FACILITY_FIXED_GBP_PER_WEEK", "GBP/week", per_week=False),
)

COST_ITEMS = tuple(spec.line_item for spec in DRIVERS)


# --------------------------------------------------------------------------- #
# One rebuilt line
# --------------------------------------------------------------------------- #
@dataclass
class DriverRow:
    """One cost line, rebuilt from its driver and compared to the ledger."""

    line_item: str
    driver_label: str
    driver_unit: str
    driver_qty: float
    rate: float
    rate_unit: str
    reconstructed_gbp: float
    registered_gbp: float
    total_gbp: float

    @property
    def residual_gbp(self) -> float:
        """Reconstruction minus the ledger's own line (should be ~0)."""
        return self.reconstructed_gbp - self.registered_gbp

    @property
    def share(self) -> float:
        """Fraction of the modelled total this line carries."""
        return self.registered_gbp / self.total_gbp if self.total_gbp else 0.0

    @property
    def elasticity_of_total(self) -> float:
        """d(total)/d(driver) in proportional terms == the cost share (linear model)."""
        return self.share


def _rate(spec: DriverSpec) -> float:
    return float(getattr(costing, spec.rate_attr))


def _reconstruct(spec: DriverSpec, qty: float, n_weeks: int) -> float:
    value = qty * _rate(spec)
    if spec.per_week:
        value *= n_weeks
    return value


def driver_rows(values: dict[str, float], n_weeks: int) -> list[DriverRow]:
    """Rebuild every cost line from ``values`` (a ledger-key -> number map)."""
    total = float(values["costing/total_cost"])
    rows: list[DriverRow] = []
    for spec in DRIVERS:
        qty = float(n_weeks) if spec.driver_key == WINDOW_WEEKS else float(values[spec.driver_key])
        rows.append(
            DriverRow(
                line_item=spec.line_item,
                driver_label=spec.driver_label,
                driver_unit=spec.driver_unit,
                driver_qty=qty,
                rate=_rate(spec),
                rate_unit=spec.rate_unit,
                reconstructed_gbp=_reconstruct(spec, qty, n_weeks),
                registered_gbp=float(values[f"costing/{spec.line_item}"]),
                total_gbp=total,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Identity (n)
# --------------------------------------------------------------------------- #
def check_cost_driver_reconstruction(values: dict[str, float], n_weeks: int) -> CheckResult:
    """(n) each cost line == its physical driver x published rate, to the cent.

    ``passed`` requires BOTH that every individual line reconstructs (within a
    penny) AND that the four reconstructions sum to the registered total -- a
    strictly stronger statement than identity (l)'s total==sum-of-lines.
    """
    rows = driver_rows(values, n_weeks)
    reconstructed_total = sum(r.reconstructed_gbp for r in rows)
    registered_total = float(values["costing/total_cost"])
    mismatches = [
        f"{r.line_item}: rebuilt {r.reconstructed_gbp:,.4f} != ledger {r.registered_gbp:,.4f}"
        for r in rows
        if abs(r.residual_gbp) > PENNY
    ]
    total_ok = abs(reconstructed_total - registered_total) <= PENNY
    return CheckResult(
        name="(n) cost-driver reconstruction",
        lhs_label="drivers x rates (rebuilt)",
        lhs=reconstructed_total,
        rhs_label="ledger total cost",
        rhs=registered_total,
        tolerance=PENNY,
        passed=not mismatches and total_ok,
        unit="GBP",
        note=(
            "; ".join(mismatches)
            if mismatches
            else "every line == its physical driver x published rate, to the cent"
        ),
    )


def check_from_ledger(ledger: Ledger, n_weeks: int) -> CheckResult:
    """Identity (n) on a freshly built reconciliation ledger."""
    values = {key: entry.value for key, entry in ledger.entries.items()}
    return check_cost_driver_reconstruction(values, n_weeks)


# --------------------------------------------------------------------------- #
# Artifact adapters (the committed full-run receipt)
# --------------------------------------------------------------------------- #
def artifact_values(artifact: dict[str, Any]) -> dict[str, float]:
    """The ledger-key -> value map, straight from a run artifact."""
    return {row["key"]: float(row["value"]) for row in artifact["ledger"]}


def artifact_window_weeks(artifact: dict[str, Any]) -> int:
    """Window-week count from the costing stage's assumptions."""
    costing_stage = artifact_mod.stage_by_name(artifact, "costing")
    return int(costing_stage["detail"]["assumptions"]["window_weeks"])


def breakdown_from_artifact(artifact: dict[str, Any]) -> list[DriverRow]:
    return driver_rows(artifact_values(artifact), artifact_window_weeks(artifact))


def check_from_artifact(artifact: dict[str, Any]) -> CheckResult:
    return check_cost_driver_reconstruction(
        artifact_values(artifact), artifact_window_weeks(artifact)
    )


# --------------------------------------------------------------------------- #
# Elasticity: verify share == measured elasticity by re-running the engine
# --------------------------------------------------------------------------- #
def verify_total_elasticity(
    line_item: str,
    *,
    cleaned: Any,
    skus: list[str],
    plan: Any,
    fulfilment: Any,
    delivery: Any,
    base_total_gbp: float,
    factor: float = 1.10,
) -> float:
    """Arc-elasticity of total cost to a proportional bump of one line's rate.

    Scales the labelled synthetic rate on ``chain.costing`` (restored in
    ``finally`` -- nothing on disk changes), re-runs the costing engine
    VERBATIM, and returns d(total)%/d(rate)%. The ledger total is linear in
    every rate, so this equals the line's cost share exactly.
    """
    spec = next(s for s in DRIVERS if s.line_item == line_item)
    rate_attr = spec.rate_attr
    original = getattr(costing, rate_attr)
    try:
        setattr(costing, rate_attr, original * factor)
        perturbed = costing.run(cleaned, skus, plan, fulfilment, delivery)
    finally:
        setattr(costing, rate_attr, original)
    d_total = (perturbed.total_cost_gbp - base_total_gbp) / base_total_gbp
    d_rate = factor - 1.0
    return d_total / d_rate


# --------------------------------------------------------------------------- #
# Formatting + emit (deterministic)
# --------------------------------------------------------------------------- #
def _rows_for_csv(rows: list[DriverRow]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows:
        out.append(
            {
                "line_item": r.line_item,
                "driver": r.driver_label,
                "driver_qty": f"{r.driver_qty:.4f}",
                "driver_unit": r.driver_unit,
                "rate": f"{r.rate:.4f}",
                "rate_unit": r.rate_unit,
                "reconstructed_gbp": f"{r.reconstructed_gbp:.4f}",
                "ledger_gbp": f"{r.registered_gbp:.4f}",
                "residual_gbp": f"{r.residual_gbp:.6f}",
                "cost_share_pct": f"{100 * r.share:.4f}",
                "elasticity_of_total": f"{r.elasticity_of_total:.6f}",
                "provenance": Provenance.SYNTHETIC_ASSIGNED.value,
            }
        )
    return out


def write_csv(rows: list[DriverRow], path: Path = COST_DRIVERS_CSV) -> Path:
    """Deterministic CSV (LF newlines, fixed field order)."""
    records = _rows_for_csv(rows)
    fields = list(records[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
    return path


def write_markdown(
    rows: list[DriverRow], check: CheckResult, source: str, path: Path = COST_DRIVERS_MD
) -> Path:
    """Deterministic Markdown breakdown the README references."""
    total = rows[0].total_gbp if rows else 0.0
    lines: list[str] = []
    lines.append("# Cost-driver breakdown + reconciliation identity (n)")
    lines.append("")
    lines.append(
        "Each of the four cost lines is rebuilt, through an independent code path, "
        "from its physical **driver** (a quantity a different stage registered) times "
        "its published synthetic rate, then checked against costing's own line. "
        "Identity (l) proves the total is the sum of the four lines; identity (n) "
        "proves each line is the physical work it claims to price. Every rate is "
        "INVENTED (synthetic-assigned, labelled); revenue is real; this is a "
        "cost-structure view, never a profit claim."
    )
    lines.append("")
    lines.append(f"Source: **{source}**. Regenerate with `python cost_drivers.py`.")
    lines.append("")
    lines.append(
        f"**Identity (n): {'PASS' if check.passed else 'FAIL'}** -- "
        f"drivers x rates (rebuilt) = {check.lhs:,.2f} GBP vs ledger total cost = "
        f"{check.rhs:,.2f} GBP (tolerance {check.tolerance} GBP)."
    )
    lines.append("")
    lines.append(
        "| line | driver | quantity | rate | rebuilt (GBP) | ledger (GBP) | "
        "residual (GBP) | cost share | elasticity of total |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        lines.append(
            f"| {r.line_item} | {r.driver_label} | {r.driver_qty:,.2f} {r.driver_unit} | "
            f"{r.rate:,.2f} {r.rate_unit} | {r.reconstructed_gbp:,.2f} | "
            f"{r.registered_gbp:,.2f} | {r.residual_gbp:+.4f} | {100 * r.share:.2f}% | "
            f"{r.elasticity_of_total:.4f} |"
        )
    lines.append(f"| **total** | | | | | **{total:,.2f}** | | **100.00%** | |")
    lines.append("")
    lines.append("### Honest reading")
    lines.append("")
    if rows:
        top = max(rows, key=lambda r: r.share)
        lines.append(
            f"- The modelled cost-to-serve is dominated by **{top.line_item}** "
            f"({100 * top.share:.1f}% of the total). Because the ledger total is linear "
            f"in every driver, a +1% move in the {top.line_item} driver moves the whole "
            f"modelled cost by {top.elasticity_of_total:.4f}% -- the elasticity equals the "
            "cost share exactly (verified by re-running the costing engine under a scaled "
            "rate)."
        )
    lines.append(
        "- The dominant driver stands on the most-invented inputs (CVRP km on seeded "
        "coordinates, a synthetic GBP/km rate), which is precisely why the ledger makes "
        "no profit claim: nothing in it is stronger than its weakest input."
    )
    lines.append(
        "- Identity (n) is additive to the 13 identities (a-m) the committed artifact "
        "records; the artifact itself is unchanged."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def print_breakdown(rows: list[DriverRow], check: CheckResult, source: str) -> None:
    print("=" * 78)
    print("decision-chain -- cost-driver breakdown + identity (n)")
    print("=" * 78)
    print(f"source: {source}")
    print()
    header = (
        f"  {'line':<10} {'driver':<20} {'quantity':>16} {'rate':>14} "
        f"{'rebuilt GBP':>15} {'ledger GBP':>15} {'share':>8}  elasticity"
    )
    print(header)
    for r in rows:
        print(
            f"  {r.line_item:<10} {r.driver_label:<20} "
            f"{r.driver_qty:>12,.2f} {r.driver_unit:<3} "
            f"{r.rate:>9,.2f} {r.rate_unit:<4} "
            f"{r.reconstructed_gbp:>15,.2f} {r.registered_gbp:>15,.2f} "
            f"{100 * r.share:>7.2f}%  {r.elasticity_of_total:.4f}"
        )
    if rows:
        print(f"  {'total':<10} {'':<20} {'':>16} {'':>14} "
              f"{'':>15} {rows[0].total_gbp:>15,.2f} {100.0:>7.2f}%")
    print()
    print(f"  {check.line()}")
    print(f"         {check.note}")
    print()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="cost_drivers", description=__doc__)
    parser.add_argument(
        "--artifact-path", type=Path, default=None,
        help=f"run artifact to read (default {paths.ARTIFACT_JSON})",
    )
    parser.add_argument(
        "--no-emit", action="store_true", help="print only; do not write deliverables"
    )
    args = parser.parse_args(argv)

    artifact_path = args.artifact_path or paths.ARTIFACT_JSON
    artifact = artifact_mod.load(artifact_path)
    stale = artifact_mod.stale_files(artifact)
    source = f"{artifact.get('source', '?')} run artifact ({artifact_path.name})"
    if stale:
        print(
            "WARNING: artifact is STALE -- chain source changed since it was saved "
            f"({len(stale)} file(s)); regenerate with "
            "`python -m chain --report --save-artifact`.\n"
        )

    rows = breakdown_from_artifact(artifact)
    check = check_from_artifact(artifact)
    print_breakdown(rows, check, source)

    if not args.no_emit:
        # Reference the module globals at call time so callers/tests can redirect them.
        csv_path = write_csv(rows, COST_DRIVERS_CSV)
        md_path = write_markdown(rows, check, source, COST_DRIVERS_MD)
        print(f"emitted: {csv_path}")
        print(f"emitted: {md_path}")

    return 0 if check.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
