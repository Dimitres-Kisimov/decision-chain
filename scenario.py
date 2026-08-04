"""Scenario / shock what-if: perturb ONE input, trace it through the WHOLE
chain to the reconciliation ledger, and PROVE every identity still holds.

A silo can survive a shock and still be wrong at the seams. This tool takes a
fully-built baseline chain (stages 0-5 + the 13 identity checks), perturbs a
SINGLE input, re-runs only the stages that input feeds -- reusing the EXISTING
stage engines verbatim, never re-inventing one -- and then re-runs all 13
cross-stage reconciliation identities on the perturbed run. The point of the
exercise is not the deltas (though it reports every one, stage by stage and on
the ledger): it is that the ledger STILL RECONCILES after the shock.

Two labelled scenarios ship:

* ``demand-surge``  -- a x1.20 MULTIPLICATIVE demand shock on one demand class:
  both the point forecast and its measured uncertainty scale by 1.20 (a
  larger-scale demand process). This perturbs a DERIVED input (the forecast),
  so it flows forecast -> inventory (safety stock and order-up-to rise 20% for
  that class) -> costing (the holding charge on that buffer rises with it) ->
  the ledger total. It does NOT touch the physical stages: the warehouse picks,
  the DES, the cartons, the CVRP km and the labour hours are all driven by the
  REAL invoice stream, which is immutable, so those cost lines and every
  physical identity are unchanged BY CONSTRUCTION. The honest reading: a
  demand-forecast surge is a PLANNING-side shock, and the reconciliation proves
  it lands only where it should.

* ``cost-rate``     -- a x1.20 shock to a synthetic-assigned cost RATE (the
  transport GBP/km). This perturbs a labelled SYNTHETIC-ASSIGNED input at the
  costing stage; the km themselves are unchanged (the rate rose, not the
  distance), the biggest cost line rises 20%, the total rises with it, and the
  additivity + revenue identities still close.

Provenance discipline is preserved end to end: the real vs synthetic-assigned
boundary is exactly the baseline's -- a demand surge on the DERIVED forecast
never claims to touch real data, and the cost-rate shock is labelled
synthetic-assigned. Real data is immutable; the synthetic layers keep seed 42.

Determinism: every number below is a function of the deterministic chain, so
re-running emits byte-identical CSV/Markdown. The committed deliverables are
the fixture CI path (the committed ~2k-row real-row sample); ``--full`` runs
the identical tool on the full UCI dataset (slow: it builds the 48-day CVRP
baseline once).

    python scenario.py            # fixture path, emit deliverables, print deltas
    python scenario.py --full     # full dataset (slow, ~51 min baseline build)
    python scenario.py --no-emit  # print only, do not write deliverables

Exit code is non-zero if ANY identity fails to hold under ANY perturbed run.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

from chain import (
    costing,
    deliver,
    fulfil,
    ingest,
    inventory,
    paths,
    reconcile,
    synthetic,
    warehouse,
)
from chain import forecast as forecast_stage
from chain.contracts import CheckResult, DemandForecast, Provenance

SURGE_FACTOR = 1.20
COST_SHOCK_FACTOR = 1.20
N_SKUS_FIXTURE = 25

SCENARIO_CSV = paths.DELIVERABLES_DIR / "scenarios.csv"
SCENARIO_MD = paths.DELIVERABLES_DIR / "scenarios.md"


# --------------------------------------------------------------------------- #
# Baseline bundle -- one fully-built chain the scenarios perturb
# --------------------------------------------------------------------------- #
@dataclass
class Baseline:
    """A fully-built chain (stages 0-5) plus the expected cross-repo revenue.

    Everything a scenario needs to perturb one input and re-run downstream.
    The physical stages (wh/ful/dl) are forecast-independent, so a demand
    scenario reuses them unchanged.
    """

    source: str
    expected_revenue_gbp: float
    cleaned: object
    demand: object
    stream: object
    fc: DemandForecast
    layers: object
    plan: object
    wh: object
    ful: object
    dl: object
    cs: object


def build_baseline(fixture: bool = True, n_skus: int | None = None) -> Baseline:
    """Run stages 0-5 once (reusing every stage engine) into a Baseline."""
    if fixture:
        raw = ingest.load_fixture()
        import json

        expected = json.loads(paths.FIXTURE_EXPECTED.read_text(encoding="utf-8"))[
            "cleaned_revenue_gbp"
        ]
        n_skus = n_skus or N_SKUS_FIXTURE
    else:
        raw = ingest.load_raw()
        expected = reconcile.PUBLISHED_REVENUE_GBP
        n_skus = n_skus or ingest.N_TOP_SKUS

    cleaned, demand, stream = ingest.run(raw, n_skus=n_skus)
    fc = forecast_stage.run(demand)
    descriptions = synthetic.modal_descriptions(cleaned.sales, demand.skus)
    classes = (
        fc.forecasts.drop_duplicates("StockCode").set_index("StockCode")["Class"].to_dict()
        if len(fc.forecasts)
        else {}
    )
    layers = synthetic.build(demand.skus, descriptions, classes)
    plan = inventory.run(fc, layers)
    wh = warehouse.run(stream, layers)
    ful = fulfil.simulate(stream, wh.workload, layers, demand.weeks)
    dl = deliver.run(cleaned, ful)
    cs = costing.run(cleaned, demand.skus, plan, ful, dl)
    return Baseline(
        source="fixture" if fixture else "full",
        expected_revenue_gbp=expected,
        cleaned=cleaned,
        demand=demand,
        stream=stream,
        fc=fc,
        layers=layers,
        plan=plan,
        wh=wh,
        ful=ful,
        dl=dl,
        cs=cs,
    )


# --------------------------------------------------------------------------- #
# Reconciliation harness (reused verbatim for baseline and perturbed runs)
# --------------------------------------------------------------------------- #
def build_ledger_and_checks(
    cleaned, demand, stream, fc, plan, wh, ful, dl, cs, expected_revenue_gbp
) -> tuple[reconcile.Ledger, list[CheckResult]]:
    """Register stages 0-5 and run all 13 identity checks -- the exact stage-6
    path from chain/__main__.py, on whichever (baseline or perturbed) objects."""
    led = reconcile.Ledger()
    reconcile.register_stage0(led, cleaned, demand, stream)
    reconcile.register_stage1(led, fc)
    reconcile.register_stage2(led, plan)
    reconcile.register_stage3(led, wh)
    reconcile.register_stage4(led, stream, ful, dl)
    reconcile.register_stage5(led, cs)
    checks = reconcile.run_phase1_checks(led, fc, demand, expected_revenue_gbp)
    checks += reconcile.run_phase2_checks(led, plan, fc, wh)
    checks += reconcile.run_phase3_checks(led, cleaned, demand, ful)
    return led, checks


# --------------------------------------------------------------------------- #
# The perturbation (pure, deterministic, non-mutating)
# --------------------------------------------------------------------------- #
def most_populous_class(fc: DemandForecast) -> str:
    """The forecast class carrying the most SKUs (deterministic tie-break by name)."""
    counts = fc.forecasts.drop_duplicates("StockCode")["Class"].value_counts()
    if counts.empty:
        raise ValueError("no forecast classes to surge")
    ordered = sorted(counts.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
    return str(ordered[0][0])


def apply_demand_surge(fc: DemandForecast, target_class: str, factor: float) -> DemandForecast:
    """A x``factor`` multiplicative demand shock on ``target_class``.

    Scales BOTH the point forecast (Units) and its measured uncertainty (Sigma)
    -- a larger-scale demand process -- for that class only. Returns a NEW
    DemandForecast; the input frame is not mutated. SKUs, models and classes are
    unchanged, so forecast coverage (identity (d)) is untouched.
    """
    frame = fc.forecasts.copy()
    mask = frame["Class"] == target_class
    frame.loc[mask, "Units"] = frame.loc[mask, "Units"] * factor
    frame.loc[mask, "Sigma"] = frame.loc[mask, "Sigma"] * factor
    return DemandForecast(
        forecasts=frame,
        cv=fc.cv,
        class_winners=fc.class_winners,
        horizon_weeks=fc.horizon_weeks,
        provenance=fc.provenance,
    )


# --------------------------------------------------------------------------- #
# Scenario result model
# --------------------------------------------------------------------------- #
@dataclass
class StageDelta:
    """One traced quantity: what it was, what the shock made it, at which stage."""

    stage: str
    metric: str
    unit: str
    provenance: Provenance
    baseline: float
    perturbed: float

    @property
    def delta(self) -> float:
        return self.perturbed - self.baseline

    @property
    def pct(self) -> float | None:
        return (self.delta / self.baseline * 100.0) if self.baseline else None


@dataclass
class ScenarioResult:
    """One what-if: the ONE input perturbed, the traced deltas, both identity sets."""

    name: str
    kind: str
    perturbed_input: str
    input_provenance: Provenance
    factor: float
    deltas: list[StageDelta]
    baseline_checks: list[CheckResult] = field(default_factory=list)
    perturbed_checks: list[CheckResult] = field(default_factory=list)

    @property
    def identities_hold(self) -> bool:
        return bool(self.perturbed_checks) and all(c.passed for c in self.perturbed_checks)

    @property
    def identities_passed(self) -> int:
        return sum(1 for c in self.perturbed_checks if c.passed)


# --------------------------------------------------------------------------- #
# Metric readouts
# --------------------------------------------------------------------------- #
def _fc_units_class(fc: DemandForecast, cls: str) -> float:
    frame = fc.forecasts
    return float(frame.loc[frame["Class"] == cls, "Units"].sum())


def _plan_total(plan, column: str) -> float:
    return float(plan.plan[column].sum()) if len(plan.plan) else 0.0


def _cost(cs, item: str) -> float:
    return float(cs.line(item).gbp)


# --------------------------------------------------------------------------- #
# Scenario 1 -- demand surge (forecast -> inventory -> holding -> ledger)
# --------------------------------------------------------------------------- #
def run_demand_surge(
    base: Baseline, target_class: str | None = None, factor: float = SURGE_FACTOR
) -> ScenarioResult:
    target_class = target_class or most_populous_class(base.fc)
    fc2 = apply_demand_surge(base.fc, target_class, factor)
    # Only the forecast-fed stages re-run; the physical stages are reused
    # unchanged (they stand on the immutable real invoice stream).
    plan2 = inventory.run(fc2, base.layers)
    cs2 = costing.run(base.cleaned, base.demand.skus, plan2, base.ful, base.dl)

    _, baseline_checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, base.fc, base.plan,
        base.wh, base.ful, base.dl, base.cs, base.expected_revenue_gbp,
    )
    _, perturbed_checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, fc2, plan2,
        base.wh, base.ful, base.dl, cs2, base.expected_revenue_gbp,
    )

    synth = Provenance.SYNTHETIC_ASSIGNED
    deltas = [
        StageDelta("1 forecast", f"forecast units, {target_class} class", "units",
                   Provenance.DERIVED, _fc_units_class(base.fc, target_class),
                   _fc_units_class(fc2, target_class)),
        StageDelta("2 inventory", "safety stock (total)", "units", synth,
                   _plan_total(base.plan, "SafetyStock"), _plan_total(plan2, "SafetyStock")),
        StageDelta("2 inventory", "order-up-to (total)", "units", synth,
                   _plan_total(base.plan, "OrderUpTo"), _plan_total(plan2, "OrderUpTo")),
        StageDelta("5 costing", "holding cost", "GBP", synth,
                   _cost(base.cs, "holding"), _cost(cs2, "holding")),
        StageDelta("5 costing", "transport cost", "GBP", synth,
                   _cost(base.cs, "transport"), _cost(cs2, "transport")),
        StageDelta("5 costing", "labour cost", "GBP", synth,
                   _cost(base.cs, "labour"), _cost(cs2, "labour")),
        StageDelta("5 costing", "total cost", "GBP", synth,
                   _cost(base.cs, "total cost"), _cost(cs2, "total cost")),
        StageDelta("6 ledger", "window revenue", "GBP", Provenance.REAL,
                   _cost(base.cs, "window revenue"), _cost(cs2, "window revenue")),
    ]
    return ScenarioResult(
        name=f"demand-surge x{factor:.2f} on the {target_class} class",
        kind="demand",
        perturbed_input=(
            f"forecast demand + uncertainty for the {target_class} class "
            f"(x{factor:.2f}, multiplicative)"
        ),
        input_provenance=Provenance.DERIVED,
        factor=factor,
        deltas=deltas,
        baseline_checks=baseline_checks,
        perturbed_checks=perturbed_checks,
    )


# --------------------------------------------------------------------------- #
# Scenario 2 -- cost-rate shock (rate -> costing -> ledger)
# --------------------------------------------------------------------------- #
def run_cost_rate_shock(
    base: Baseline, rate_attr: str = "TRANSPORT_RATE_GBP_PER_KM", factor: float = COST_SHOCK_FACTOR
) -> ScenarioResult:
    original = getattr(costing, rate_attr)
    try:
        # Reuse the costing engine verbatim under the perturbed labelled rate;
        # the module constant is restored in `finally` (nothing on disk changes).
        setattr(costing, rate_attr, original * factor)
        cs2 = costing.run(base.cleaned, base.demand.skus, base.plan, base.ful, base.dl)
    finally:
        setattr(costing, rate_attr, original)

    _, baseline_checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, base.fc, base.plan,
        base.wh, base.ful, base.dl, base.cs, base.expected_revenue_gbp,
    )
    _, perturbed_checks = build_ledger_and_checks(
        base.cleaned, base.demand, base.stream, base.fc, base.plan,
        base.wh, base.ful, base.dl, cs2, base.expected_revenue_gbp,
    )

    synth = Provenance.SYNTHETIC_ASSIGNED
    deltas = [
        StageDelta("4b transport", "CVRP route km", "km", synth,
                   float(base.dl.total_cvrp_km), float(base.dl.total_cvrp_km)),
        StageDelta("5 costing", "transport cost", "GBP", synth,
                   _cost(base.cs, "transport"), _cost(cs2, "transport")),
        StageDelta("5 costing", "holding cost", "GBP", synth,
                   _cost(base.cs, "holding"), _cost(cs2, "holding")),
        StageDelta("5 costing", "total cost", "GBP", synth,
                   _cost(base.cs, "total cost"), _cost(cs2, "total cost")),
        StageDelta("6 ledger", "window revenue", "GBP", Provenance.REAL,
                   _cost(base.cs, "window revenue"), _cost(cs2, "window revenue")),
    ]
    return ScenarioResult(
        name=f"cost-rate shock x{factor:.2f} on transport GBP/km",
        kind="cost-rate",
        perturbed_input=f"transport cost rate ({original:.2f} -> {original * factor:.2f} GBP/km)",
        input_provenance=Provenance.SYNTHETIC_ASSIGNED,
        factor=factor,
        deltas=deltas,
        baseline_checks=baseline_checks,
        perturbed_checks=perturbed_checks,
    )


def run_all(base: Baseline) -> list[ScenarioResult]:
    return [run_demand_surge(base), run_cost_rate_shock(base)]


# --------------------------------------------------------------------------- #
# Formatting + emit (deterministic)
# --------------------------------------------------------------------------- #
def _fmt(value: float, unit: str) -> str:
    if unit == "km":
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _fmt_pct(delta: StageDelta) -> str:
    return "n/a" if delta.pct is None else f"{delta.pct:+.2f}%"


def print_report(results: list[ScenarioResult], source: str) -> bool:
    print("=" * 72)
    print("decision-chain -- scenario / shock what-if (baseline vs perturbed)")
    print("=" * 72)
    print(f"source: {source}")
    print(
        "each scenario perturbs ONE input, re-runs the stages it feeds, and re-checks"
    )
    print("all 13 cross-stage reconciliation identities on the perturbed run.")
    print()
    all_hold = True
    for res in results:
        all_hold &= res.identities_hold
        print("-" * 72)
        print(f"SCENARIO: {res.name}")
        print(f"  perturbed input : {res.perturbed_input}  {res.input_provenance.tag()}")
        print(f"  {'stage':<14} {'metric':<28} {'baseline':>14} {'perturbed':>14} {'delta':>14} {'pct':>9}")
        for d in res.deltas:
            print(
                f"  {d.stage:<14} {d.metric:<28} {_fmt(d.baseline, d.unit):>14} "
                f"{_fmt(d.perturbed, d.unit):>14} {_fmt(d.delta, d.unit):>14} {_fmt_pct(d):>9}  "
                f"{d.unit} {d.provenance.tag()}"
            )
        verdict = (
            f"ALL {len(res.perturbed_checks)} IDENTITIES HOLD"
            if res.identities_hold
            else "IDENTITY FAILURE UNDER PERTURBATION"
        )
        print(
            f"  reconciliation  : {res.identities_passed}/{len(res.perturbed_checks)} "
            f"identities PASS under the perturbed run -- {verdict}"
        )
        print()
    print("-" * 72)
    print(
        "result: "
        + (
            "every reconciliation identity holds under every shock"
            if all_hold
            else "AT LEAST ONE IDENTITY FAILED UNDER A SHOCK"
        )
    )
    print("-" * 72)
    return all_hold


def _rows(results: list[ScenarioResult]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for res in results:
        for d in res.deltas:
            rows.append(
                {
                    "scenario": res.name,
                    "kind": res.kind,
                    "perturbed_input": res.perturbed_input,
                    "input_provenance": res.input_provenance.value,
                    "factor": f"{res.factor:.2f}",
                    "stage": d.stage,
                    "metric": d.metric,
                    "unit": d.unit,
                    "provenance": d.provenance.value,
                    "baseline": f"{d.baseline:.4f}",
                    "perturbed": f"{d.perturbed:.4f}",
                    "delta": f"{d.delta:.4f}",
                    "delta_pct": "n/a" if d.pct is None else f"{d.pct:.4f}",
                    "identities_passed": str(res.identities_passed),
                    "identities_total": str(len(res.perturbed_checks)),
                    "identities_hold": "yes" if res.identities_hold else "no",
                }
            )
    return rows


def write_csv(results: list[ScenarioResult], path: Path = SCENARIO_CSV) -> Path:
    """Deterministic CSV (LF newlines, fixed field order)."""
    rows = _rows(results)
    fields = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_markdown(results: list[ScenarioResult], source: str, path: Path = SCENARIO_MD) -> Path:
    """Deterministic Markdown summary the README references."""
    lines: list[str] = []
    lines.append("# Scenario / shock what-if -- the ledger still reconciles")
    lines.append("")
    lines.append(
        "Each scenario perturbs **one** input, re-runs only the stages that input "
        "feeds (reusing the existing stage engines, never re-inventing one), and "
        "re-runs **all 13** cross-stage reconciliation identities on the perturbed "
        "run. Real data is immutable; the synthetic layers keep seed 42; every "
        "number is a function of the deterministic chain, so this file regenerates "
        "byte-for-byte."
    )
    lines.append("")
    lines.append(f"Source: **{source}**. Regenerate with `python scenario.py`.")
    lines.append("")
    for res in results:
        lines.append(f"## {res.name}")
        lines.append("")
        lines.append(
            f"- Perturbed input: {res.perturbed_input} "
            f"(`{res.input_provenance.value}`)"
        )
        lines.append(
            f"- Reconciliation: **{res.identities_passed}/{len(res.perturbed_checks)} "
            f"identities PASS** under the perturbed run "
            + ("(all hold)" if res.identities_hold else "(**FAILURE**)")
        )
        lines.append("")
        lines.append("| stage | metric | baseline | perturbed | delta | pct | unit | provenance |")
        lines.append("|---|---|---:|---:|---:|---:|---|---|")
        for d in res.deltas:
            lines.append(
                f"| {d.stage} | {d.metric} | {_fmt(d.baseline, d.unit)} | "
                f"{_fmt(d.perturbed, d.unit)} | {_fmt(d.delta, d.unit)} | {_fmt_pct(d)} | "
                f"{d.unit} | `{d.provenance.value}` |"
            )
        lines.append("")
    lines.append("### Honest reading")
    lines.append("")
    lines.append(
        "- The **demand surge** is a planning-side shock: it perturbs the *derived* "
        "forecast, so the safety-stock buffer and its holding charge rise for the "
        "surged class, but the physical fulfilment of the already-real orders "
        "(picks, cartons, CVRP km, labour hours) is unchanged -- those cost lines "
        "and every physical identity (i, j, k) do not move. The reconciliation "
        "proves the shock lands only where it should."
    )
    lines.append(
        "- The **cost-rate shock** perturbs a labelled *synthetic-assigned* rate: "
        "the km are unchanged (the rate rose, not the distance), the dominant "
        "transport line and the total rise together, and additivity (l) plus the "
        "real window revenue (m) still close to the cent."
    )
    lines.append(
        "- No overclaim: these are cost-structure what-ifs under labelled synthetic "
        "rates and lead times, not profit statements; the real vs synthetic-assigned "
        "boundary is exactly the baseline's."
    )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="scenario", description=__doc__)
    parser.add_argument(
        "--full", action="store_true",
        help="run on the full UCI dataset instead of the fixture (slow: builds the CVRP baseline)",
    )
    parser.add_argument("--no-emit", action="store_true", help="print only; do not write deliverables")
    args = parser.parse_args(argv)

    base = build_baseline(fixture=not args.full)
    source = (
        "committed real-row fixture (deterministic CI path)"
        if base.source == "fixture"
        else "full UCI Online Retail II dataset"
    )
    results = run_all(base)
    all_hold = print_report(results, source)
    if not args.no_emit:
        csv_path = write_csv(results)
        md_path = write_markdown(results, source)
        print(f"emitted: {csv_path}")
        print(f"emitted: {md_path}")
    return 0 if all_hold else 1


if __name__ == "__main__":
    raise SystemExit(main())
