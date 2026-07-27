"""Executive deliverables from the run artifact (phase 4).

``python -m chain --deliverables`` reads ``artifacts/full_run.json`` (the
committed receipt of one measured run — chain/artifact.py) and builds:

* ``deliverables/chain_report.pdf``   — the CHAIN REPORT: cover with the
  one-dataset-through-everything story and the boundary statement; the
  pipeline diagram with provenance colors; the identities table; the slotting
  and routing comparisons; the cost-to-serve ledger + reconciliation ledger.
* ``deliverables/chain_ledger.xlsx``  — the LEDGER workbook: Stages,
  Identities, CostToServe, SlottingComparison, Assumptions sheets.

NOTHING is recomputed — the deliverables quote the artifact, so they always
match the dashboard and the committed run. Regenerating from the same
artifact yields byte-identical files (fixed metadata timestamps, no
wall-clock anywhere). If the code is newer than the artifact, a staleness
warning is printed and stamped on the PDF cover.

Provenance colors match the dashboard: real=green, derived=blue,
synthetic-assigned=amber.
"""

from __future__ import annotations

import datetime as _dt
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyArrow, FancyBboxPatch

from chain import artifact as artifact_mod
from chain import paths

# Provenance -> color (the same code the dashboard uses).
PROV_COLOR = {
    "real": "#1d9e6f",
    "derived": "#2f6bff",
    "synthetic-assigned": "#e8a33d",
}
INK = "#1a2233"
MUTED = "#6b7488"
RED = "#d64550"

# Fixed metadata: regenerating from the same artifact must be byte-identical.
_FIXED_TS = _dt.datetime(2000, 1, 1, 0, 0, 0)
_PDF_METADATA = {
    "Title": "CHAIN REPORT - decision-chain run artifact",
    "Author": "decision-chain",
    "CreationDate": _FIXED_TS,
    "ModDate": _FIXED_TS,
}


def _prov_color(tag: str) -> str:
    return PROV_COLOR.get(str(tag), PROV_COLOR["synthetic-assigned"])


def _new_page(pdf: PdfPages, title: str) -> plt.Axes:
    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    if title:
        ax.text(0.05, 0.945, title, fontsize=17, fontweight="bold", color=INK)
        ax.plot([0.05, 0.95], [0.925, 0.925], color=MUTED, lw=0.8)
    return ax


def _finish(pdf: PdfPages, ax: plt.Axes) -> None:
    pdf.savefig(ax.figure)
    plt.close(ax.figure)


# --------------------------------------------------------------------------- #
# PDF pages
# --------------------------------------------------------------------------- #
def _page_cover(pdf: PdfPages, art: dict[str, Any], stale: list[str]) -> None:
    ax = _new_page(pdf, "")
    ax.text(0.05, 0.86, "CHAIN REPORT", fontsize=30, fontweight="bold", color=INK)
    ax.text(0.05, 0.80, "decision-chain: one real dataset through the whole distributor decision chain",
            fontsize=13, color=INK)

    ingest = artifact_mod.stage_by_name(art, "ingest")["detail"]
    ful = artifact_mod.stage_by_name(art, "fulfilment (DES)")["detail"]
    story = (
        f"The UCI Online Retail II transactions ({ingest['raw_rows']:,} raw rows, "
        f"{ingest['weeks']} weeks, {ingest['tracked_skus']:,} tracked SKUs) flow through\n"
        "ingest -> forecast -> inventory -> warehouse -> fulfilment -> transport -> costing.\n"
        "A reconciliation ledger (stage 6) machine-checks identities at every seam: two numbers,\n"
        "computed by independent code paths, must agree - to the penny where pennies exist.\n\n"
        f"This report is generated from the saved run artifact ({art['source']} run, schema "
        f"{art['schema_version']}) - the committed receipt\nof one measured ~51-minute run "
        f"(48 CVRP instances); stages 4-5 run on the same {ful['window_weeks']}-week "
        f"representative window\n({ful['window_start']} .. {ful['window_end']}), stated on "
        "every window identity. Nothing here is recomputed."
    )
    ax.text(0.05, 0.74, story, fontsize = 10.5, color=INK, va="top", linespacing=1.55)

    verdict = f"{art['identities_passed']}/{art['identities_total']} identity checks PASS"
    color = PROV_COLOR["real"] if art["all_passed"] else RED
    ax.add_patch(FancyBboxPatch((0.05, 0.44), 0.42, 0.055,
                                boxstyle="round,pad=0.008", fc=color, ec="none", alpha=0.15))
    ax.text(0.06, 0.468, verdict, fontsize=15, fontweight="bold", color=color, va="center")

    boundary = (
        "BOUNDARY STATEMENT - what is real vs assigned. Transactions, invoice composition,\n"
        "timestamps, demand and revenue are REAL (observed). Forecasts are DERIVED from real\n"
        "inputs. SKU dims, lead times, warehouse geometry, customer geography, crew, cartons,\n"
        "vehicles and every cost rate are SYNTHETIC-ASSIGNED: invented, seeded, labelled - and\n"
        "never presented as data. Every number in this report carries its provenance color:"
    )
    ax.text(0.05, 0.38, boundary, fontsize=10, color=INK, va="top", linespacing=1.5)
    for i, (tag, color) in enumerate(PROV_COLOR.items()):
        ax.add_patch(FancyBboxPatch((0.06 + i * 0.21, 0.175), 0.045, 0.02,
                                    boxstyle="round,pad=0.004", fc=color, ec="none"))
        ax.text(0.115 + i * 0.21, 0.185, tag, fontsize=10, color=INK, va="center")
    if stale:
        ax.text(0.05, 0.10,
                f"WARNING - STALE ARTIFACT: {len(stale)} chain/ source file(s) changed since "
                "this artifact was saved.\nRegenerate with: python -m chain --report --save-artifact",
                fontsize=10, color=RED, va="top")
    _finish(pdf, ax)


def _page_pipeline(pdf: PdfPages, art: dict[str, Any]) -> None:
    ax = _new_page(pdf, "The chain - stage flow with provenance")
    stages = art["stages"]
    n = len(stages)
    box_w, gap = 0.115, 0.0135
    x0 = 0.05
    y, box_h = 0.55, 0.22
    for i, stage in enumerate(stages):
        x = x0 + i * (box_w + gap)
        color = _prov_color(stage["provenance"])
        ax.add_patch(FancyBboxPatch((x, y), box_w, box_h, boxstyle="round,pad=0.004",
                                    fc="white", ec=color, lw=2.2))
        ax.add_patch(FancyBboxPatch((x, y + box_h - 0.012), box_w, 0.012,
                                    boxstyle="round,pad=0.001", fc=color, ec="none"))
        ax.text(x + box_w / 2, y + box_h - 0.045, f"{i} - {stage['name']}",
                fontsize=8.5, fontweight="bold", ha="center", color=INK)
        ax.text(x + box_w / 2, y + box_h / 2 - 0.008, f"{stage['headline']['value']:,.0f}",
                fontsize=12, fontweight="bold", ha="center", color=INK)
        ax.text(x + box_w / 2, y + 0.055, stage["headline"]["label"], fontsize=6.4,
                ha="center", color=MUTED, wrap=True)
        ax.text(x + box_w / 2, y + 0.022, f"[{stage['provenance']}]", fontsize=6.8,
                ha="center", color=color, fontweight="bold")
        if i < n - 1:
            ax.add_patch(FancyArrow(x + box_w + 0.001, y + box_h / 2, gap - 0.006, 0,
                                    width=0.004, head_width=0.016, head_length=0.005,
                                    fc=MUTED, ec="none"))
    ax.text(0.05, 0.44, "Every stage consumes the typed contract of the one before; the "
            "reconciliation ledger (below) checks identities at every seam.",
            fontsize=10, color=MUTED)

    # provenance legend
    for i, (tag, color) in enumerate(PROV_COLOR.items()):
        ax.add_patch(FancyBboxPatch((0.05 + i * 0.22, 0.36), 0.04, 0.018,
                                    boxstyle="round,pad=0.003", fc=color, ec="none"))
        ax.text(0.098 + i * 0.22, 0.369, tag, fontsize=9.5, color=INK, va="center")
    _finish(pdf, ax)


def _table_page(
    pdf: PdfPages,
    title: str,
    columns: list[str],
    rows: list[list[str]],
    col_widths: list[float],
    row_colors: list[str | None] | None = None,
    fontsize: float = 8.0,
    note: str = "",
) -> None:
    ax = _new_page(pdf, title)
    table = ax.table(cellText=rows, colLabels=columns, colWidths=col_widths,
                     cellLoc="left", loc="upper left",
                     bbox=(0.05, 0.06 if not note else 0.10, 0.90, 0.80))
    table.auto_set_font_size(False)
    table.set_fontsize(fontsize)
    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor("#e5e9f2")
        if r == 0:
            cell.set_facecolor("#f0f3fa")
            cell.get_text().set_fontweight("bold")
        elif row_colors and row_colors[r - 1]:
            cell.get_text().set_color(row_colors[r - 1])
    if note:
        ax.text(0.05, 0.055, note, fontsize=8.5, color=MUTED, va="top")
    _finish(pdf, ax)


def _page_identities(pdf: PdfPages, art: dict[str, Any]) -> None:
    rows, colors = [], []
    for check in art["identities"]:
        rows.append([
            check["name"],
            f"{check['lhs']:,.2f}",
            f"{check['rhs']:,.2f}",
            f"{check['tolerance']}",
            check["unit"],
            "PASS" if check["passed"] else "FAIL",
        ])
        colors.append(PROV_COLOR["real"] if check["passed"] else RED)
    _table_page(
        pdf,
        f"Reconciliation - the {art['identities_total']} cross-stage identities "
        f"({art['identities_passed']}/{art['identities_total']} PASS)",
        ["identity", "left side", "right side", "tolerance", "unit", "status"],
        rows,
        [0.24, 0.18, 0.18, 0.10, 0.10, 0.08],
        row_colors=colors,
        fontsize=8.5,
        note="Each identity compares two numbers computed by independent code paths; "
             "both sides are printed - no rounding tricks.",
    )


def _page_comparisons(pdf: PdfPages, art: dict[str, Any]) -> None:
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("Measured comparisons - slotting and routing", fontsize=15,
                 fontweight="bold", color=INK, x=0.05, ha="left", y=0.96)

    wh = artifact_mod.stage_by_name(art, "warehouse")["detail"]
    slotting = wh["slotting_comparison"]
    ax1 = fig.add_axes((0.07, 0.58, 0.40, 0.28))
    variants = [row["variant"] for row in slotting]
    travels = [row["mean_travel_m"] for row in slotting]
    bar_colors = [MUTED, PROV_COLOR["synthetic-assigned"], PROV_COLOR["real"]]
    ax1.barh(variants, travels, color=bar_colors[: len(variants)])
    for i, row in enumerate(slotting):
        label = f"{row['mean_travel_m']:,.1f} m"
        if row["variant"] != "random":
            label += f"  ({row['delta_vs_random_pct']:+.1f}% vs random)"
        ax1.text(row["mean_travel_m"], i, "  " + label, va="center", fontsize=8.5, color=INK)
    ax1.set_xlim(0, max(travels) * 1.45)
    ax1.invert_yaxis()
    ax1.set_title("Mean pick travel per invoice (identical real invoice set)",
                  fontsize=10, loc="left", color=INK)
    ax1.tick_params(labelsize=9)
    for spine in ("top", "right"):
        ax1.spines[spine].set_visible(False)

    tr = artifact_mod.stage_by_name(art, "transport (CVRP)")["detail"]
    per_day = tr["per_day"]
    ax2 = fig.add_axes((0.56, 0.58, 0.38, 0.28))
    xs = range(len(per_day))
    ax2.plot(xs, [row["CwKm"] for row in per_day], color=MUTED, ls="--", lw=1.4,
             label=f"Clarke-Wright  {tr['cw_km']:,.0f} km")
    ax2.plot(xs, [row["CvrpKm"] for row in per_day], color=PROV_COLOR["derived"], lw=1.7,
             label=f"OR-Tools CVRP  {tr['cvrp_km']:,.0f} km")
    ax2.set_title(
        f"Route km per delivery day ({tr['delivery_days']} days; CVRP "
        f"{tr['delta_vs_cw_pct']:+.1f}% vs CW)", fontsize=10, loc="left", color=INK)
    ax2.legend(fontsize=8.5, frameon=False)
    ax2.tick_params(labelsize=8.5)
    ax2.set_xlabel("delivery day", fontsize=9)
    ax2.set_ylabel("km", fontsize=9)
    for spine in ("top", "right"):
        ax2.spines[spine].set_visible(False)

    record = tr["record"]
    fig.text(0.07, 0.47,
             f"Per-day record: CVRP wins {record['wins']}, ties {record['ties']}, "
             f"loses {record['losses']} of {tr['delivery_days']} days - measured, not assumed. "
             f"Deterministic solution limit {tr['solution_limit']} (never wall-clock).",
             fontsize=9.5, color=INK)

    fc = artifact_mod.stage_by_name(art, "forecast")["detail"]
    winners = [row for row in fc["class_winners"] if row.get("winner")]
    lines = ["Forecast class winners (rolling-origin CV, mean MASE; lower is better):"]
    for row in winners:
        lines.append(f"   {row['class']:<14} ->  {row['model']}  (MASE {row['mean_mase']:.3f})")
    lines.append("When naive wins on real data, naive wins the report.")
    fig.text(0.07, 0.40, "\n".join(lines), fontsize=9.5, color=INK, va="top",
             family="monospace")

    ful = artifact_mod.stage_by_name(art, "fulfilment (DES)")["detail"]
    fig.text(0.07, 0.20,
             f"Fulfilment window: {ful['lines_picked']:,} lines picked across "
             f"{ful['working_days']} working days by {ful['n_pickers']} synthetic pickers; "
             f"{ful['cartons_shipped']:,} cartons (FFD packing); labour {ful['labour_hours']:,.1f} h; "
             f"mean order wait {ful['mean_wait_min']:.1f} min (p95 {ful['p95_wait_min']:.1f}).",
             fontsize=9.5, color=INK)

    pdf.savefig(fig)
    plt.close(fig)


def _page_ledger(pdf: PdfPages, art: dict[str, Any]) -> None:
    costing = artifact_mod.stage_by_name(art, "costing")["detail"]
    rows, colors = [], []
    for line in costing["lines"]:
        rows.append([line["item"], f"{line['gbp']:,.2f}", line["provenance"], line["basis"]])
        colors.append(_prov_color(line["provenance"]))
    _table_page(
        pdf,
        "Cost-to-serve ledger (window) - a cost-structure view, NOT a margin statement",
        ["item", "GBP", "provenance", "basis"],
        rows,
        [0.14, 0.13, 0.15, 0.48],
        row_colors=colors,
        fontsize=8.5,
        note=f"Modelled cost equals {costing['cost_pct_of_revenue']:.1f}% of window revenue "
             "- every rate is INVENTED and labelled; no profit claims.",
    )

    ledger_rows = [
        [e["key"], f"{e['value']:,.2f}", e["unit"], e["provenance"]]
        for e in art["ledger"]
    ]
    ledger_colors = [_prov_color(e["provenance"]) for e in art["ledger"]]
    _table_page(
        pdf,
        "Reconciliation ledger - every number a stage stands on",
        ["key", "value", "unit", "provenance"],
        ledger_rows,
        [0.30, 0.18, 0.12, 0.18],
        row_colors=ledger_colors,
        fontsize=7.2,
    )


def build_pdf(art: dict[str, Any], path: Path = paths.DELIVERABLE_PDF) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = artifact_mod.stale_files(art)
    with PdfPages(path, metadata=_PDF_METADATA) as pdf:
        _page_cover(pdf, art, stale)
        _page_pipeline(pdf, art)
        _page_identities(pdf, art)
        _page_comparisons(pdf, art)
        _page_ledger(pdf, art)
    return path


# --------------------------------------------------------------------------- #
# Excel LEDGER workbook
# --------------------------------------------------------------------------- #
def _normalize_zip(path: Path) -> None:
    """Rewrite an .xlsx (a zip) with fixed entry timestamps and sorted names.

    openpyxl stamps each zip entry with the wall clock AND rewrites the
    dcterms:modified core property at save time, so two builds from the same
    artifact would differ byte-for-byte. This pass pins both.
    """
    with zipfile.ZipFile(path, "r") as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}
    core = "docProps/core.xml"
    if core in entries:
        fixed = _FIXED_TS.strftime("%Y-%m-%dT%H:%M:%SZ")
        entries[core] = re.sub(
            rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
            (r"\g<1>" + fixed + r"\g<2>").encode(),
            entries[core],
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zout.writestr(info, entries[name])
    path.write_bytes(buffer.getvalue())


def build_excel(art: dict[str, Any], path: Path = paths.DELIVERABLE_XLSX) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    stages_df = pd.DataFrame(
        [
            {
                "stage": s["id"],
                "name": s["name"],
                "provenance": s["provenance"],
                "headline": s["headline"]["label"],
                "value": s["headline"]["value"],
            }
            for s in art["stages"]
        ]
    )
    identities_df = pd.DataFrame(art["identities"])[
        ["name", "lhs_label", "lhs", "rhs_label", "rhs", "tolerance", "unit", "passed", "note"]
    ]
    identities_df["status"] = identities_df["passed"].map({True: "PASS", False: "FAIL"})
    costing = artifact_mod.stage_by_name(art, "costing")["detail"]
    cost_df = pd.DataFrame(costing["lines"])[["item", "gbp", "provenance", "basis"]]
    slotting_df = pd.DataFrame(
        artifact_mod.stage_by_name(art, "warehouse")["detail"]["slotting_comparison"]
    )
    ledger_df = pd.DataFrame(art["ledger"])[["key", "value", "unit", "provenance", "note"]]

    assumption_rows = [
        {"assumption": "source", "value": art["source"]},
        {"assumption": "schema_version", "value": art["schema_version"]},
        {"assumption": "synthetic_seed", "value": art["synthetic_seed"]},
        {"assumption": "identities", "value": f"{art['identities_passed']}/{art['identities_total']} PASS"},
    ]
    for key, value in costing["assumptions"].items():
        assumption_rows.append({"assumption": f"costing/{key}", "value": value})
    for row in art["boundary_map"]:
        assumption_rows.append(
            {"assumption": f"boundary/{row['quantity']}", "value": f"{row['provenance']} - {row['note']}"}
        )
    assumptions_df = pd.DataFrame(assumption_rows)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        stages_df.to_excel(writer, sheet_name="Stages", index=False)
        identities_df.drop(columns=["passed"]).to_excel(writer, sheet_name="Identities", index=False)
        cost_df.to_excel(writer, sheet_name="CostToServe", index=False)
        slotting_df.to_excel(writer, sheet_name="SlottingComparison", index=False)
        ledger_df.to_excel(writer, sheet_name="Ledger", index=False)
        assumptions_df.to_excel(writer, sheet_name="Assumptions", index=False)
        # Fixed timestamps: regenerating from the same artifact is byte-identical.
        props = writer.book.properties
        props.created = _FIXED_TS
        props.modified = _FIXED_TS
    _normalize_zip(path)
    return path


# --------------------------------------------------------------------------- #
# CLI entry (python -m chain --deliverables)
# --------------------------------------------------------------------------- #
def main(artifact_path: Path = paths.ARTIFACT_JSON) -> int:
    if not Path(artifact_path).exists():
        print(f"ERROR: no run artifact at {artifact_path}")
        print("Run once (slow, ~51 min on the full dataset):")
        print("  python -m chain --report --save-artifact")
        return 1
    art = artifact_mod.load(artifact_path)
    stale = artifact_mod.stale_files(art)
    print(f"artifact: {artifact_path} ({art['source']} run, schema {art['schema_version']}, "
          f"{art['identities_passed']}/{art['identities_total']} identities PASS)")
    if stale:
        print(f"WARNING: artifact is STALE - {len(stale)} chain/ file(s) changed since it "
              f"was saved: {', '.join(stale)}")
    pdf = build_pdf(art)
    print(f"deliverable: {pdf} ({pdf.stat().st_size:,} bytes)")
    xlsx = build_excel(art)
    print(f"deliverable: {xlsx} ({xlsx.stat().st_size:,} bytes)")
    return 0
