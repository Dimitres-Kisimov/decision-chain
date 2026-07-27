"""Entry point: run stages 0-5 + reconciliation and print the honest P1-P3 report.

    python -m chain --report              # full real dataset (needs raw data on disk)
    python -m chain --report --fixture    # committed ~2k-row real fixture (CI path)

Output is ASCII-only and UTF-8-safe on the Windows console. Every reported
number carries its provenance tag; every identity check prints both sides.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chain import (
    artifact as artifact_mod,
)
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
from chain.contracts import Provenance


def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _rule(char: str = "-") -> None:
    print(char * 72)


def run_report(
    fixture: bool = False,
    n_skus: int | None = None,
    artifact_path: Path | None = None,
) -> bool:
    tag_real = Provenance.REAL.tag()
    tag_derived = Provenance.DERIVED.tag()
    tag_synth = Provenance.SYNTHETIC_ASSIGNED.tag()

    if fixture:
        raw = ingest.load_fixture()
        expected_revenue = json.loads(paths.FIXTURE_EXPECTED.read_text(encoding="utf-8"))[
            "cleaned_revenue_gbp"
        ]
        n_skus = n_skus or 25
        source = f"committed fixture ({len(raw):,} real rows)"
    else:
        raw = ingest.load_raw()
        expected_revenue = reconcile.PUBLISHED_REVENUE_GBP
        n_skus = n_skus or ingest.N_TOP_SKUS
        source = "full UCI Online Retail II dataset"

    _rule("=")
    print("decision-chain -- Phase 1-3 report (stages 0-5 + reconciliation)")
    _rule("=")
    print(f"source: {source}")
    print()

    # stage 0
    cleaned, demand, stream = ingest.run(raw, n_skus=n_skus)
    total_revenue = cleaned.revenue_gbp
    tracked_lines = ingest.sales_in_week_window(cleaned, demand.skus, demand.weeks)
    tracked_revenue = float(tracked_lines["Revenue"].sum())
    coverage = tracked_revenue / total_revenue if total_revenue else 0.0

    print(f"stage 0 -- ingest {tag_real}")
    print(f"  raw rows                  {len(raw):>12,}")
    print(f"  cleaned sales rows        {len(cleaned.sales):>12,}")
    print(f"  returns rows              {len(cleaned.returns):>12,}")
    print(f"  cleaned revenue (GBP)     {total_revenue:>15,.2f}")
    print(f"  weekly index              {len(demand.weeks)} weeks "
          f"({demand.weeks[0].date()} .. {demand.weeks[-1].date()})")
    print(f"  tracked SKUs (top by rev) {len(demand.skus):>12,}")
    print(f"  tracked revenue share     {100 * coverage:>11.1f}%")
    print(f"  invoice-stream lines      {stream.n_lines:>12,} "
          f"across {stream.n_invoices:,} invoices")
    print("  cleaning log:")
    for step in cleaned.steps:
        print(
            f"    {step['step']:<34} {step['rows_in']:>9,} -> {step['rows_out']:>9,} "
            f"(-{step['pct_removed'] * 100:.2f}%)"
        )
    print()

    # stage 1
    fc = forecast_stage.run(demand)
    print(f"stage 1 -- forecast {tag_derived}")
    eligible = fc.cv["StockCode"].nunique() if len(fc.cv) else 0
    print(f"  SKUs forecast             {eligible:>12,} of {len(demand.skus):,} tracked "
          f"(rest below {forecast_stage.MIN_NONZERO} nonzero weeks or too short)")
    print(f"  CV design                 rolling-origin, {forecast_stage.N_FOLDS} folds, "
          f"horizon {fc.horizon_weeks}w, MASE scale m=1")
    if len(fc.class_winners):
        print("  per-class results (mean MASE over SKU x fold cells; lower is better):")
        for cls, group in fc.class_winners.groupby("class", observed=True):
            n_sku_class = fc.cv.loc[fc.cv["class"] == cls, "StockCode"].nunique()
            print(f"    class {cls!s:<13} ({n_sku_class} SKUs)")
            for _, row in group.sort_values("mean_mase").iterrows():
                marker = "  <-- winner" if row["winner"] else ""
                print(
                    f"      {row['model']:<16} mean MASE {row['mean_mase']:>7.3f} "
                    f"({row['cells']} cells){marker}"
                )
    print()

    # synthetic layers (phase 2 inputs -- invented, seeded, labelled)
    descriptions = synthetic.modal_descriptions(cleaned.sales, demand.skus)
    demand_classes = (
        fc.forecasts.drop_duplicates("StockCode").set_index("StockCode")["Class"].to_dict()
        if len(fc.forecasts)
        else {}
    )
    layers = synthetic.build(demand.skus, descriptions, demand_classes)
    print(f"synthetic layers {tag_synth} (seed {layers.seed} -- invented, labelled, never data)")
    size_counts = layers.sku_physical["SizeClass"].value_counts()
    print("  SKU dims/weights          "
          + ", ".join(f"{cls}: {int(size_counts.get(cls, 0))}" for cls in ("small", "medium", "large"))
          + "  (description-keyword rule)")
    lead_span = (int(layers.lead_times["LeadTimeWeeks"].min()),
                 int(layers.lead_times["LeadTimeWeeks"].max()))
    print(f"  supplier lead times       {lead_span[0]}-{lead_span[1]} weeks by demand class + jitter")
    geo = layers.geometry
    print(f"  warehouse geometry        {geo.n_slots} slots "
          f"({geo.assumptions['aisles']} aisles x {geo.assumptions['bays_per_aisle']} bays), "
          f"capacity >= {len(demand.skus)} tracked SKUs")
    print()

    # stage 2
    plan = inventory.run(fc, layers)
    print(f"stage 2 -- inventory {plan.provenance.tag()} "
          f"(service level {plan.assumptions['service_level']:.0%}, weekly review, "
          "base-stock + sqrt-law)")
    print(f"  SKUs planned              {plan.plan['StockCode'].nunique():>12,} "
          f"(= every forecasted SKU; identity (e))")
    print(f"  total order-up-to         {plan.plan['OrderUpTo'].sum():>15,.0f} units")
    print(f"  total safety stock        {plan.plan['SafetyStock'].sum():>15,.0f} units")
    summary = inventory.class_summary(plan)
    print("  per class (safety-stock weeks = SS / weekly demand -- the buffer the")
    print("  forecast quality costs):")
    for _, row in summary.sort_values("SafetyStockWeeks").iterrows():
        print(
            f"    {row['Class']:<13} {int(row['SKUs']):>4} SKUs  lead {row['MeanLeadWeeks']:.1f}w  "
            f"sigma/mu {row['SigmaOverMu']:>8.2f}  SS weeks {row['SafetyStockWeeks']:>7.2f}"
        )
    lumpy = summary.loc[summary["Class"] == "lumpy"]
    smooth = summary.loc[summary["Class"] == "smooth"]
    if len(lumpy) and len(smooth):
        ratio = float(lumpy["SafetyStockWeeks"].iloc[0]) / max(
            float(smooth["SafetyStockWeeks"].iloc[0]), 1e-9
        )
        print(
            "  HONEST NOTE: lumpy demand is unforecastable (stage 1: naive wins, MASE > 1),"
        )
        print(
            f"  so its measured sigma buys {float(lumpy['SafetyStockWeeks'].iloc[0]):.2f} weeks of "
            f"buffer vs {float(smooth['SafetyStockWeeks'].iloc[0]):.2f} for smooth "
            f"({ratio:.1f}x) -- the cost of unforecastability, not a modelling win."
        )
    elif len(lumpy):
        print(
            "  HONEST NOTE: lumpy demand is unforecastable (stage 1: naive wins, MASE > 1); "
            f"its buffer is {float(lumpy['SafetyStockWeeks'].iloc[0]):.2f} weeks of demand."
        )
    if len(summary):
        widest = summary.loc[summary["SafetyStockWeeks"].idxmax()]
        print(
            f"  measured widest buffer per unit of demand: class {widest['Class']} at "
            f"{float(widest['SafetyStockWeeks']):.2f} SS weeks (reported as measured)."
        )
    print()

    # stage 3
    wh = warehouse.run(stream, layers)
    print(f"stage 3 -- warehouse {wh.workload.provenance.tag()} "
          "(real invoices picked in the synthetic geometry)")
    print(f"  velocity input            invoice lines per SKU {tag_real}")
    print("  three slottings, identical real invoice set (identity (g)):")
    comparison = wh.comparison.summary()
    for _, row in comparison.iterrows():
        delta = f"{row['delta_vs_random_pct']:+6.1f}% vs random" if row["variant"] != "random" else "baseline"
        print(
            f"    {row['variant']:<8} mean travel {row['mean_travel_m']:>7.1f} m/invoice  "
            f"({int(row['invoices']):,} invoices, {int(row['lines']):,} lines)  {delta}"
        )
    abc_mean = wh.comparison.mean_travel("abc")
    opt_mean = wh.comparison.mean_travel("optimal")
    if abs(opt_mean - abc_mean) <= 0.005 * abc_mean:
        print(
            "  HONEST NOTE: assignment-optimal ~= ABC here -- with one distance per slot"
        )
        print(
            "  the LAP optimum IS velocity-sorted placement (rearrangement inequality);"
        )
        print("  the exact solver only re-breaks velocity ties. Reported as measured.")
    print()

    # stage 4a -- fulfilment DES (representative window)
    ful = fulfil.simulate(stream, wh.workload, layers, demand.weeks)
    window = ful.window_weeks
    print(f"stage 4a -- fulfilment DES {ful.provenance.tag()} "
          f"({ful.assumptions['n_pickers']} synthetic pickers, deployed optimal slotting)")
    print(f"  representative window     {len(window)} of {len(demand.weeks)} weeks "
          f"({window[0].date()} .. {window[-1].date()})")
    print("  window rule               contiguous span with the most invoice lines")
    print("  (simulating all weeks through the CVRP stage is slow; stages 4-5 run")
    print("  on this SAME window and every window identity says so explicitly)")
    n_days = len(ful.per_day)
    if n_days:
        print(f"  orders shipped            {len(ful.orders):>12,} "
              f"across {n_days} working days")
        print(f"  lines picked              {int(ful.orders['Lines'].sum()):>12,}")
        print(f"  mean orders/day           {len(ful.orders) / n_days:>12.1f}")
        n_pickers = int(ful.assumptions["n_pickers"])
        util = ful.labour_hours / (n_days * n_pickers * 8.0)
        print(f"  labour hours (picking)    {ful.labour_hours:>12.1f} "
              f"(~{ful.labour_hours / n_days:.1f} h/day; {100 * util:.0f}% of a "
              f"{n_pickers}-picker 8h-day crew -- utilisation reported, not hidden)")
        print(f"  mean order wait           {float(ful.orders['WaitMin'].mean()):>12.1f} min "
              f"(p95 {float(ful.orders['WaitMin'].quantile(0.95)):.1f})")
        print(f"  cartons shipped           {ful.cartons_shipped:>12,} "
              f"(FFD, {int(ful.assumptions['carton_cm'][0])}x"
              f"{int(ful.assumptions['carton_cm'][1])}x"
              f"{int(ful.assumptions['carton_cm'][2])}cm, "
              f"{ful.assumptions['carton_max_kg']:.0f}kg cap)")
    print()

    # stage 4b -- transport CVRP per delivery day
    dl = deliver.run(cleaned, ful)
    print(f"stage 4b -- transport {dl.plan.provenance.tag()} "
          "(seeded per-customer coordinates -- INVENTED geography)")
    n_uk = int((dl.plan.shipments["Country"] == "United Kingdom").sum())
    n_exp = len(dl.plan.shipments) - n_uk
    print(f"  drops routed              {len(dl.plan.shipments):>12,} "
          f"({n_uk:,} UK band, {n_exp:,} export band)")
    print(f"  delivery days             {len(dl.per_day):>12,}")
    print(f"  vehicle capacity          {dl.plan.assumptions['vehicle_capacity_cartons']:>12,} cartons; "
          f"solution limit {dl.plan.assumptions['solution_limit']} (deterministic, not wall-clock)")
    cvrp_km, cw_km = dl.total_cvrp_km, dl.total_cw_km
    delta_pct = 100.0 * (cvrp_km - cw_km) / cw_km if cw_km else 0.0
    print(f"  Clarke-Wright baseline    {cw_km:>12,.1f} km  "
          f"({int(dl.per_day['CwVehicles'].sum()):,} vehicle-days)")
    print(f"  OR-Tools CVRP             {cvrp_km:>12,.1f} km  "
          f"({int(dl.per_day['CvrpVehicles'].sum()):,} vehicle-days)  "
          f"{delta_pct:+.1f}% vs CW")
    wins = int((dl.per_day["CvrpKm"] < dl.per_day["CwKm"] - 1e-6).sum())
    ties = int((abs(dl.per_day["CvrpKm"] - dl.per_day["CwKm"]) <= 1e-6).sum())
    losses = len(dl.per_day) - wins - ties
    print(f"  per-day record            CVRP wins {wins}, ties {ties}, loses {losses} "
          f"of {len(dl.per_day)} days (measured, not assumed)")
    if cvrp_km <= cw_km:
        print("  HONEST NOTE: the CVRP gain over Clarke-Wright is what it is above --")
        print("  on this synthetic geography many small same-band drops leave the 1964")
        print("  construction little to lose; the delta is measured, not sold as savings.")
    else:
        print("  HONEST NOTE: within this deterministic solution budget the metaheuristic")
        print("  did NOT beat the 1964 Clarke-Wright construction overall -- reported")
        print("  as measured; the ledger costs the CVRP plan the chain actually emits.")
    print()

    # stage 5 -- cost-to-serve ledger
    cs = costing.run(cleaned, demand.skus, plan, ful, dl)
    print(f"stage 5 -- cost-to-serve {tag_synth} "
          f"(window of {len(window)} weeks; NO profit claims -- every rate is INVENTED)")
    print("  input split: REAL order composition/timestamps/revenue; every rate,")
    print("  hour, km and safety-stock unit is synthetic-assigned (labelled).")
    print(f"  {'item':<22} {'GBP':>14}      {'provenance':<21} basis")
    for line in cs.lines:
        print(f"  {line.formatted()}")
    ratio = cs.total_cost_gbp / cs.window_revenue_gbp if cs.window_revenue_gbp else 0.0
    print(f"  modelled cost equals {100 * ratio:.1f}% of window revenue -- a cost-structure")
    print("  view under labelled assumptions, NOT a margin statement.")
    print()

    # stage 6
    print(f"stage 6 -- reconciliation {tag_real}/{tag_derived}/{tag_synth}")
    ledger = reconcile.Ledger()
    reconcile.register_stage0(ledger, cleaned, demand, stream)
    reconcile.register_stage1(ledger, fc)
    reconcile.register_stage2(ledger, plan)
    reconcile.register_stage3(ledger, wh)
    reconcile.register_stage4(ledger, stream, ful, dl)
    reconcile.register_stage5(ledger, cs)
    checks = reconcile.run_phase1_checks(ledger, fc, demand, expected_revenue)
    checks += reconcile.run_phase2_checks(ledger, plan, fc, wh)
    checks += reconcile.run_phase3_checks(ledger, cleaned, demand, ful)
    all_passed = reconcile.print_checks(checks)
    print()
    _rule()
    verdict = "ALL IDENTITY CHECKS PASSED" if all_passed else "IDENTITY CHECK FAILURE"
    print(f"result: {verdict} ({sum(c.passed for c in checks)}/{len(checks)})")
    _rule()

    if artifact_path is not None:
        bundle = artifact_mod.build(
            source="fixture" if fixture else "full",
            raw_rows=len(raw),
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
            ledger=ledger,
            checks=checks,
        )
        saved = artifact_mod.save(bundle, artifact_path)
        print(f"artifact saved: {saved} ({saved.stat().st_size:,} bytes, "
              f"schema {artifact_mod.SCHEMA_VERSION})")
    return all_passed


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="chain", description=__doc__)
    parser.add_argument("--report", action="store_true", help="run stages 0-5 + reconciliation")
    parser.add_argument("--fixture", action="store_true", help="use the committed fixture (CI)")
    parser.add_argument("--skus", type=int, default=None, help="override tracked-SKU count")
    parser.add_argument(
        "--save-artifact",
        action="store_true",
        help="serialize the run to a deterministic JSON artifact after the report",
    )
    parser.add_argument(
        "--artifact-path",
        type=Path,
        default=None,
        help=f"artifact location (default {paths.ARTIFACT_JSON})",
    )
    parser.add_argument(
        "--deliverables",
        action="store_true",
        help="build the CHAIN REPORT PDF + Excel LEDGER from the saved artifact (no recompute)",
    )
    args = parser.parse_args(argv)

    if args.deliverables:
        from chain import exports

        return exports.main(artifact_path=args.artifact_path or paths.ARTIFACT_JSON)

    if not args.report:
        parser.print_help()
        return 0
    artifact_path = None
    if args.save_artifact or args.artifact_path is not None:
        artifact_path = args.artifact_path or paths.ARTIFACT_JSON
    ok = run_report(fixture=args.fixture, n_skus=args.skus, artifact_path=artifact_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
