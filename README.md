# decision-chain

**One real dataset through the whole distributor decision chain — with machine-checked
reconciliation at every seam.**

My other repositories each solve one silo: cleaning, forecasting, warehouse simulation,
routing, costing. Real distributors do not fail inside silos; they fail **at the seams**,
where the forecast quietly uses different numbers than the invoices, and controlling
allocates cost over a different order count than the warehouse picked. This repo is the
integration capstone: the UCI *Online Retail II* transactions (~1,067,371 raw rows, two
years of a UK giftware distributor) flow through ingest -> forecast -> inventory ->
warehouse -> transport -> costing, and a **reconciliation ledger** (stage 6) asserts,
in pytest-able identity checks that print both numbers, that no stage drifted from the
one upstream. The reconciliation harness is not scaffolding — it is the product.

**The finished system (phases 1-4, all done).** One measured full run — 51 minutes,
48 per-day CVRPs — proves the chain closes: **all 13 cross-stage identities PASS**,
including cleaned revenue reproduced across two repositories *to the penny*
(GBP 19,643,861.62) and the ledger's window revenue equal to the cleaned data's,
again to the penny. The honest findings are part of the product: on lumpy demand
nothing beats the naive walk; the exact slotting optimum is worth 1.6% over classic
ABC; the metaheuristic router beats the 1964 Clarke-Wright construction by only
0.2% on this geography and loses 19 of 48 days. The run is serialized to a committed
**artifact** (`artifacts/full_run.json`), and the product layer reads it instead of
recomputing: the **CHAIN DASHBOARD** (Flask, offline assets) renders the stage flow,
the 13-identity reconciliation panel, the boundary map and every comparison; the
**deliverables** (`chain_report.pdf` + `chain_ledger.xlsx`) regenerate from the same
artifact byte-for-byte.

## The boundary: real vs synthetic-assigned

Honesty is the point of this portfolio, so the boundary is declared up front and never
blurred. Every quantity in the chain carries a provenance tag (`real` |
`synthetic-assigned` | `derived`); a derived quantity inherits the **weakest** provenance
of its inputs, and reports must print the tag.

| Quantity | Provenance | Status |
|---|---|---|
| Transactions, invoice composition | **REAL** (UCI Online Retail II) | phase 1 (done) |
| Demand (weekly units per SKU) | **REAL** (lossless aggregation) | phase 1 (done) |
| Seasonality, returns | **REAL** | phase 1 (done) |
| Forecasts, uncertainty | derived (from real) | phase 1 (done) |
| SKU dimensions / weights | **SYNTHETIC-ASSIGNED** (seeded, labelled) | **phase 2 (done)** — description-keyword size classes |
| Supplier lead times | **SYNTHETIC-ASSIGNED** (seeded, labelled) | **phase 2 (done)** — per demand class + jitter |
| Warehouse geometry, slotting | **SYNTHETIC-ASSIGNED** (seeded, labelled) | **phase 2 (done)** — 8 aisles x 25 bays, rectilinear |
| Customer geography (one 2D plane; the real country sets the distance band only) | **SYNTHETIC-ASSIGNED** (seeded, labelled) | **phase 3 (done)** — per-customer digest-seeded coordinates |
| Picker crew, carton dims, vehicle capacity | **SYNTHETIC-ASSIGNED** (labelled) | **phase 3 (done)** — 4 pickers, 60x40x40cm / 20kg cartons, 80-carton vans |
| Cost rates (labour, km, holding, facility) | **SYNTHETIC-ASSIGNED** (labelled) | **phase 3 (done)** — printed on every ledger line |

And the house standard on models: results are measured out-of-sample, and when a simple
baseline beats the fancier model on real data, the baseline wins the report.

## The chain (contracts first)

All seven stages are typed I/O contracts in [`chain/contracts.py`](chain/contracts.py) —
later phases implement against them:

| stage | in | out | phase |
|---|---|---|---|
| 0 ingest | raw workbook | `CleanedTransactions`, `WeeklyDemand`, `InvoiceStream` | **1 (done)** |
| 1 forecast | `WeeklyDemand` | `DemandForecast` (units/week + sigma per SKU) | **1 (done)** |
| 2 inventory | `DemandForecast` | `ReplenishmentPlan` | **2 (done)** |
| 3 warehouse | `InvoiceStream` + plan | `WarehouseWorkload` (pick lists) | **2 (done)** |
| 4 transport | `WarehouseWorkload` | `FulfilmentLog` (DES) + `TransportPlan` (CVRP) | **3 (done)** |
| 5 costing | all upstream | `CostToServe` (the provenance-tagged ledger) | **3 (done)** |
| 6 reconcile | ledger entries from 0-5 | identity checks (PASS/FAIL, both numbers) | **1-3 (a-m done)** |

## Phase 1 — what is measured (real data, full run)

Stage 0 adapts the documented cleaning pipeline from my
[retail-analytics-real](../retail-analytics-real) repo step for step — deliberately, because
identity (a) below only means something if it is the *same* pipeline. Tracked SKUs are the
top 200 product codes by cleaned gross revenue (the chain is about recurring demand; the
report prints the exact revenue share they cover).

### Identity checks (from `python -m chain --report`, full dataset, measured 2026-07-27)

| # | identity | lhs | rhs | result |
|---|---|---:|---:|---|
| a | cross-repo revenue, GBP (to the penny) | 19,643,861.62 | 19,643,861.62 | **PASS** |
| b | demand conservation (weekly sum == line sum, units) | 3,361,605 | 3,361,605 | **PASS** |
| c | line conservation (invoice stream == cleaned lines) | 256,787 | 256,787 | **PASS** |
| d | forecast coverage (forecast SKUs known to demand) | 185 | 185 | **PASS** |

Context from the same run: 1,067,371 raw rows -> 1,003,340 cleaned sales rows + 17,914
returns rows; 104-week index (2009-12-13 .. 2011-12-04); the 200 tracked SKUs carry
40.2% of cleaned revenue across 256,787 invoice lines on 33,492 invoices.

Identity (a) is the cross-repository proof: this repo's pipeline, run on the same raw
file, reproduces the revenue figure retail-analytics-real published (GBP 19,643,862 in
its README; exact value 19,643,861.62) — two codebases, one number.

### Forecast results (rolling-origin CV, MASE, per demand class)

Per-SKU weekly unit demand is classified into the Syntetos-Boylan quadrants
(ADI 1.32 / CV^2 0.49) and four from-scratch models compete per class under
leakage-safe rolling-origin CV (3 folds, 8-week horizon, MASE scaled by the
in-sample one-week naive walk):

185 of the 200 tracked SKUs are forecastable (the rest have under 5 nonzero weeks or too
little history for a leakage-safe fold). Measured winners, full dataset:

| class | SKUs | winner | mean MASE | runner-up | mean MASE |
|---|---:|---|---:|---|---:|
| smooth | 44 | croston_sba | 0.772 | lag_linear | 0.847 |
| erratic | 122 | croston_sba | 0.935 | lag_linear | 0.977 |
| intermittent | 1 | croston_sba | 0.983 | naive | 1.174 |
| lumpy | 18 | **naive** | 1.782 | seasonal_naive | 2.068 |

The honest readings: on lumpy demand **nothing beats the one-week naive walk**
(MASE > 1 across the board — those SKUs' spikes are not forecastable from their own
history, which is exactly what stage 2 safety stock has to absorb); Croston-SBA's win on
smooth/erratic classes comes largely from behaving like a well-damped level estimate; and
seasonal-naive never wins a class, because one full seasonal cycle of history (104 weeks,
52-week season) is barely one observation of the seasonality per SKU.

## Phase 2 — what is measured (real data, full run, 2026-07-27)

Phase 2 adds the first labelled synthetic layers (`chain/synthetic.py`, seed 42):
SKU dims/weights drawn per description-keyword size class (small: 20, medium: 136,
large: 44 of the 200 tracked SKUs), supplier lead times of 2-5 weeks assigned per
demand class, and a 200-slot warehouse (8 aisles x 25 bays, rectilinear cross-aisle
travel, dispatch at the front corner). Everything downstream of these layers is
provenance-capped at `synthetic-assigned`, and identity (h) machine-checks the labels.

### Stage 2 — replenishment (base-stock, 95% service, weekly review)

All 185 forecasted SKUs get a plan (identity (e)): total order-up-to 251,658 units,
total safety stock 91,878 units, on synthetic lead times that the plan declares.
The buffer each demand class carries, in weeks of its forecast demand (sqrt-law on
the *measured* rolling-origin sigma):

| class | SKUs | mean lead | pooled sigma/mu | safety-stock weeks |
|---|---:|---:|---:|---:|
| smooth | 44 | 2.5w | 0.55 | 1.70 |
| intermittent | 1 | 5.0w | 0.45 | 1.83 |
| lumpy | 18 | 4.4w | 0.60 | 2.30 |
| erratic | 122 | 3.5w | 0.86 | 3.02 |

The honest reading: lumpy demand is unforecastable (stage 1: nothing beats naive,
MASE 1.78), so its measured sigma buys 2.30 weeks of buffer against 1.70 for smooth
(1.4x) — the cost of unforecastability, not a modelling win. And measured, the
*widest* buffer per unit of demand is actually the erratic class (3.02 weeks): its
122 SKUs forecast decently in MASE terms yet still carry sigma/mu 0.86 into the
sqrt-law. Both numbers are reported as measured.

### Stage 3 — three slottings on the identical 33,492 real invoices

Real invoice lines become nearest-neighbour pick tours in the synthetic warehouse;
velocity is REAL (invoice lines per SKU); travel is `synthetic-assigned` (invented
geometry) and labelled so. Mean pick travel per invoice, same invoice set (identity (g)):

| slotting | mean travel / invoice | vs random | vs abc |
|---|---:|---:|---:|
| random (seeded baseline) | 214.3 m | — | — |
| abc (by real velocity) | 183.2 m | **-14.5%** | — |
| assignment-optimal (Hungarian) | 180.2 m | **-15.9%** | -1.6% |

Honest notes: with one scalar distance per slot, the linear-assignment optimum on
velocity x distance is exactly velocity-sorted placement (rearrangement inequality),
so `optimal` differs from `abc` only in how velocity ties are broken — measured on
real multi-line pick tours those tie-breaks are still worth 1.6%. On the small CI
fixture (mostly single-line invoices) the two are identical, and the tests assert
the ordering that actually holds, not a hoped-for one.

### Identity checks e-h (full dataset, all PASS — a-d unchanged)

| # | identity | lhs | rhs | result |
|---|---|---:|---:|---|
| e | replenishment covers every forecasted SKU | 185 | 185 | **PASS** |
| f | picked lines (all tours) == invoice-stream lines | 256,787 | 256,787 | **PASS** |
| g | identical invoice set across all three slottings | 256,787 | 256,787 | **PASS** |
| h | provenance audit (travel synthetic, demand/velocity real) | 6 | 6 | **PASS** |

## Phase 3 — what is measured (real data, full run, 2026-07-27)

Phase 3 makes the chain physical on a **representative 8-week window** — the
contiguous span with the most invoice lines (2010-10-24 .. 2010-12-12, which by
construction contains the peak picking week). Simulating all 104 weeks through
the per-day CVRP stage is slow, so stages 4-5 and every window identity run on
this SAME window, and every output says so. Real WHAT/WHEN/WHO; synthetic HOW:
crew size, carton dims, coordinates and every cost rate are invented and
labelled.

### Stage 4a — fulfilment DES (heapq, no SimPy; adapted from logistics-digital-twin)

The real invoice stream of the window arrives at its real timestamps and is
picked through the **deployed stage-3 slotting** (assignment-optimal — the
layout a planner would run, and the one the `WarehouseWorkload` contract
carries) by 4 synthetic pickers, then packed by an FFD volume+weight carton
fill (the 1D relaxation, labelled) using the seeded SKU dims:

| measure (window) | value |
|---|---:|
| orders shipped | 4,151 across 48 working days (86.5/day) |
| lines picked | 33,312 (identity (i) pins this to the stream) |
| picking labour | 270.4 h (~5.6 h/day — 18% of a 4-picker 8h-day crew; utilisation reported, not hidden) |
| mean order wait | 0.0 min (the crew is over-provisioned even in the peak; measured, not tuned away) |
| cartons shipped | 70,820 (FFD, 60x40x40 cm / 20 kg; identity (j)) |

### Stage 4b — transport: CVRP vs Clarke-Wright, measured honestly

Every shipped order becomes a drop on its ship day at a digest-seeded
coordinate (UK band 5-120 km, export band 150-300 km — one 2D plane, INVENTED
geography, the km are synthetic-assigned). Per delivery day, two solvers on
identical instances: Clarke-Wright (1964) parallel savings, and OR-Tools
CVRP (PATH_CHEAPEST_ARC + guided local search) with a **deterministic
solution-limit stop rule** — not wall-clock, so the same run gives the same
km on any machine (both adapted from my route-optimizer repo).

The solution limit was chosen at the measured knee of a sweep on a
representative 8-day subset of the window (622 orders):

| solution limit | CVRP vs CW | per-day record (8 days) | sweep runtime |
|---:|---:|---|---:|
| 100 | +4.66% (loses) | 0 wins / 8 losses | 7 s |
| 300 | +0.41% (near-tie) | 5 wins / 3 losses | 87 s |
| **1000 (chosen)** | **-1.08% (wins)** | 6 wins / 2 losses | 350 s |

The sweep was stopped after the 1000-solution point: the gain per extra
solution was already collapsing (-4.25 pp from 100 to 300, -1.49 pp from 300
to 1000) while runtime grew superlinearly; larger budgets were not measured
to completion and no claim is made about them.

The honest reading: the classic 1964 construction is a STRONG baseline on
this geography — many small same-band drops leave it little to lose — and the
metaheuristic only overtakes it with a real search budget. Full window, all
48 delivery days, at the chosen limit:

| method | total km | vehicle-days | delta |
|---|---:|---:|---:|
| Clarke-Wright savings | 253,201.2 | 937 | baseline |
| OR-Tools CVRP (limit 1000) | 252,713.5 | 924 | **-0.2%** |

Per-day record: CVRP wins 29, ties 0, loses 19 of the 48 days. On the full
window the metaheuristic's edge nearly vanishes (bigger peak-day instances
than the sweep subset), and the routing saving over the classic baseline is
**modest on this geography** — reported as measured, not sold as savings.
Both totals include the identical peeled full-vehicle direct trips, so the
comparison is fair; determinism means these km reproduce exactly on any
machine.

### Stage 5 — cost-to-serve (NO profit claims)

Every rate is INVENTED and labelled; the table is a cost-structure view under
stated assumptions, not a margin statement. Verbatim from the full-run report:

```
item                              GBP      provenance            basis
labour                       3,920.27 GBP  [synthetic-assigned]  270.4 DES hours x 14.50/h (rate INVENTED)
transport                  214,806.49 GBP  [synthetic-assigned]  252,713.5 CVRP km x 0.85/km (km + rate INVENTED)
holding                     14,700.40 GBP  [synthetic-assigned]  91,878 SS units x 0.02/unit-week x 8w
facility                    20,000.00 GBP  [synthetic-assigned]  2,500/week fixed x 8w (INVENTED)
total cost                 253,427.16 GBP  [synthetic-assigned]  sum of the four lines above (identity (l))
window revenue           1,047,042.41 GBP  [real]                real revenue of the same 8-week window (identity (m))
```

Modelled cost equals 24.2% of window revenue. Split of inputs, explicit:
REAL — order composition, timestamps, window revenue; SYNTHETIC-ASSIGNED —
every rate, hour, km and safety-stock unit; nothing in the ledger is stronger
than its weakest input (transport dominates the cost side, and its km stand
entirely on invented coordinates — which is exactly why the table makes no
profit claim).

### Identity checks i-m (full dataset, all PASS — a-h unchanged)

| # | identity | lhs | rhs | result |
|---|---|---:|---:|---|
| i | DES picked lines == window stream lines | 33,312 | 33,312 | **PASS** |
| j | cartons shipped == cartons packed | 70,820 | 70,820 | **PASS** |
| k | routed drops (route structures) == shipped orders | 4,151 | 4,151 | **PASS** |
| l | ledger total == sum of cost lines (to the cent) | 253,427.16 | 253,427.16 | **PASS** |
| m | ledger window revenue == cleaned revenue, same window (to the penny) | 1,047,042.41 | 1,047,042.41 | **PASS** |

Each identity has a deliberate-corruption FAIL path in the tests (a lost pick,
a phantom carton, a route node deleted from a real CVRP route, a one-cent
total drift, a one-penny revenue drift).

## Phase 4 — the product layer (artifact, dashboard, deliverables)

The full report is measured, not cheap: ~51 minutes end to end (48 per-day CVRP
solves at the swept deterministic budget). Phase 4 therefore splits *measuring*
from *presenting*:

**Run artifact** (`chain/artifact.py`). `python -m chain --report --save-artifact`
serializes the whole run — stage headline summaries, the full reconciliation
ledger, all 13 identity results with both sides, the cost-to-serve lines, the
slotting and routing comparisons, provenance tag on every entry — to
`artifacts/full_run.json`: deterministic (sorted keys, no wall-clock, ASCII;
same code + same data -> byte-identical JSON), versioned (`schema_version`),
and stamped with a sha256 **code fingerprint** of every `chain/*.py` source
file. The artifact of the measured full run is committed — it is the
reproducible receipt of every number in this README. If any chain source
changes after the save, every consumer flags the artifact as **STALE** and
says how to regenerate it.

**CHAIN DASHBOARD** (`app.py` + `templates/` + `static/`, Flask, port 5077,
offline assets only — no CDNs, guarded by a test). Boots in seconds from the
artifact, recomputes nothing:

- **stage flow** — the 7 stages left-to-right, each with its headline number and
  provenance tag, color-coded (real = green, derived = blue, synthetic-assigned = amber);
- **reconciliation panel** (the hero): all 13 identities, both numbers side by
  side, tolerance, PASS badges;
- **boundary map** — the real-vs-assigned table above, rendered;
- the **cost-to-serve ledger** with provenance tags, the **slotting comparison**
  (hand-built bars), **CVRP vs Clarke-Wright per delivery day** (hand-built SVG
  line chart), and the **forecast class winners**;
- `/api/health` plus JSON endpoints (`/api/identities`, `/api/ledger`,
  `/api/stages`, ...) reading the same artifact.

**Executive deliverables** (`chain/exports.py`). `python -m chain --deliverables`
builds, from the artifact alone: the **CHAIN REPORT** PDF (cover with the
one-dataset-through-everything story and the boundary statement, the pipeline
diagram in provenance colors, the identities table, the slotting/routing
comparisons, the cost-to-serve + reconciliation ledgers) and the **LEDGER
workbook** (`Stages`, `Identities`, `CostToServe`, `SlottingComparison`,
`Ledger`, `Assumptions` sheets). Metadata timestamps are pinned, so
regenerating from the same artifact is **byte-identical** — asserted by tests.

### Seeing it

No captures are committed (the license is portfolio-review only and the views
regenerate in seconds): run `python app.py` and open `http://127.0.0.1:5077`
for the stage flow + identity board, or `python -m chain --deliverables` and
open `deliverables/chain_report.pdf` for the same views in hand-off form.
Headless proof that the views render lives in the test suite
(`tests/test_app.py` asserts the DOM contains all 13 identity rows with PASS
badges and the three provenance color classes).

## Scenario / shock what-if — the ledger still reconciles under a shock

A reconciliation harness earns its keep the moment an input moves. `scenario.py`
(a chain consumer, alongside `app.py`) perturbs **one** input, re-runs only the
stages that input feeds — **reusing the existing stage engines, never
re-inventing one** — and then re-runs **all 13** cross-stage identities on the
perturbed run. The deltas are reported stage by stage and on the ledger, but the
point is the last line of each: *the ledger still reconciles*. Two labelled
scenarios ship (`python scenario.py`; deterministic, so the deliverables
regenerate byte-for-byte):

| scenario | the ONE input perturbed | where it lands | identities |
|---|---|---|---|
| demand surge ×1.20 on a demand class | forecast demand + uncertainty (**derived**) | inventory buffer + its holding charge | **13/13 hold** |
| cost-rate shock ×1.20 on transport GBP/km | a cost rate (**synthetic-assigned**) | the dominant transport line + the total | **13/13 hold** |

The honest reading is the finding, not the numbers. The **demand surge** is a
*planning-side* shock: it perturbs the derived forecast, so the safety-stock
buffer and its holding charge rise for the surged class (fixture path: holding
467.19 → 534.36 GBP), yet the physical fulfilment of the already-real orders —
picks, cartons, CVRP km, labour hours — is **unchanged by construction** (the
real invoice stream is immutable), so those cost lines and every physical
identity (i, j, k) do not move; the aggregate safety-stock rise (+14.4%) is
*less* than the +20% class surge because the untouched lumpy class dilutes it —
reported as measured. The **cost-rate shock** perturbs a labelled synthetic rate:
the km are unchanged (the rate rose, not the distance), transport and the total
rise together (fixture: total 45,920.63 → 51,004.18 GBP, +11.1%), and additivity
(l) plus the real window revenue (m) still close to the cent. The real
vs synthetic-assigned boundary is exactly the baseline's; these are
cost-structure what-ifs under labelled assumptions, never profit claims.

The committed deliverables ([`deliverables/scenarios.md`](deliverables/scenarios.md)
+ [`deliverables/scenarios.csv`](deliverables/scenarios.csv)) are the deterministic
fixture CI path; `python scenario.py --full` runs the identical tool on the full
UCI dataset. `tests/test_scenario.py` asserts the perturbation is pure and scoped,
that it traces to the ledger exactly, and that all 13 identities hold under every
perturbed run.

## How to run

```bash
pip install -r requirements.txt

# Raw data: either keep my retail-analytics-real repo checked out as a sibling
# directory (chain/paths.py finds its data/raw/ automatically), or:
python scripts/download_data.py

# 1) measure: the full report (slow, ~51 min: 48 CVRPs) + the run artifact
python -m chain --report --save-artifact
#    (or the fast CI path: python -m chain --report --fixture)

# 2) browse: the CHAIN DASHBOARD reads the artifact, recomputes nothing
python app.py                         # http://127.0.0.1:5077 (CHAIN_PORT overrides)

# 3) hand off: executive deliverables from the same artifact
python -m chain --deliverables        # deliverables/chain_report.pdf + chain_ledger.xlsx

# 4) stress it: perturb one input, prove the ledger still reconciles
python scenario.py                    # deliverables/scenarios.md + scenarios.csv (fixture path)
#    (or on the full dataset: python scenario.py --full)

ruff check .   # lint gate
pytest -q      # fixture-based tests; full-data tests skip without raw data
```

The committed `artifacts/full_run.json` already contains the measured full run,
so steps 2-3 work immediately after `pip install` — step 1 is only needed to
re-measure (and the dashboard/deliverables will tell you if the code has
drifted from the committed artifact).

Runtime note: the full-dataset report runs 48 per-day CVRPs at the swept
solution limit (1000) — measured 51 minutes end to end on the reference
machine; the fixture path stays fast. The budget is a solution count, not a
wall clock, so the km (not the minutes) are identical on any machine.

The dataset itself is not redistributed (CC BY 4.0, see [CREDITS.md](CREDITS.md));
`data/` is git-ignored and the committed fixture is a small, seeded sample of real rows.

## Roadmap

- [x] **Phase 2 — inventory + warehouse** (done): base-stock replenishment from
  `DemandForecast` on labelled synthetic lead times; real invoices picked as tours in
  the seeded synthetic warehouse, three slottings compared honestly; identities e-h
  (coverage, pick conservation, same-invoice evaluation, provenance audit).
- [x] **Phase 3 — physical stages + the full ledger** (done): fulfilment DES of the
  real invoice stream through the deployed slotting (representative 8-week peak
  window, stated); FFD carton packing; per-day CVRP at a swept deterministic
  solution limit vs an honest Clarke-Wright baseline on seeded synthetic
  geography; the cost-to-serve ledger with provenance on every line and no
  profit claims; identities i-m close the window loop — the ledger's revenue is
  the cleaned data's revenue, to the penny.
- [x] **Phase 4 — the product layer** (done): the deterministic, committed run
  artifact with code-fingerprint staleness detection; the offline Flask CHAIN
  DASHBOARD (stage flow, the 13-identity reconciliation panel, boundary map,
  ledgers and comparisons); byte-reproducible executive deliverables (CHAIN
  REPORT PDF + LEDGER workbook) built from the artifact, never recomputed.
- [x] **Scenario / shock what-if** (done): `scenario.py` perturbs one input (a
  ×1.20 demand surge on a class, or a ×1.20 transport-rate shock), re-runs the
  stages it feeds by reusing the existing engines, and re-checks all 13
  identities on the perturbed run — proving the ledger still reconciles under a
  shock. Deterministic deliverables in `deliverables/scenarios.{md,csv}`.

## Docs

- [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) — the reconciliation story: why the
  seams, not the silos, are where distributors lose money.
- [deliverables/scenarios.md](deliverables/scenarios.md) — the shock what-if: one
  input perturbed, traced to the ledger, all 13 identities still holding.
- [CREDITS.md](CREDITS.md) — dataset citation, method references, adapted-from notes.

## License

Proprietary — portfolio review only. See [LICENSE](LICENSE).
