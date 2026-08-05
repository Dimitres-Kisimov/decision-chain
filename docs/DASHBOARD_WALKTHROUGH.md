# Dashboard walkthrough — reading the reconciliation story

This is a plain-language tour of the clickable dashboard,
[`web/chain_dashboard.html`](../web/chain_dashboard.html), written for a reader who has
never seen the code. It explains what each panel is showing and — just as important —
what is a **measured fact** versus a **labelled assumption**. No supply-chain background
is assumed.

## What the dashboard is, and how to open it

`web/chain_dashboard.html` is a **single, self-contained HTML file**. There is no server
to start, no build step, no internet connection required: every byte of styling and
script is baked into that one file. To open it, just double-click
`web/chain_dashboard.html` (or drag it into any web browser). It renders instantly, works
offline, and never phones home.

The page does not compute anything. It only *reads* and *displays* two committed
receipts:

* `artifacts/full_run.json` — the frozen record of one real, end-to-end run of the whole
  chain (a run that took about 51 minutes to measure). Every headline number on the page
  comes from here.
* `web/scenario_fixture.json` — a frozen record of the two "what-if" stress tests (the
  bottom panel), measured on a small, fast sample of the same real data.

Because the numbers are read from those receipts rather than recomputed, the page you see
is exactly the run that was measured — nothing is live, nothing can quietly change.

### The one thing to know before you start: the three colours

Every number on the dashboard carries a colour-coded **provenance** chip that says where
it came from. This is the honesty spine of the whole project, so it is worth thirty
seconds:

* **green — real.** Observed directly in the public UCI *Online Retail II* dataset (two
  years of a UK giftware distributor's actual transactions and invoices). Real, and
  attributed under CC BY 4.0.
* **blue — derived.** Computed *from* the real data (for example, a demand forecast).
  Honest, but a calculation, not a raw observation.
* **amber — synthetic-assigned.** **Invented** and clearly labelled: a warehouse layout,
  delivery coordinates, staffing levels, and every cost rate (pounds per km, per hour,
  and so on). These are *modelled, not measured.* They are seeded so they reproduce
  exactly, but they are not claims about the real business.

The chip text is always shown — the colour is never the only signal — so the boundary
between fact and assumption is legible even in black and white.

---

## 1. Stage flow — the whole chain on one line

The top panel lays the seven stages left to right, the way work actually flows through a
distributor: **ingest → forecast → inventory → warehouse → fulfilment → transport →
costing.** Each stage is a card with one headline number and its provenance colour, so
you can see the story change colour as it moves from real fact to modelled operation:

* **0 · ingest** — cleaned revenue of **GBP 19,643,861.62** (green: real).
* **1 · forecast** — **185** SKUs forecast (blue: derived from the real demand).
* **2 · inventory** — **91,878** units of safety stock (amber: it stands on invented
  supplier lead times).
* **3 · warehouse** — mean pick travel of **180.25 m** per invoice (amber: invented
  layout).
* **4 · fulfilment** — **4,151** orders shipped in the window (amber).
* **5 · transport** — **252,714** route-km (amber: invented delivery geography).
* **6 · costing** — a modelled cost of about **GBP 253,427** for the window (amber:
  invented rates).

Underneath, a caption states the scale of the raw input — **1,067,371** raw rows, **200**
tracked SKUs, **104** weeks (2009-12-13 to 2011-12-04) — and, honestly, that the physical
stages (4–6) run on one representative **8-week window** (2010-10-24 to 2010-12-12, 48
working days), not the whole two years, because simulating every day through the routing
solver would take far longer. Every window number on the page says so.

The takeaway from this panel: the chain **starts green and turns amber.** The early
stages are real data; the operational stages are an honest, labelled model built on top
of it.

## 2. The 13 identities — the reconciliation panel (the hero)

This is the centre of the whole project, outlined in green because it is the point. A
**reconciliation identity** is simply a promise that two numbers, computed by two
*independent* parts of the code, must agree. If they ever disagree, a stage has silently
drifted from the one before it — exactly the kind of seam where real distributors lose
money (the forecast quietly using different numbers than the invoices; controlling
costing over a different order count than the warehouse actually picked).

The table lists **all 13** identities (labelled **a** through **m**), and for each one it
shows **both** numbers side by side, the tolerance allowed, the unit, and a **PASS**
badge. Nothing is rounded away or hidden. On this run, **all 13 PASS**. A few worth
pointing at:

* **(a) cross-repo revenue** — `19,643,861.62` = `19,643,861.62` GBP. The same cleaning
  pipeline, run in a *different* repository on the same raw file, reproduces the revenue
  figure **to the penny**. Two codebases, one number.
* **(l) ledger additivity** — `253,427.16` = `253,427.16` GBP. The reported total cost
  equals the sum of its four cost lines, **to the cent**.
* **(m) window revenue** — `1,047,042.41` = `1,047,042.41` GBP. The revenue the cost
  ledger books for the window equals the revenue the cleaning stage independently measured
  for the same window, **to the penny** — the loop closes back onto real money.

The rest (b–k) conserve units, invoice lines, forecast coverage, picks, cartons, and
delivery drops across the seams. Read this panel as: **no euro, unit, or parcel appears or
vanishes as the data crosses from one stage to the next.**

## 3. The real-vs-synthetic boundary map

This panel is the honesty statement rendered as a table. It lists the ten quantities the
chain uses and states, for each, whether it is **real**, **derived**, or
**synthetic-assigned**, with a one-line note on how it was obtained. A tally at the top
counts them: **3 real, 1 derived, 6 synthetic-assigned.**

Reading down the table, the line between fact and assumption is explicit:

* **Real (green):** the transactions and invoice composition, the weekly demand, and the
  seasonality and returns — all observed in UCI *Online Retail II*.
* **Derived (blue):** the forecasts and their uncertainty — computed from that real
  demand.
* **Synthetic-assigned (amber):** SKU dimensions and weights, supplier lead times, the
  warehouse geometry and slotting, the customer geography, the picker crew / cartons /
  vehicles, and every cost rate. All invented, seeded (42), and labelled.

The point of showing this so bluntly: the operational and financial figures are only as
strong as their weakest input, and this table tells you exactly where that weakness is.
The model never dresses an invented number up as a measured one. (Between this panel and
the what-if below, the page also shows the cost-to-serve ledger and a slotting comparison;
both are labelled the same way and make no profit claim.)

## 4. The scenario what-if — shock one input, watch the ledger still reconcile

A reconciliation harness earns its keep the moment an input moves. This bottom panel has
two tabs, each a small stress test that perturbs **one** input, re-runs only the stages
that input feeds (reusing the same engines, never a special-case copy), and then re-checks
**all 13** identities on the shocked run. Each tab shows before/after bars and a
stage-by-stage delta table, and the badge you are meant to watch reads
**"13/13 identities still reconcile."**

**Important honesty note on these numbers.** The two what-ifs are measured on the fast
**fixture path** — a small, seeded sample of real rows — *not* the full 51-minute run, so
their magnitudes are much smaller than the stage-flow figures above (for example, the
fixture window's real revenue is `14,545.03` GBP, versus `1,047,042.41` in the full run).
The label on the panel says so. What carries over from the fixture to the full run is the
**mechanism**, not the size of the numbers.

* **Demand surge ×1.20 on a demand class** (a *planning-side* shock to a **derived**
  input). The forecast for the erratic class rises `3,508.10 → 4,209.72` units (+20.00%),
  which lifts the safety stock `2,919.91 → 3,339.72` units (+14.38%) and its holding
  charge `467.19 → 534.36` GBP (+14.38%). But the physical fulfilment of the
  already-real orders — the picks, cartons, delivery km, and labour hours — **does not
  move at all**, because those orders already happened and are immutable. So the total
  cost barely nudges (`45,920.63 → 45,987.80`, +0.15%), and all 13 identities still hold.
* **Cost-rate shock ×1.20 on the transport rate** (a shock to a **synthetic-assigned**
  input: 0.85 → 1.02 GBP/km). Here the distance does not change — the CVRP route-km stay
  flat at `29,903.24` — only the *rate* rises, so the transport cost line moves
  `25,417.76 → 30,501.31` GBP (+20.00%) and the total follows `45,920.63 → 51,004.18`
  (+11.07%). Additivity (l) and the real window revenue (m) still close to the penny.

Neither scenario is a profit statement, and neither blurs the real-vs-synthetic boundary —
it stays exactly where the baseline put it. They are cost-*structure* what-ifs under
labelled assumptions.

---

## What to take away

* The dashboard is one **offline HTML file** — open it in a browser, no setup.
* It reads a **committed receipt** of one real, end-to-end run; it computes nothing, so
  what you see is exactly what was measured.
* The early chain is **real data** (UCI *Online Retail II*, CC BY 4.0); the operational
  and cost layers are **labelled synthetic** — *modelled, not measured* — and the boundary
  is stated on every number.
* The heart of it is the **13-identity reconciliation panel**: two independent codepaths
  agree on every seam, all 13 PASS, and they keep passing even when an input is shocked.

For the full method, the measured results, and the honest findings, see the repository
[`README.md`](../README.md).
