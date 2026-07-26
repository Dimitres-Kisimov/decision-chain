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
| SKU dimensions / weights | **SYNTHETIC-ASSIGNED** (seeded, labelled) | phase 2+ |
| Warehouse geometry, slotting | **SYNTHETIC-ASSIGNED** (seeded, labelled) | phase 2/3+ |
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
| 2 inventory | `DemandForecast` | `ReplenishmentPlan` | 2 |
| 3 warehouse | `InvoiceStream` + plan | `WarehouseWorkload` (pick lists) | 2/3 |
| 4 transport | `WarehouseWorkload` | `TransportPlan` | 3 |
| 5 costing | all upstream | `CostToServe` | 3/4 |
| 6 reconcile | ledger entries from 0-5 | identity checks (PASS/FAIL, both numbers) | **1 (harness done)** |

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

## How to run

```bash
pip install -r requirements.txt

# Raw data: either keep my retail-analytics-real repo checked out as a sibling
# directory (chain/paths.py finds its data/raw/ automatically), or:
python scripts/download_data.py

python -m chain --report              # full dataset: stages 0-1 + reconciliation
python -m chain --report --fixture    # committed real-row fixture (CI path, no download)

ruff check .   # lint gate
pytest -q      # fixture-based tests; full-data tests skip without raw data
```

The dataset itself is not redistributed (CC BY 4.0, see [CREDITS.md](CREDITS.md));
`data/` is git-ignored and the committed fixture is a small, seeded sample of real rows.

## Roadmap

- **Phase 2 — inventory + warehouse skeleton**: reorder points and safety stock from
  `DemandForecast` (synthetic-assigned lead times, labelled); invoice stream becomes pick
  lists in a seeded synthetic warehouse; new identities: picked units == invoiced units
  per SKU, plan covers every forecast SKU.
- **Phase 3 — transport + geography**: shipment consolidation and routing on real
  destination countries with seeded coordinates; identities: shipped == picked, every
  shipment maps to a real invoice.
- **Phase 4 — costing + the closing of the loop**: cost-to-serve per SKU/invoice on
  labelled synthetic rates; the final identity closes the chain — end-of-chain revenue
  equals the stage-0 revenue, to the penny.

## Docs

- [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) — the reconciliation story: why the
  seams, not the silos, are where distributors lose money.
- [CREDITS.md](CREDITS.md) — dataset citation, method references, adapted-from notes.

## License

Proprietary — portfolio review only. See [LICENSE](LICENSE).
