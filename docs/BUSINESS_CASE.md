# Business case: why a distributor needs a reconciled decision chain

## The problem nobody owns

A mid-size distributor runs its business on a chain of decisions: what demand
to expect, what to reorder, how to staff the warehouse, how to load the trucks,
and what each order actually costs to serve. In almost every real company these
decisions live in **silos** — a forecasting spreadsheet here, an inventory tool
there, a WMS, a TMS, a controlling workbook — each maintained by different
people, each internally consistent, and each quietly using a *different version
of the same numbers*.

The failure mode is never inside a silo. It is at the seams:

- The forecast is in units; the inventory plan was built on cases. Nobody
  notices until safety stock is 12x too high for the A-articles.
- The warehouse plans labor on order counts from the OMS; controlling allocates
  handling cost on invoice counts from the ERP. The two counts differ by the
  cancelled orders, so the cost-to-serve figures are fiction at exactly the
  margin-relevant decimal.
- Demand history was "cleaned" twice by two teams with two different rules for
  returns. The forecast and the replenishment plan literally disagree about
  what was sold.

None of this is a modeling problem. It is a **reconciliation** problem: no
machine ever checks that the number leaving one stage is the number entering
the next. This repo's claim: the reconciliation harness is not project
scaffolding — **it is the product.**

## What this project does about it

decision-chain runs ONE real dataset — the UCI Online Retail II transactions of
a UK giftware distributor, ~1M rows over two years — through the entire chain,
with a machine-checked ledger at the seams:

1. Every stage registers the numbers it stands on into a shared **Ledger**
   (with unit and provenance).
2. **Identity checks** — pytest-able assertions that print both sides —
   verify conservation across stage boundaries: units aggregated for the
   forecast equal units on the invoice lines; every invoice line that
   controlling will cost is a line the warehouse actually picked; revenue at
   the end of the chain equals revenue at the start.
3. **Provenance discipline**: every quantity is tagged `real`,
   `synthetic-assigned`, or `derived`, a derived quantity inherits the weakest
   provenance of its inputs, and reports must print the tag. You can always
   see which numbers are data and which are assumptions.

The strongest of the Phase-1 checks is deliberately *cross-repository*:
identity (a) asserts that this repo's cleaning pipeline reproduces the revenue
figure published by my retail-analytics-real repo — **GBP 19,643,861.62, to
the penny**. Two codebases, one raw file, one number. That is exactly the
property the two-teams-two-cleaning-rules failure above lacks, demonstrated
positively.

## What the reconciliation buys, concretely

| Seam | Identity | Business failure it catches |
|---|---|---|
| repo boundary | cleaned revenue == published figure (to the penny) | two teams "cleaning" the same data differently |
| ingest -> forecast | weekly demand sum == invoice line sum | forecasting a series that drifted from the transactions |
| forecast -> inventory (P2, done) | replenishment covers every forecasted SKU, both directions | plans for phantom articles / articles nobody plans |
| ingest -> warehouse (P2, done) | lines picked across all tours == invoice-stream lines (256,787 == 256,787) | labor planned on a different order count than billed |
| warehouse evaluation (P2, done) | all slotting variants measured on the identical invoice set | comparing layouts on cherry-picked order samples |
| provenance (P2, done) | travel numbers labelled synthetic-assigned, demand/velocity labelled real | invented geometry quietly presented as data |
| warehouse -> fulfilment (P3, done) | DES picked lines == pick-list lines for the simulated window | simulating throughput on orders nobody actually placed |
| fulfilment -> transport (P3, done) | routed drops (from the route structures) == shipped orders | trucks planned for a different order count than the warehouse shipped |
| costing internal (P3, done) | ledger total == sum of its cost lines, to the cent | a summary total that quietly drifts from its own detail |
| chain -> costing (P3, done) | ledger window revenue == cleaned-data revenue, same window, to the penny | cost-to-serve allocated over the wrong revenue base |

Phases 1-3 implement all of these; identities a-m (13 checks) all PASS on the
full dataset. Every new stage arrived with its conservation identities, or it
did not merge.

Phase 2 also puts a first number on two classic seam decisions, with the
boundary declared: slotting the warehouse by *real* pick velocity instead of
randomly cuts mean pick travel per invoice by 14.5% (183.2 vs 214.3 m over
33,492 real invoices; exact linear-assignment slotting adds another 1.6%) —
inside an *invented*, labelled geometry, so the % is the claim, not the
metres. And the safety-stock table prices forecast quality directly: the
erratic class carries 3.02 weeks of demand as buffer and lumpy 2.30 vs 1.70
for smooth — the measured cost of unforecastability at a 95% service target.

Phase 3 closes the physical loop on a representative 8-week peak window
(stated, not hidden): the real invoice stream is picked through the deployed
slotting by a discrete-event simulation, packed into cartons, routed to
seeded synthetic customer coordinates, and costed — labour, transport,
holding, facility — against the real revenue of the same window, with the
split between real and invented inputs printed on every ledger line. The
deliverable is the *shape* of cost-to-serve under labelled assumptions, and
deliberately NOT a margin claim: every rate is invented, and the ledger says
so line by line.

Phase 3 also delivers a negative result worth having: on the seeded
geography, OR-Tools' guided local search beats the 1964 Clarke-Wright
construction by only 0.2% of total km over the 48 routed delivery days (and
loses 19 of them) — measured at a deterministic, swept solution budget. A
routing pitch promising double-digit savings here would be selling the
geography, not the optimizer; the honest table says so.

## The hand-off: from a 51-minute run to a receipt anyone can open

A reconciliation harness that only a developer can run is a demo, not a
product. Phase 4 closes that gap. The measured full run — 51 minutes, 48
per-day CVRP solves — is serialized once into a **committed run artifact**
(`artifacts/full_run.json`): every stage's headline numbers, the full ledger,
all 13 identity results with both sides printed, provenance on every entry,
and a code fingerprint that makes the artifact self-aware of staleness. From
that one receipt:

- the **CHAIN DASHBOARD** gives an auditor the identity board — 13 PASS
  badges with the two numbers behind each one — plus the stage flow with its
  real/derived/synthetic color code and the boundary map, in a browser,
  offline, in seconds;
- the **CHAIN REPORT** (PDF) and the **LEDGER workbook** (Excel) give an
  executive the same numbers in hand-off form — and regenerate byte-for-byte
  from the same artifact, so "which version of the deck is this?" has a
  machine answer.

Nothing in the product layer recomputes anything: dashboard, PDF and workbook
all quote the one measured run. That is the same discipline the identities
enforce inside the chain, applied to the reporting seam — the last seam,
where most decision chains quietly fork into slideware.

## Honesty as a design constraint

The chain only proves something if the boundary between data and invention is
never blurred. Demand, invoice composition, seasonality, returns: **real**.
SKU dimensions, warehouse geometry, geography, cost rates: **synthetic,
assigned, seeded, and labelled** (phases 2+). Model outputs: **derived**, and
reported with measured out-of-sample error — including when the sophisticated
model loses to a naive baseline, which on real weekly retail demand it
regularly does.

A decision chain that fabricates its inputs, or hides which model actually won,
reconciles nothing.
