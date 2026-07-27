"""Run artifact: the serialized receipt of one full chain run (phase 4).

``python -m chain --report --save-artifact`` runs stages 0-6 once and writes
``artifacts/full_run.json`` — a deterministic, versioned JSON snapshot of
every number the report printed: stage headline summaries, the reconciliation
ledger, all identity-check results (both sides, tolerance, PASS/FAIL), the
cost-to-serve lines, the slotting and routing comparisons, and the provenance
tag on every entry.

Why an artifact at all: the full run solves 48 CVRP instances and takes ~51
minutes. The dashboard (app.py) and the executive deliverables
(chain/exports.py) read THIS file instead of recomputing, so they boot in
seconds and always quote the numbers of one specific, committed run.

Determinism: same code + same data -> byte-identical JSON. There is no
wall-clock timestamp in the file; keys are sorted; floats are emitted with
Python's shortest-repr (itself deterministic). The run is identified by the
``code_fingerprint`` block — a sha256 per ``chain/*.py`` source file at save
time — which also powers staleness detection: if any current source hash
differs from the stored one, consumers must say the artifact is stale.

Schema versioning: ``schema_version`` gates consumers; bump on breaking
structural change. Consumers reject a major version they do not know.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from chain import inventory, paths
from chain.contracts import (
    CheckResult,
    CleanedTransactions,
    InvoiceStream,
    Provenance,
    WeeklyDemand,
)
from chain.costing import CostingResult
from chain.deliver import DeliveryResult
from chain.reconcile import Ledger
from chain.warehouse import WarehouseResult

SCHEMA_VERSION = "1.0"

# The README boundary table, machine-readable (the dashboard renders this).
BOUNDARY_MAP: list[dict[str, str]] = [
    {"quantity": "Transactions, invoice composition", "provenance": "real",
     "note": "UCI Online Retail II"},
    {"quantity": "Demand (weekly units per SKU)", "provenance": "real",
     "note": "lossless aggregation; identity (b)"},
    {"quantity": "Seasonality, returns", "provenance": "real", "note": "observed"},
    {"quantity": "Forecasts, uncertainty", "provenance": "derived",
     "note": "rolling-origin CV on real demand"},
    {"quantity": "SKU dimensions / weights", "provenance": "synthetic-assigned",
     "note": "seeded description-keyword size classes"},
    {"quantity": "Supplier lead times", "provenance": "synthetic-assigned",
     "note": "seeded per demand class + jitter"},
    {"quantity": "Warehouse geometry, slotting", "provenance": "synthetic-assigned",
     "note": "8 aisles x 25 bays, rectilinear"},
    {"quantity": "Customer geography", "provenance": "synthetic-assigned",
     "note": "digest-seeded coordinates; real country sets the band only"},
    {"quantity": "Picker crew, cartons, vehicles", "provenance": "synthetic-assigned",
     "note": "4 pickers, 60x40x40cm/20kg cartons, 80-carton vans"},
    {"quantity": "Cost rates (labour, km, holding, facility)",
     "provenance": "synthetic-assigned", "note": "printed on every ledger line"},
]


# --------------------------------------------------------------------------- #
# Code fingerprint + staleness
# --------------------------------------------------------------------------- #
def code_fingerprint() -> dict[str, str]:
    """sha256 per chain/*.py source file (sorted, posix-relative paths)."""
    out: dict[str, str] = {}
    for py in sorted((paths.ROOT / "chain").glob("*.py")):
        digest = hashlib.sha256(py.read_bytes()).hexdigest()
        out[py.relative_to(paths.ROOT).as_posix()] = digest
    return out


def stale_files(artifact: dict[str, Any]) -> list[str]:
    """Source files whose current hash differs from the artifact's (or are new/gone)."""
    saved: dict[str, str] = artifact.get("code_fingerprint", {})
    current = code_fingerprint()
    changed = [f for f in sorted(set(saved) | set(current)) if saved.get(f) != current.get(f)]
    return changed


def is_stale(artifact: dict[str, Any]) -> bool:
    return bool(stale_files(artifact))


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _iso(ts: object) -> str:
    return pd.Timestamp(ts).date().isoformat()  # type: ignore[arg-type]


def _records(df: pd.DataFrame, date_cols: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """DataFrame -> list of plain-python dicts (dates as ISO strings)."""
    df = df.copy()
    for col in date_cols:
        if col in df.columns:
            df[col] = df[col].map(_iso)
    records: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, float | int | str | bool) or value is None:
                clean[key] = value
            elif hasattr(value, "item"):  # numpy scalar
                clean[key] = value.item()
            else:
                clean[key] = str(value)
        records.append(clean)
    return records


def _check_dict(check: CheckResult) -> dict[str, Any]:
    return {
        "name": check.name,
        "lhs_label": check.lhs_label,
        "lhs": check.lhs,
        "rhs_label": check.rhs_label,
        "rhs": check.rhs,
        "tolerance": check.tolerance,
        "unit": check.unit,
        "passed": check.passed,
        "note": check.note,
    }


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(
    *,
    source: str,
    raw_rows: int,
    cleaned: CleanedTransactions,
    demand: WeeklyDemand,
    stream: InvoiceStream,
    fc: Any,
    layers: Any,
    plan: Any,
    wh: WarehouseResult,
    ful: Any,
    dl: DeliveryResult,
    cs: CostingResult,
    ledger: Ledger,
    checks: list[CheckResult],
) -> dict[str, Any]:
    """The versioned artifact dict for one completed run (stages 0-6)."""
    window = ful.window_weeks
    n_days = len(ful.per_day)
    n_pickers = int(ful.assumptions["n_pickers"])
    cvrp_km, cw_km = dl.total_cvrp_km, dl.total_cw_km
    wins = int((dl.per_day["CvrpKm"] < dl.per_day["CwKm"] - 1e-6).sum())
    ties = int((abs(dl.per_day["CvrpKm"] - dl.per_day["CwKm"]) <= 1e-6).sum())
    cost_ratio = cs.total_cost_gbp / cs.window_revenue_gbp if cs.window_revenue_gbp else 0.0
    inv_summary = inventory.class_summary(plan)
    tracked_lines = float(
        ledger.value("ingest/line_count") if "ingest/line_count" in ledger.entries else 0
    )

    stages = [
        {
            "id": 0,
            "name": "ingest",
            "provenance": cleaned.provenance.value,
            "headline": {"label": "cleaned revenue (GBP)", "value": cleaned.revenue_gbp},
            "detail": {
                "raw_rows": raw_rows,
                "cleaned_sales_rows": len(cleaned.sales),
                "returns_rows": len(cleaned.returns),
                "tracked_skus": len(demand.skus),
                "tracked_lines": tracked_lines,
                "weeks": len(demand.weeks),
                "week_start": _iso(demand.weeks[0]),
                "week_end": _iso(demand.weeks[-1]),
                "invoices": stream.n_invoices,
                "stream_lines": stream.n_lines,
                "cleaning_log": list(cleaned.steps),
            },
        },
        {
            "id": 1,
            "name": "forecast",
            "provenance": fc.provenance.value,
            "headline": {
                "label": "SKUs forecast",
                "value": float(fc.cv["StockCode"].nunique()) if len(fc.cv) else 0.0,
            },
            "detail": {
                "horizon_weeks": fc.horizon_weeks,
                "class_winners": _records(fc.class_winners),
            },
        },
        {
            "id": 2,
            "name": "inventory",
            "provenance": plan.provenance.value,
            "headline": {
                "label": "safety stock (units)",
                "value": float(plan.plan["SafetyStock"].sum()),
            },
            "detail": {
                "skus_planned": int(plan.plan["StockCode"].nunique()),
                "order_up_to_units": float(plan.plan["OrderUpTo"].sum()),
                "service_level": plan.assumptions["service_level"],
                "class_summary": _records(inv_summary),
            },
        },
        {
            "id": 3,
            "name": "warehouse",
            "provenance": wh.workload.provenance.value,
            "headline": {
                "label": "mean pick travel, deployed (m/invoice)",
                "value": wh.comparison.mean_travel("optimal"),
            },
            "detail": {
                "deployed_variant": "optimal (linear assignment)",
                "slotting_comparison": _records(wh.comparison.summary()),
            },
        },
        {
            "id": 4,
            "name": "fulfilment (DES)",
            "provenance": ful.provenance.value,
            "headline": {"label": "orders shipped (window)", "value": float(len(ful.orders))},
            "detail": {
                "window_weeks": len(window),
                "window_start": _iso(window[0]),
                "window_end": _iso(window[-1]),
                "working_days": n_days,
                "lines_picked": int(ful.orders["Lines"].sum()),
                "cartons_shipped": ful.cartons_shipped,
                "labour_hours": ful.labour_hours,
                "n_pickers": n_pickers,
                "mean_wait_min": float(ful.orders["WaitMin"].mean()) if len(ful.orders) else 0.0,
                "p95_wait_min": (
                    float(ful.orders["WaitMin"].quantile(0.95)) if len(ful.orders) else 0.0
                ),
                "per_day": _records(
                    ful.per_day[["Day", "Orders", "Lines", "LabourHours", "Cartons"]],
                    date_cols=("Day",),
                ),
            },
        },
        {
            "id": 5,
            "name": "transport (CVRP)",
            "provenance": dl.plan.provenance.value,
            "headline": {"label": "CVRP route km (window)", "value": cvrp_km},
            "detail": {
                "cw_km": cw_km,
                "cvrp_km": cvrp_km,
                "delta_vs_cw_pct": 100.0 * (cvrp_km - cw_km) / cw_km if cw_km else 0.0,
                "delivery_days": len(dl.per_day),
                "drops_routed": dl.routed_drops,
                "cvrp_vehicle_days": int(dl.per_day["CvrpVehicles"].sum()),
                "cw_vehicle_days": int(dl.per_day["CwVehicles"].sum()),
                "record": {"wins": wins, "ties": ties, "losses": len(dl.per_day) - wins - ties},
                "solution_limit": dl.plan.assumptions["solution_limit"],
                "per_day": _records(
                    dl.per_day[["Day", "Drops", "Cartons", "CvrpKm", "CwKm",
                                "CvrpVehicles", "CwVehicles"]],
                    date_cols=("Day",),
                ),
            },
        },
        {
            "id": 6,
            "name": "costing",
            "provenance": Provenance.SYNTHETIC_ASSIGNED.value,
            "headline": {"label": "modelled cost (GBP, window)", "value": cs.total_cost_gbp},
            "detail": {
                "window_revenue_gbp": cs.window_revenue_gbp,
                "cost_pct_of_revenue": 100.0 * cost_ratio,
                "lines": [
                    {
                        "item": line.item,
                        "gbp": line.gbp,
                        "provenance": line.provenance.value,
                        "basis": line.basis,
                    }
                    for line in cs.lines
                ],
                "assumptions": {
                    k: v for k, v in cs.cost.assumptions.items() if isinstance(v, str | int | float)
                },
            },
        },
    ]

    identities = [_check_dict(c) for c in checks]
    ledger_rows = [
        {
            "key": e.key,
            "value": e.value,
            "unit": e.unit,
            "provenance": e.provenance.value,
            "note": e.note,
        }
        for e in ledger.entries.values()
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "code_fingerprint": code_fingerprint(),
        "boundary_map": BOUNDARY_MAP,
        "stages": stages,
        "identities": identities,
        "identities_passed": sum(1 for c in checks if c.passed),
        "identities_total": len(checks),
        "all_passed": all(c.passed for c in checks),
        "ledger": ledger_rows,
        "synthetic_seed": layers.seed,
    }


# --------------------------------------------------------------------------- #
# Save / load
# --------------------------------------------------------------------------- #
def save(artifact: dict[str, Any], path: Path = paths.ARTIFACT_JSON) -> Path:
    """Deterministic write: sorted keys, ASCII, indent 2, trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def load(path: Path = paths.ARTIFACT_JSON) -> dict[str, Any]:
    """Load + gate on the schema version (consumers reject unknown majors)."""
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    version = str(artifact.get("schema_version", ""))
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ValueError(
            f"artifact schema {version!r} is not compatible with {SCHEMA_VERSION!r}; "
            "regenerate with: python -m chain --report --save-artifact"
        )
    return artifact


def stage_by_name(artifact: dict[str, Any], name: str) -> dict[str, Any]:
    for stage in artifact["stages"]:
        if stage["name"] == name:
            return stage
    raise KeyError(name)
