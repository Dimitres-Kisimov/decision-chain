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
| ingest -> warehouse (P2) | pick-list lines == cleaned sales lines | labor planned on a different order count than billed |
| forecast -> inventory (P2) | every forecast SKU exists in demand | plans for phantom articles |
| chain -> costing (P3+) | end-of-chain revenue == start-of-chain revenue | cost-to-serve allocated over the wrong base |

Phase 1 implements the first four (the warehouse seam in its P1 form: the
invoice stream that becomes the pick lists). Later phases extend the same
ledger through inventory, warehouse, transport, and costing — every new stage
arrives with its conservation identities, or it does not merge.

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
