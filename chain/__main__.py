"""Entry point: run stages 0-1 + reconciliation and print the honest P1 report.

    python -m chain --report              # full real dataset (needs raw data on disk)
    python -m chain --report --fixture    # committed ~2k-row real fixture (CI path)

Output is ASCII-only and UTF-8-safe on the Windows console. Every reported
number carries its provenance tag; every identity check prints both sides.
"""

from __future__ import annotations

import argparse
import json
import sys

from chain import forecast as forecast_stage
from chain import ingest, paths, reconcile
from chain.contracts import Provenance


def _utf8_console() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _rule(char: str = "-") -> None:
    print(char * 72)


def run_report(fixture: bool = False, n_skus: int | None = None) -> bool:
    tag_real = Provenance.REAL.tag()
    tag_derived = Provenance.DERIVED.tag()

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
    print("decision-chain -- Phase 1 report (stages 0-1 + reconciliation)")
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

    # stage 6
    print(f"stage 6 -- reconciliation {tag_real}/{tag_derived}")
    ledger = reconcile.Ledger()
    reconcile.register_stage0(ledger, cleaned, demand, stream)
    reconcile.register_stage1(ledger, fc)
    checks = reconcile.run_phase1_checks(ledger, fc, demand, expected_revenue)
    all_passed = reconcile.print_checks(checks)
    print()
    _rule()
    verdict = "ALL IDENTITY CHECKS PASSED" if all_passed else "IDENTITY CHECK FAILURE"
    print(f"result: {verdict} ({sum(c.passed for c in checks)}/{len(checks)})")
    _rule()
    return all_passed


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="chain", description=__doc__)
    parser.add_argument("--report", action="store_true", help="run stages 0-1 + reconciliation")
    parser.add_argument("--fixture", action="store_true", help="use the committed fixture (CI)")
    parser.add_argument("--skus", type=int, default=None, help="override tracked-SKU count")
    args = parser.parse_args(argv)
    if not args.report:
        parser.print_help()
        return 0
    ok = run_report(fixture=args.fixture, n_skus=args.skus)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
