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
| Customer geography (within real countries) | **SYNTHETIC-ASSIGNED** (seeded, labelled) | phase 3+ |
| Cost rates (pick, km, holding) | **SYNTHETIC-ASSIGNED** (seeded, labelled) | phase 3/4+ |

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
| 4 transport | `WarehouseWorkload` | `TransportPlan` | 3 |
| 5 costing | all upstream | `CostToServe` | 3/4 |
| 6 reconcile | ledger entries from 0-5 | identity checks (PASS/FAIL, both numbers) | **1-2 (a-h done)** |

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

## How to run

```bash
pip install -r requirements.txt

# Raw data: either keep my retail-analytics-real repo checked out as a sibling
# directory (chain/paths.py finds its data/raw/ automatically), or:
python scripts/download_data.py

python -m chain --report              # full dataset: stages 0-3 + reconciliation
python -m chain --report --fixture    # committed real-row fixture (CI path, no download)

ruff check .   # lint gate
pytest -q      # fixture-based tests; full-data tests skip without raw data
```

The dataset itself is not redistributed (CC BY 4.0, see [CREDITS.md](CREDITS.md));
`data/` is git-ignored and the committed fixture is a small, seeded sample of real rows.

## Roadmap

- [x] **Phase 2 — inventory + warehouse** (done): base-stock replenishment from
  `DemandForecast` on labelled synthetic lead times; real invoices picked as tours in
  the seeded synthetic warehouse, three slottings compared honestly; identities e-h
  (coverage, pick conservation, same-invoice evaluation, provenance audit).
- [ ] **Phase 3 — transport + geography**: shipment consolidation and routing on real
  destination countries with seeded coordinates; identities: shipped == picked, every
  shipment maps to a real invoice.
- [ ] **Phase 4 — costing + the closing of the loop**: cost-to-serve per SKU/invoice on
  labelled synthetic rates; the final identity closes the chain — end-of-chain revenue
  equals the stage-0 revenue, to the penny.

## Docs

- [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) — the reconciliation story: why the
  seams, not the silos, are where distributors lose money.
- [CREDITS.md](CREDITS.md) — dataset citation, method references, adapted-from notes.

## License

Proprietary — portfolio review only. See [LICENSE](LICENSE).
