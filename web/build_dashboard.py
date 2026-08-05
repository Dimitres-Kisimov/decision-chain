"""Static CHAIN dashboard generator -- a self-contained, offline web view.

This lives at the REPO ROOT (``web/``), NOT under ``chain/``: it only ever
READS the chain package, so it can never change a ``chain/*.py`` byte and can
never stale the committed run receipt (``artifacts/full_run.json``, whose
identity is a sha256 per chain source file -- see ``chain/artifact.py``).

What it does
------------
Reads two COMMITTED receipts and renders one self-contained HTML page:

* ``artifacts/full_run.json``  -- the measured ~51-minute full run (stages,
  the 13 reconciliation identities, the boundary map, the cost ledger,
  slotting). Loaded through ``chain.artifact.load`` so the schema gate and the
  staleness banner come for free; NOTHING is recomputed here.
* ``web/scenario_fixture.json`` -- a frozen receipt of the two shock what-ifs
  (``scenario.py``'s demand-surge / cost-rate deltas), measured on the
  committed real-row fixture (the fast, deterministic CI path). The full-run
  baseline would need the 51-minute CVRP build, so the what-if is honestly
  labelled as the fixture path.

The output ``web/chain_dashboard.html`` inlines every byte of CSS and JS: no
CDN, no external font, no network at all. It is deterministic -- same two
receipts + same chain sources -> byte-identical HTML (no timestamps, fixed
number formatting, input order preserved).

Usage
-----
    python web/build_dashboard.py                 # build web/chain_dashboard.html
    python web/build_dashboard.py --out other.html
    python web/build_dashboard.py --check         # non-zero if committed HTML is stale
    python web/build_dashboard.py --refresh-scenarios
                                                  # re-measure the fixture what-ifs
                                                  # (imports scenario.py; ~1 min)

Honesty: the real vs derived vs synthetic-assigned boundary is rendered exactly
as the receipts label it (green / blue / amber, chip text always present, never
colour alone). Cost rates are labelled INVENTED; this is a cost-structure view,
never a profit or valuation claim. The UCI Online Retail II dataset is real and
carries CC BY 4.0 attribution (Chen, 2019; doi:10.24432/C5CG6D).
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

# Allow running as a script (``python web/build_dashboard.py``): the script's own
# directory (web/) is sys.path[0], so put the repo root on the path for ``chain``.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain import artifact as artifact_mod  # noqa: E402
from chain import paths  # noqa: E402

ROOT = paths.ROOT
WEB_DIR = ROOT / "web"
SCENARIO_JSON = WEB_DIR / "scenario_fixture.json"
DEFAULT_OUT = WEB_DIR / "chain_dashboard.html"

# Provenance -> (css class, human label). The colour code is shared with the
# Flask dashboard and the PDF: real = green, derived = blue, synthetic = amber.
PROV_CLASS = {
    "real": "prov-real",
    "derived": "prov-derived",
    "synthetic-assigned": "prov-synth",
}


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #
def esc(value: object) -> str:
    """HTML-escape any value's string form."""
    return html.escape(str(value), quote=True)


def fmt(value: object, decimals: int | None = None) -> str:
    """Compact number formatting: thousands separators, sane decimals.

    Mirrors app.py's ``_fmt`` so the static page quotes numbers identically to
    the served dashboard.
    """
    if not isinstance(value, int | float):
        return esc(value)
    if decimals is None:
        decimals = 0 if float(value).is_integer() or abs(value) >= 10_000 else 2
    return f"{value:,.{decimals}f}"


def prov_class(prov: object) -> str:
    return PROV_CLASS.get(str(prov), "prov-synth")


def prov_chip(prov: object) -> str:
    p = str(prov)
    return f'<span class="prov {prov_class(p)}">{esc(p)}</span>'


def pct_str(pct: float | None) -> str:
    return "n/a" if pct is None else f"{pct:+.2f}%"


# --------------------------------------------------------------------------- #
# Section renderers -- each returns a chunk of HTML
# --------------------------------------------------------------------------- #
def render_masthead(art: dict, stale: list[str]) -> str:
    verdict_cls = "pass" if art["all_passed"] else "fail"
    verdict_word = "PASS" if art["all_passed"] else "FAIL"
    return f"""  <header class="masthead">
    <div>
      <h1>decision-chain &middot; chain dashboard</h1>
      <div class="crumb">
        one real dataset through ingest &rarr; forecast &rarr; inventory &rarr;
        warehouse &rarr; fulfilment &rarr; transport &rarr; costing &mdash;
        machine-reconciled at every seam.
      </div>
    </div>
    <div class="masthead-right">
      <span class="verdict {verdict_cls}">{art['identities_passed']}/{art['identities_total']} identities {verdict_word}</span>
      <span class="chip">source: {esc(art['source'])} run</span>
      <span class="chip">schema {esc(art['schema_version'])}</span>
      <span class="chip">seed {esc(art['synthetic_seed'])}</span>
      <button class="btn" id="themeToggle" type="button" title="Toggle light / dark">theme</button>
    </div>
  </header>"""


def render_stale(stale: list[str]) -> str:
    if not stale:
        return ""
    plural = "s" if len(stale) != 1 else ""
    files = esc(", ".join(stale))
    return f"""  <div class="stale-banner" id="staleBanner">
    <strong>STALE ARTIFACT:</strong> the chain code is newer than this receipt
    ({len(stale)} changed file{plural}: {files}). Numbers below are from the last
    saved run &mdash; regenerate with <code>python -m chain --report --save-artifact</code>.
  </div>"""


def render_legend() -> str:
    return """  <div class="legend">
    <strong>provenance</strong>
    <span class="prov prov-real">real</span> observed in the dataset
    <span class="prov prov-derived">derived</span> computed from real inputs
    <span class="prov prov-synth">synthetic-assigned</span> invented, seeded (42), labelled
  </div>"""


def render_flow(art: dict) -> str:
    ingest = artifact_mod.stage_by_name(art, "ingest")["detail"]
    fulfil = artifact_mod.stage_by_name(art, "fulfilment (DES)")["detail"]
    cards: list[str] = []
    stages = art["stages"]
    for i, stage in enumerate(stages):
        cards.append(
            f"""      <div class="flow-stage">
        <div class="stage-card {prov_class(stage['provenance'])}-border">
          <div class="stage-name">{i} &middot; {esc(stage['name'])}</div>
          <div class="stage-number">{fmt(stage['headline']['value'])}</div>
          <div class="stage-label">{esc(stage['headline']['label'])}</div>
          {prov_chip(stage['provenance'])}
        </div>
      </div>"""
        )
        if i != len(stages) - 1:
            cards.append('      <div class="flow-arrow">&rarr;</div>')
    flow = "\n".join(cards)
    return f"""  <section class="panel" id="sec-flow">
    <h2>The chain &mdash; stage flow</h2>
    <div class="flow">
{flow}
    </div>
    <p class="note">
      {fmt(ingest['raw_rows'])} raw rows &middot; {fmt(ingest['tracked_skus'])} tracked SKUs
      &middot; {esc(ingest['weeks'])} weeks ({esc(ingest['week_start'])} .. {esc(ingest['week_end'])}).
      Stages 4&ndash;6 run on the same {esc(fulfil['window_weeks'])}-week representative window
      ({esc(fulfil['window_start'])} .. {esc(fulfil['window_end'])}), stated on every window identity.
    </p>
  </section>"""


def render_reconciliation(art: dict) -> str:
    rows: list[str] = []
    for check in art["identities"]:
        badge = "pass" if check["passed"] else "fail"
        word = "PASS" if check["passed"] else "FAIL"
        rows.append(
            f"""        <tr class="identity-row" data-status="{badge}">
          <td>
            <div class="id-name">{esc(check['name'])}</div>
            <div class="id-note">{esc(check['note'])}</div>
          </td>
          <td class="num">
            <div class="id-val">{fmt(check['lhs'], 2)}</div>
            <div class="id-label">{esc(check['lhs_label'])}</div>
          </td>
          <td class="num">
            <div class="id-val">{fmt(check['rhs'], 2)}</div>
            <div class="id-label">{esc(check['rhs_label'])}</div>
          </td>
          <td class="num">{esc(check['tolerance'])}</td>
          <td>{esc(check['unit'])}</td>
          <td><span class="badge {badge}">{word}</span></td>
        </tr>"""
        )
    body = "\n".join(rows)
    return f"""  <section class="panel hero" id="sec-reconciliation">
    <h2>Reconciliation &mdash; the {art['identities_total']} cross-stage identities</h2>
    <p class="note">
      Each identity compares two numbers computed by <em>independent code paths</em>
      (across stages, once across repositories). Both sides are shown &mdash; no rounding
      tricks, no hidden slack. This panel is the product.
    </p>
    <div class="table-scroll">
    <table class="identities">
      <thead>
        <tr><th>identity</th><th class="num">left side</th><th class="num">right side</th>
            <th class="num">tolerance</th><th>unit</th><th>status</th></tr>
      </thead>
      <tbody>
{body}
      </tbody>
    </table>
    </div>
  </section>"""


def render_boundary(art: dict) -> str:
    rows: list[str] = []
    for row in art["boundary_map"]:
        rows.append(
            f"""        <tr class="bnd-row" data-prov="{esc(row['provenance'])}">"""
            f"""<td class="bnd-q">{esc(row['quantity'])}</td>"""
            f"""<td>{prov_chip(row['provenance'])}</td>"""
            f"""<td class="muted small">{esc(row['note'])}</td></tr>"""
        )
    body = "\n".join(rows)
    counts: dict[str, int] = {}
    for row in art["boundary_map"]:
        counts[row["provenance"]] = counts.get(row["provenance"], 0) + 1
    tally = " &middot; ".join(
        f"{counts.get(p, 0)} {prov_chip(p)}" for p in ("real", "derived", "synthetic-assigned")
    )
    return f"""  <section class="panel" id="sec-boundary">
    <h2>Boundary map &mdash; what is real vs assigned</h2>
    <p class="note">
      The exact seam between measured fact and labelled assumption. Everything above the
      line is observed in UCI Online Retail II or derived from it; everything below is
      invented, seeded and labelled &mdash; never dressed up as real. Tally: {tally}.
    </p>
    <div class="table-scroll">
    <table class="boundary">
      <thead><tr><th>quantity</th><th>provenance</th><th>note</th></tr></thead>
      <tbody>
{body}
      </tbody>
    </table>
    </div>
  </section>"""


def render_costing(art: dict) -> str:
    costing = artifact_mod.stage_by_name(art, "costing")["detail"]
    rows: list[str] = []
    for line in costing["lines"]:
        total = " total-row" if line["item"] == "total cost" else ""
        rows.append(
            f"""        <tr class="{total.strip()}">"""
            f"""<td>{esc(line['item'])}</td>"""
            f"""<td class="num">{fmt(line['gbp'], 2)}</td>"""
            f"""<td>{prov_chip(line['provenance'])}</td>"""
            f"""<td class="muted small">{esc(line['basis'])}</td></tr>"""
        )
    body = "\n".join(rows)
    return f"""    <section class="panel" id="sec-costing">
      <h2>Cost-to-serve ledger (window)</h2>
      <p class="note">Cost-<em>structure</em> view, NOT a margin statement: every rate is invented and labelled synthetic-assigned.</p>
      <div class="table-scroll">
      <table class="costing">
        <thead><tr><th>item</th><th class="num">GBP</th><th>provenance</th><th>basis</th></tr></thead>
        <tbody>
{body}
        </tbody>
      </table>
      </div>
      <p class="note">Modelled cost is {fmt(costing['cost_pct_of_revenue'], 1)}% of the window's real revenue &mdash; under labelled assumptions only.</p>
    </section>"""


def render_slotting(art: dict) -> str:
    warehouse = artifact_mod.stage_by_name(art, "warehouse")["detail"]
    slotting = warehouse["slotting_comparison"]
    max_travel = max((r["mean_travel_m"] for r in slotting), default=1.0) or 1.0
    bars: list[str] = []
    for i, row in enumerate(slotting):
        pct = 100.0 * row["mean_travel_m"] / max_travel
        best = " bar-best" if i == len(slotting) - 1 else ""
        delta = (
            f' <span class="muted">({fmt(row["delta_vs_random_pct"], 1)}% vs random)</span>'
            if row["variant"] != "random"
            else ""
        )
        bars.append(
            f"""        <div class="bar-row">
          <div class="bar-label">{esc(row['variant'])}</div>
          <div class="bar-track"><div class="bar-fill{best}" style="width: {fmt(pct, 1)}%"></div></div>
          <div class="bar-value">{fmt(row['mean_travel_m'], 1)} m{delta}</div>
        </div>"""
        )
    body = "\n".join(bars)
    return f"""    <section class="panel" id="sec-slotting">
      <h2>Slotting comparison</h2>
      <p class="note">Three slottings, the <em>identical</em> real invoice set (identity (g)); mean pick travel per invoice on synthetic geometry.</p>
      <div class="bars">
{body}
      </div>
      <p class="note">Reported, not hidden: with one distance per slot the assignment optimum <em>is</em> velocity-sorted placement; optimal only re-breaks velocity ties vs abc.</p>
    </section>"""


def _delta_by_metric(scenario: dict) -> dict[str, dict]:
    return {d["metric"]: d for d in scenario["deltas"]}


def render_scenarios(scen: dict) -> str:
    tabs: list[str] = []
    panels: list[str] = []
    for i, s in enumerate(scen["scenarios"]):
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab{active}" data-tab="scn{i}" type="button">{esc(s["kind"])}</button>'
        )

        # before/after bars, scaled to the perturbed value of the driving line
        by_metric = _delta_by_metric(s)
        total = by_metric.get("total cost")
        driver = None
        for cand in ("holding cost", "transport cost"):
            d = by_metric.get(cand)
            if d and abs(d["delta"]) > 1e-9:
                driver = d
                break
        bar_lines = [d for d in (driver, total) if d is not None]
        bars: list[str] = []
        for d in bar_lines:
            # Scale each before/after pair to ITS OWN larger value, so the change
            # within a metric is visible (different metrics have different units).
            g_max = max(d["baseline"], d["perturbed"]) or 1.0
            b_pct = 100.0 * d["baseline"] / g_max
            p_pct = 100.0 * d["perturbed"] / g_max
            moved = "moved" if abs(d["delta"]) > 1e-9 else "flat"
            bars.append(
                f"""          <div class="ba-group">
            <div class="ba-metric">{esc(d['metric'])} <span class="muted">({esc(d['unit'])}, {esc(d['provenance'])})</span></div>
            <div class="ba-row"><span class="ba-tag">before</span>
              <div class="bar-track"><div class="bar-fill ba-before" style="width: {fmt(b_pct, 1)}%"></div></div>
              <span class="ba-val">{fmt(d['baseline'], 2)}</span></div>
            <div class="ba-row"><span class="ba-tag">after</span>
              <div class="bar-track"><div class="bar-fill ba-after {moved}" style="width: {fmt(p_pct, 1)}%"></div></div>
              <span class="ba-val">{fmt(d['perturbed'], 2)} <span class="delta">{pct_str(d['pct'])}</span></span></div>
          </div>"""
            )
        bars_html = "\n".join(bars)

        drows: list[str] = []
        for d in s["deltas"]:
            moved_cls = "" if abs(d["delta"]) > 1e-9 else " unchanged"
            drows.append(
                f"""            <tr class="{moved_cls.strip()}">"""
                f"""<td>{esc(d['stage'])}</td>"""
                f"""<td>{esc(d['metric'])}</td>"""
                f"""<td class="num">{fmt(d['baseline'], 2)}</td>"""
                f"""<td class="num">{fmt(d['perturbed'], 2)}</td>"""
                f"""<td class="num">{fmt(d['delta'], 2)}</td>"""
                f"""<td class="num">{pct_str(d['pct'])}</td>"""
                f"""<td>{prov_chip(d['provenance'])}</td></tr>"""
            )
        drows_html = "\n".join(drows)

        hold_cls = "pass" if s["identities_hold"] else "fail"
        hold_word = (
            f"{s['identities_passed']}/{s['identities_total']} identities still reconcile"
            if s["identities_hold"]
            else "IDENTITY FAILURE UNDER SHOCK"
        )
        panels.append(
            f"""      <div class="scn-panel{active}" id="scn{i}">
        <div class="scn-head">
          <div class="scn-title">{esc(s['name'])}</div>
          <span class="badge {hold_cls}">{esc(hold_word)}</span>
        </div>
        <p class="note">Perturbed input: {esc(s['perturbed_input'])} &nbsp;{prov_chip(s['input_provenance'])}</p>
        <div class="ba-bars">
{bars_html}
        </div>
        <div class="table-scroll">
        <table class="scn-table">
          <thead><tr><th>stage</th><th>metric</th><th class="num">baseline</th><th class="num">perturbed</th><th class="num">delta</th><th class="num">pct</th><th>provenance</th></tr></thead>
          <tbody>
{drows_html}
          </tbody>
        </table>
        </div>
      </div>"""
        )
    tabs_html = "\n      ".join(tabs)
    panels_html = "\n".join(panels)
    return f"""  <section class="panel" id="sec-scenario">
    <h2>Scenario what-if &mdash; shock one input, the ledger still reconciles</h2>
    <p class="note">
      Each scenario perturbs <strong>one</strong> input, re-runs only the stages that input
      feeds (reusing the stage engines verbatim), and re-checks <strong>all 13</strong>
      identities on the perturbed run. Source: <strong>{esc(scen['source'])}</strong> &mdash;
      the full-run baseline would need the 51-minute CVRP build, so the what-if runs the fast,
      deterministic fixture path (smaller magnitudes than the full run above; the mechanics are identical).
    </p>
    <div class="tabs">
      {tabs_html}
    </div>
{panels_html}
    <p class="note">
      Honest reading: the demand surge is a <em>planning-side</em> shock &mdash; it lifts the
      derived forecast and its safety-stock holding charge, but the physical fulfilment of the
      already-real orders (picks, cartons, CVRP km, labour) does not move, so those lines stay
      flat by construction. The cost-rate shock moves a labelled synthetic rate, not a distance.
      Neither is a profit statement; the real vs synthetic boundary is exactly the baseline's.
    </p>
  </section>"""


def render_footer(art: dict, scen: dict) -> str:
    return f"""  <footer class="foot">
    <div>Receipts: <code>artifacts/full_run.json</code> (full run) &middot;
      <code>web/scenario_fixture.json</code> (fixture what-if) &middot; schema {esc(art['schema_version'])}
      &middot; synthetic seed {esc(art['synthetic_seed'])} &middot; offline / self-contained (no CDNs, no network).</div>
    <div class="muted small">Real data: UCI Online Retail II &mdash; Chen (2019), CC BY 4.0 (doi:10.24432/C5CG6D),
      read at build time, not redistributed. Cost rates and physical layers are synthetic-assigned and labelled;
      this is a cost-structure view, not a profit or valuation claim. Proprietary &mdash; portfolio review only.</div>
  </footer>"""


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #
def _css() -> str:
    return CSS


def _js() -> str:
    return JS


def build_html(art: dict, scen: dict, stale: list[str]) -> str:
    """Assemble the full self-contained page. Deterministic: no timestamps."""
    favicon = (
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'"
        "%3E%3Crect width='32' height='32' rx='7' fill='%231d9e6f'/%3E%3Ctext x='16' y='22'"
        " font-size='15' font-family='Arial' font-weight='bold' fill='white'"
        " text-anchor='middle'%3EDC%3C/text%3E%3C/svg%3E"
    )
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8" />',
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
        "  <title>decision-chain &middot; chain dashboard</title>",
        '  <meta name="description" content="One real dataset through the whole distributor'
        ' decision chain, machine-reconciled at every seam." />',
        f'  <link rel="icon" href="{favicon}" />',
        f"  <style>\n{_css()}\n  </style>",
        "</head>",
        "<body>",
        '<div class="page">',
        render_masthead(art, stale),
        render_stale(stale),
        render_legend(),
        render_flow(art),
        render_reconciliation(art),
        render_boundary(art),
        '  <div class="grid-2">',
        render_costing(art),
        render_slotting(art),
        "  </div>",
        render_scenarios(scen),
        render_footer(art, scen),
        "</div>",
        f"<script>\n{_js()}\n</script>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Load / write
# --------------------------------------------------------------------------- #
def load_scenarios(path: Path = SCENARIO_JSON) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_html(text: str, path: Path) -> Path:
    """Write with LF newlines on every platform (byte-deterministic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def generate(
    artifact_path: Path = paths.ARTIFACT_JSON,
    scenario_path: Path = SCENARIO_JSON,
) -> str:
    """Load both committed receipts and return the rendered HTML string."""
    art = artifact_mod.load(artifact_path)
    stale = artifact_mod.stale_files(art)
    scen = load_scenarios(scenario_path)
    return build_html(art, scen, stale)


# --------------------------------------------------------------------------- #
# Maintenance: refresh the fixture what-if receipt from scenario.py
# --------------------------------------------------------------------------- #
def _scenario_to_dict(res) -> dict:
    return {
        "name": res.name,
        "kind": res.kind,
        "perturbed_input": res.perturbed_input,
        "input_provenance": res.input_provenance.value,
        "factor": res.factor,
        "identities_passed": res.identities_passed,
        "identities_total": len(res.perturbed_checks),
        "identities_hold": res.identities_hold,
        "deltas": [
            {
                "stage": d.stage,
                "metric": d.metric,
                "unit": d.unit,
                "provenance": d.provenance.value,
                "baseline": d.baseline,
                "perturbed": d.perturbed,
                "delta": d.delta,
                "pct": d.pct,
            }
            for d in res.deltas
        ],
    }


def refresh_scenarios(path: Path = SCENARIO_JSON) -> Path:
    """Re-measure the two shock what-ifs on the fixture and write the receipt.

    Reuses scenario.py verbatim (which reuses the chain stage engines); nothing
    under chain/ is touched. Deterministic: sorted keys, ASCII, LF.
    """
    import scenario  # local import: only needed for this maintenance path

    base = scenario.build_baseline(fixture=True)
    results = scenario.run_all(base)
    payload = {
        "source": "committed real-row fixture (deterministic CI path)",
        "generated_by": "python web/build_dashboard.py --refresh-scenarios (reuses scenario.py)",
        "surge_factor": scenario.SURGE_FACTOR,
        "cost_shock_factor": scenario.COST_SHOCK_FACTOR,
        "scenarios": [_scenario_to_dict(r) for r in results],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_dashboard", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output HTML path")
    parser.add_argument(
        "--artifact", type=Path, default=paths.ARTIFACT_JSON, help="run artifact JSON"
    )
    parser.add_argument(
        "--scenarios", type=Path, default=SCENARIO_JSON, help="scenario what-if JSON"
    )
    parser.add_argument(
        "--refresh-scenarios", action="store_true",
        help="re-measure the fixture what-ifs from scenario.py, then build",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="exit non-zero if the committed HTML differs from a fresh build",
    )
    args = parser.parse_args(argv)

    if args.refresh_scenarios:
        out = refresh_scenarios(args.scenarios)
        print(f"refreshed: {out}")

    html_text = generate(args.artifact, args.scenarios)

    if args.check:
        if not args.out.exists():
            print(f"MISSING: {args.out} does not exist; run without --check to build it")
            return 1
        on_disk = args.out.read_text(encoding="utf-8").replace("\r\n", "\n")
        if on_disk != html_text:
            print(f"STALE: {args.out} differs from a fresh build; rebuild and commit it")
            return 1
        print(f"OK: {args.out} is in sync with the receipts")
        return 0

    write_html(html_text, args.out)
    print(f"wrote: {args.out} ({len(html_text):,} bytes)")
    return 0


# --------------------------------------------------------------------------- #
# Inlined assets (offline: no CDN, no external font, no network)
# --------------------------------------------------------------------------- #
CSS = """    :root {
      --green:#1d9e6f; --blue:#2f6bff; --amber:#e8a33d; --amber-ink:#b07617; --red:#d64550;
      --bg:#f4f6fb; --panel:#ffffff; --panel-2:#f8fafc; --line:#e5e9f2;
      --text:#1a2233; --muted:#6b7488;
      --shadow:0 1px 2px rgba(20,30,60,.06),0 8px 24px rgba(20,30,60,.06);
      --radius:12px;
      --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      --mono:"Cascadia Code",Consolas,"Courier New",monospace;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg:#0e131f; --panel:#161d2b; --panel-2:#1b2331; --line:#262f42;
        --text:#e8ecf5; --muted:#8b95ab; --amber-ink:#e8a33d;
        --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
      }
    }
    :root[data-theme="dark"] {
      --bg:#0e131f; --panel:#161d2b; --panel-2:#1b2331; --line:#262f42;
      --text:#e8ecf5; --muted:#8b95ab; --amber-ink:#e8a33d;
      --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
    }
    :root[data-theme="light"] {
      --bg:#f4f6fb; --panel:#ffffff; --panel-2:#f8fafc; --line:#e5e9f2;
      --text:#1a2233; --muted:#6b7488; --amber-ink:#b07617;
      --shadow:0 1px 2px rgba(20,30,60,.06),0 8px 24px rgba(20,30,60,.06);
    }
    * { box-sizing:border-box; }
    body { margin:0; font-family:var(--font); background:var(--bg); color:var(--text);
      font-size:14px; -webkit-font-smoothing:antialiased; }
    .page { max-width:1200px; margin:0 auto; padding:20px 24px 60px; }
    code { font-family:var(--mono); font-size:12px; }
    h2 { margin:0 0 8px; font-size:16px; }
    .table-scroll { overflow-x:auto; }

    .masthead { display:flex; justify-content:space-between; align-items:flex-start; gap:16px;
      padding:8px 0 16px; flex-wrap:wrap; }
    .masthead h1 { margin:0 0 4px; font-size:23px; letter-spacing:-.02em; }
    .crumb { color:var(--muted); max-width:660px; }
    .masthead-right { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .chip { background:var(--panel); border:1px solid var(--line); border-radius:999px;
      padding:4px 12px; color:var(--muted); font-size:12px; }
    .btn { background:var(--panel); border:1px solid var(--line); border-radius:8px;
      padding:5px 12px; color:var(--text); cursor:pointer; font-size:12px; }
    .btn:hover { border-color:var(--muted); }
    .verdict { border-radius:999px; padding:5px 14px; font-weight:700; font-size:13px; }
    .verdict.pass { background:rgba(29,158,111,.14); color:var(--green); border:1px solid var(--green); }
    .verdict.fail { background:rgba(214,69,80,.14); color:var(--red); border:1px solid var(--red); }

    .stale-banner { background:rgba(232,163,61,.14); border:1px solid var(--amber);
      border-radius:var(--radius); padding:10px 16px; margin-bottom:16px; }

    .prov { border-radius:999px; padding:2px 10px; font-size:11px; font-weight:700; white-space:nowrap; }
    .prov-real { background:rgba(29,158,111,.14); color:var(--green); border:1px solid var(--green); }
    .prov-derived { background:rgba(47,107,255,.12); color:var(--blue); border:1px solid var(--blue); }
    .prov-synth { background:rgba(232,163,61,.16); color:var(--amber-ink); border:1px solid var(--amber); }
    .legend { color:var(--muted); margin:0 0 16px; display:flex; gap:10px; align-items:center;
      flex-wrap:wrap; font-size:12px; }

    .panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
      box-shadow:var(--shadow); padding:18px 20px; margin-bottom:18px; }
    .panel.hero { border-width:2px; border-color:var(--green); }
    .note { color:var(--muted); font-size:12.5px; margin:6px 0; }
    .muted { color:var(--muted); }
    .small { font-size:12px; }
    .grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
    @media (max-width:980px) { .grid-2 { grid-template-columns:1fr; } }

    .flow { display:flex; align-items:stretch; gap:6px; overflow-x:auto; padding:8px 0; }
    .flow-stage { flex:1 1 0; min-width:128px; display:flex; }
    .flow-arrow { align-self:center; color:var(--muted); font-size:18px; flex:0 0 auto; }
    .stage-card { background:var(--panel-2); border:1px solid var(--line); border-top:3px solid var(--line);
      border-radius:10px; padding:10px 12px; width:100%; display:flex; flex-direction:column; gap:4px; }
    .stage-card .stage-name { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
    .stage-card .stage-number { font-size:19px; font-weight:700; font-variant-numeric:tabular-nums; }
    .stage-card .stage-label { font-size:11.5px; color:var(--muted); min-height:28px; }
    .stage-card .prov { align-self:flex-start; }
    .prov-real-border { border-top-color:var(--green); }
    .prov-derived-border { border-top-color:var(--blue); }
    .prov-synth-border { border-top-color:var(--amber); }

    table { border-collapse:collapse; width:100%; }
    th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); white-space:nowrap; }
    td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
    tr:last-child td { border-bottom:none; }

    .identities .id-name { font-weight:700; }
    .identities .id-note { color:var(--muted); font-size:11.5px; margin-top:2px; max-width:400px; }
    .identities .id-val { font-weight:600; font-variant-numeric:tabular-nums; }
    .identities .id-label { color:var(--muted); font-size:11px; font-family:var(--mono); }
    .identity-row:hover td { background:var(--panel-2); }

    .badge { border-radius:6px; padding:2px 10px; font-weight:700; font-size:11.5px; }
    .badge.pass { background:rgba(29,158,111,.16); color:var(--green); }
    .badge.fail { background:rgba(214,69,80,.16); color:var(--red); }

    .bnd-row .bnd-q { font-weight:600; }
    .costing .total-row td { font-weight:700; border-top:2px solid var(--line); }

    .bars { display:flex; flex-direction:column; gap:8px; margin:10px 0; }
    .bar-row { display:grid; grid-template-columns:70px 1fr 210px; gap:10px; align-items:center; }
    .bar-label { font-weight:600; }
    .bar-track { background:var(--panel-2); border:1px solid var(--line); border-radius:6px;
      height:18px; overflow:hidden; }
    .bar-fill { background:var(--amber); height:100%; border-radius:5px 0 0 5px; transition:width .2s; }
    .bar-fill.bar-best { background:var(--green); }
    .bar-value { font-variant-numeric:tabular-nums; font-size:12.5px; }

    .tabs { display:flex; gap:6px; margin:4px 0 14px; flex-wrap:wrap; }
    .tab { background:var(--panel-2); border:1px solid var(--line); border-radius:999px;
      padding:5px 14px; color:var(--muted); cursor:pointer; font-size:12.5px; font-weight:600; }
    .tab.active { background:rgba(47,107,255,.10); color:var(--blue); border-color:var(--blue); }
    .scn-panel { display:none; }
    .scn-panel.active { display:block; }
    .scn-head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:2px; }
    .scn-title { font-weight:700; font-size:14px; }
    .ba-bars { display:flex; flex-wrap:wrap; gap:18px; margin:12px 0 14px; }
    .ba-group { flex:1 1 320px; min-width:280px; }
    .ba-metric { font-size:12px; font-weight:600; margin-bottom:6px; }
    .ba-row { display:grid; grid-template-columns:52px 1fr auto; gap:8px; align-items:center; margin-bottom:5px; }
    .ba-tag { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
    .ba-before { background:var(--muted); }
    .ba-after { background:var(--amber); }
    .ba-after.flat { background:var(--muted); }
    .ba-val { font-variant-numeric:tabular-nums; font-size:12px; white-space:nowrap; }
    .delta { color:var(--amber-ink); font-weight:700; }
    .scn-table .unchanged td { color:var(--muted); }

    .foot { color:var(--muted); font-size:12px; margin-top:8px; display:flex; flex-direction:column; gap:6px; }"""

JS = """    (function () {
      var root = document.documentElement;
      var btn = document.getElementById('themeToggle');
      if (btn) {
        btn.addEventListener('click', function () {
          var dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
          var current = root.getAttribute('data-theme') || (dark ? 'dark' : 'light');
          root.setAttribute('data-theme', current === 'dark' ? 'light' : 'dark');
        });
      }
      var tabs = document.querySelectorAll('.tab');
      tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
          var id = tab.getAttribute('data-tab');
          document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
          document.querySelectorAll('.scn-panel').forEach(function (p) { p.classList.remove('active'); });
          tab.classList.add('active');
          var panel = document.getElementById(id);
          if (panel) { panel.classList.add('active'); }
        });
      });
    })();"""


if __name__ == "__main__":
    sys.exit(main())
