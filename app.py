"""CHAIN DASHBOARD — the decision chain's run artifact, rendered (phase 4).

Reads ``artifacts/full_run.json`` (the committed receipt of one measured full
run; see chain/artifact.py) and renders it. NOTHING is recomputed here — the
full run takes ~51 minutes because of 48 CVRP instances, so the dashboard
boots in seconds from the artifact and always quotes the numbers of the run
that was actually measured. If any ``chain/*.py`` source has changed since
the artifact was saved, a staleness banner says so.

Views (one page, server-rendered, offline assets only — no CDNs):

* stage flow      the 7 stages left-to-right, each with its headline number
                  and provenance tag (real=green, derived=blue,
                  synthetic-assigned=amber);
* RECONCILIATION  the hero panel: all 13 identities, both numbers, PASS badges;
* boundary map    what is real vs assigned (the README table, rendered);
* cost-to-serve   the ledger lines with provenance tags;
* slotting        random / abc / optimal, measured on identical invoices;
* routing         CVRP vs Clarke-Wright per delivery day (hand-built SVG);
* forecast        the per-class model scoreboard, winners marked.

API: ``/api/health`` plus JSON endpoints, all reading the same artifact.

Run:  python app.py            (port 5077; CHAIN_PORT overrides)
      CHAIN_ARTIFACT=<path>    read a different artifact (tests use this)
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, render_template

from chain import artifact as artifact_mod
from chain import paths

DEFAULT_PORT = 5077

# Provenance -> CSS class (the color code the whole dashboard shares).
PROVENANCE_CLASS = {
    "real": "prov-real",            # green
    "derived": "prov-derived",      # blue
    "synthetic-assigned": "prov-synth",  # amber
}


def _fmt(value: object, decimals: int | None = None) -> str:
    """Compact number formatting for templates: thousands separators, sane decimals."""
    if not isinstance(value, int | float):
        return str(value)
    if decimals is None:
        decimals = 0 if float(value).is_integer() or abs(value) >= 10_000 else 2
    return f"{value:,.{decimals}f}"


def _polyline(values: list[float], width: float, height: float, vmax: float) -> str:
    """SVG polyline points for a hand-built line chart (no external libraries)."""
    if not values or vmax <= 0:
        return ""
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = i * width / max(n - 1, 1)
        y = height - (v / vmax) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def create_app(artifact_path: str | Path | None = None) -> Flask:
    """App factory. Loads the artifact once; every route serves from it."""
    app = Flask(__name__)
    path = Path(artifact_path or os.environ.get("CHAIN_ARTIFACT", "") or paths.ARTIFACT_JSON)
    art = artifact_mod.load(path)
    stale = artifact_mod.stale_files(art)

    app.config["ARTIFACT"] = art
    app.config["ARTIFACT_PATH"] = str(path)
    app.config["STALE_FILES"] = stale
    app.jinja_env.filters["num"] = _fmt
    app.jinja_env.filters["provclass"] = lambda p: PROVENANCE_CLASS.get(str(p), "prov-synth")

    # ---- HTML ------------------------------------------------------------- #
    @app.route("/")
    def dashboard() -> str:
        stages = art["stages"]
        transport = artifact_mod.stage_by_name(art, "transport (CVRP)")["detail"]
        warehouse = artifact_mod.stage_by_name(art, "warehouse")["detail"]
        costing = artifact_mod.stage_by_name(art, "costing")["detail"]
        forecast = artifact_mod.stage_by_name(art, "forecast")["detail"]
        fulfil = artifact_mod.stage_by_name(art, "fulfilment (DES)")["detail"]
        ingest = artifact_mod.stage_by_name(art, "ingest")["detail"]

        # Slotting bars: widths relative to the worst (random) mean travel.
        slotting = warehouse["slotting_comparison"]
        max_travel = max(row["mean_travel_m"] for row in slotting) if slotting else 1.0
        slotting_bars = [
            {**row, "bar_pct": 100.0 * row["mean_travel_m"] / max_travel} for row in slotting
        ]

        # Routing chart: CVRP vs CW km per delivery day, hand-built polylines.
        per_day = transport["per_day"]
        chart_w, chart_h = 640.0, 150.0
        vmax = max(
            [row["CvrpKm"] for row in per_day] + [row["CwKm"] for row in per_day] + [1.0]
        )
        routing_chart = {
            "width": chart_w,
            "height": chart_h,
            "vmax": vmax,
            "cvrp_points": _polyline([row["CvrpKm"] for row in per_day], chart_w, chart_h, vmax),
            "cw_points": _polyline([row["CwKm"] for row in per_day], chart_w, chart_h, vmax),
            "n_days": len(per_day),
            "first_day": per_day[0]["Day"] if per_day else "",
            "last_day": per_day[-1]["Day"] if per_day else "",
        }

        # Forecast scoreboard grouped by class, winners first.
        winners: dict[str, list[dict]] = {}
        for row in forecast["class_winners"]:
            winners.setdefault(str(row["class"]), []).append(row)
        for rows in winners.values():
            rows.sort(key=lambda r: r["mean_mase"])

        return render_template(
            "dashboard.html",
            art=art,
            stages=stages,
            identities=art["identities"],
            boundary=art["boundary_map"],
            costing=costing,
            slotting=slotting_bars,
            routing=transport,
            routing_chart=routing_chart,
            winners=winners,
            fulfil=fulfil,
            ingest=ingest,
            stale_files=stale,
            artifact_path=str(path),
        )

    # ---- API -------------------------------------------------------------- #
    @app.route("/api/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "schema_version": art["schema_version"],
                "source": art["source"],
                "identities_passed": art["identities_passed"],
                "identities_total": art["identities_total"],
                "all_passed": art["all_passed"],
                "artifact": str(path),
                "artifact_stale": bool(stale),
                "stale_files": stale,
            }
        )

    @app.route("/api/artifact")
    def api_artifact():
        return jsonify(art)

    @app.route("/api/stages")
    def api_stages():
        return jsonify(art["stages"])

    @app.route("/api/identities")
    def api_identities():
        return jsonify(art["identities"])

    @app.route("/api/ledger")
    def api_ledger():
        return jsonify(art["ledger"])

    @app.route("/api/boundary")
    def api_boundary():
        return jsonify(art["boundary_map"])

    @app.route("/api/costing")
    def api_costing():
        return jsonify(artifact_mod.stage_by_name(art, "costing")["detail"])

    @app.route("/api/slotting")
    def api_slotting():
        return jsonify(artifact_mod.stage_by_name(art, "warehouse")["detail"]["slotting_comparison"])

    @app.route("/api/transport")
    def api_transport():
        return jsonify(artifact_mod.stage_by_name(art, "transport (CVRP)")["detail"])

    return app


if __name__ == "__main__":
    port = int(os.environ.get("CHAIN_PORT", str(DEFAULT_PORT)))
    create_app().run(host="127.0.0.1", port=port, debug=False)
