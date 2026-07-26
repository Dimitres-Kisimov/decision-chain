"""Stage 4a — fulfilment: a DES of picking + packing the REAL invoice stream.

Real WHEN and WHAT, synthetic HOW FAST. Every invoice in the representative
window arrives at its REAL timestamp and is picked through the phase-2 slotted
warehouse by a small synthetic crew, then packed into shipment cartons.

Which slotting: the DEPLOYED variant — the assignment-optimal slotting that
stage 3 carries in its ``WarehouseWorkload`` contract. Rationale: stage 3
already compared random/abc/optimal honestly and concluded a planner would
deploy the assignment-optimal layout; stage 4 simulates the warehouse a
planner would actually run, and it consumes the contract object, not a
re-derivation.

The event engine is a hand-rolled heapq DES — no SimPy — adapted from my
logistics-digital-twin repo (logitwin/simulate.py): a priority queue of
``(time, seq, kind, payload)`` tuples with ARRIVAL/PICK_DONE events and a
deterministic ``seq`` tie-break. Differences here: ``N_PICKERS`` parallel
pickers instead of one, service times come from the stage-3 pick tours
(TimeMin per real invoice), and orders queue FIFO in arrival order.

Packing is a simple First-Fit-Decreasing carton fill (adapted from my
logistics-digital-twin repo, logitwin/packing.py: ffd_min_bins_1d), using the
labelled synthetic SKU dims/weights from chain/synthetic.py: lines are sorted
by unit volume (descending) and units are placed in bulk into the first open
carton with enough remaining volume AND weight. This is the 1D volume+weight
relaxation of true 3D packing — an idealised carton count, and labelled so.
Units too big/heavy for any carton ship one-per-carton.

Representative window: simulating all ~104 weeks through the CVRP stage
downstream is slow, so stages 4-5-6 all run on the SAME representative
window — the contiguous ``WINDOW_WEEKS``-week span with the most invoice
lines (which by construction contains the peak picking week). The report
states this; nothing about the window is hidden.

Provenance: SYNTHETIC_ASSIGNED — arrivals and order composition are real,
but every duration stands on the invented geometry/speed/crew and every
carton on invented dims. Weakest input wins.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import pandas as pd

from chain.contracts import FulfilmentLog, InvoiceStream, Provenance, WarehouseWorkload
from chain.synthetic import SyntheticLayers

WINDOW_WEEKS = 8          # representative simulated window (contains the peak)
N_PICKERS = 4             # synthetic-assigned crew size
CARTON_L_CM = 60.0        # synthetic-assigned shipment carton (internal dims)
CARTON_W_CM = 40.0
CARTON_H_CM = 40.0
CARTON_VOLUME_CM3 = CARTON_L_CM * CARTON_W_CM * CARTON_H_CM
CARTON_MAX_KG = 20.0


# --------------------------------------------------------------------------- #
# Representative window selection (deterministic)
# --------------------------------------------------------------------------- #
def select_window(stream: InvoiceStream, weeks: pd.DatetimeIndex, n_weeks: int = WINDOW_WEEKS) -> pd.DatetimeIndex:
    """The contiguous ``n_weeks`` span of ``weeks`` with the most invoice lines.

    Ties break on the EARLIEST window. By construction the busiest contiguous
    span contains the peak picking week; the report prints the chosen dates.
    """
    if len(weeks) <= n_weeks:
        return weeks
    lines_per_week = stream.lines.groupby("Week", observed=True).size()
    counts = lines_per_week.reindex(weeks, fill_value=0).to_numpy()
    best_start, best_total = 0, -1
    running = int(counts[:n_weeks].sum())
    for start in range(len(weeks) - n_weeks + 1):
        if start > 0:
            running += int(counts[start + n_weeks - 1]) - int(counts[start - 1])
        if running > best_total:
            best_total, best_start = running, start
    return weeks[best_start : best_start + n_weeks]


def window_lines(stream: InvoiceStream, window: pd.DatetimeIndex) -> pd.DataFrame:
    """The REAL invoice lines whose week falls inside the window."""
    return stream.lines.loc[stream.lines["Week"].isin(window)].copy()


# --------------------------------------------------------------------------- #
# FFD carton packing (adapted from logitwin/packing.py: ffd_min_bins_1d)
# --------------------------------------------------------------------------- #
def pack_invoice(items: list[tuple[float, float, int]]) -> int:
    """Carton count for one invoice: FFD by unit volume, bulk placement.

    ``items`` are (unit_volume_cm3, unit_weight_kg, quantity) per line.
    First-Fit-Decreasing adapted from my logistics-digital-twin repo
    (logitwin/packing.py, ffd_min_bins_1d), extended with a weight cap and
    bulk placement: identical units of a line fill each candidate carton in
    one step, so wholesale quantities stay O(lines x cartons). Oversize units
    (> carton volume or > carton weight) ship one-per-carton.
    """
    cartons: list[list[float]] = []  # [remaining_volume, remaining_weight]
    oversize = 0
    for vol, wt, qty in sorted(items, key=lambda t: (-t[0], -t[1])):
        if qty <= 0:
            continue
        if vol > CARTON_VOLUME_CM3 or wt > CARTON_MAX_KG:
            oversize += qty
            continue
        remaining = qty
        for carton in cartons:
            if remaining == 0:
                break
            fit = min(
                remaining,
                int(carton[0] / vol + 1e-9),
                int(carton[1] / wt + 1e-9) if wt > 0 else remaining,
            )
            if fit >= 1:
                carton[0] -= fit * vol
                carton[1] -= fit * wt
                remaining -= fit
        while remaining > 0:
            fit = min(
                remaining,
                int(CARTON_VOLUME_CM3 / vol + 1e-9),
                int(CARTON_MAX_KG / wt + 1e-9) if wt > 0 else remaining,
            )
            carton = [CARTON_VOLUME_CM3 - fit * vol, CARTON_MAX_KG - fit * wt]
            cartons.append(carton)
            remaining -= fit
    return len(cartons) + oversize


# --------------------------------------------------------------------------- #
# The DES (heapq pattern adapted from logitwin/simulate.py)
# --------------------------------------------------------------------------- #
@dataclass
class _Order:
    invoice: str
    arrival: pd.Timestamp
    pick_minutes: float
    lines: int
    units: float
    cartons: int
    weight_kg: float


def _build_orders(
    lines: pd.DataFrame,
    workload: WarehouseWorkload,
    layers: SyntheticLayers,
) -> list[_Order]:
    """One order per REAL window invoice, with stage-3 pick time and FFD cartons."""
    pick = workload.pick_lists.set_index("Invoice")
    phys = layers.sku_physical.set_index("StockCode")
    unit_vol = (phys["LengthCm"] * phys["WidthCm"] * phys["HeightCm"]).to_dict()
    unit_wt = phys["WeightKg"].to_dict()

    orders: list[_Order] = []
    for invoice, group in lines.groupby("Invoice", observed=True, sort=True):
        items = [
            (float(unit_vol[sku]), float(unit_wt[sku]), int(qty))
            for sku, qty in zip(group["StockCode"], group["Quantity"], strict=True)
        ]
        row = pick.loc[invoice]
        orders.append(
            _Order(
                invoice=str(invoice),
                arrival=group["InvoiceDate"].min(),
                pick_minutes=float(row["TimeMin"]),
                lines=int(row["Lines"]),
                units=float(row["Units"]),
                cartons=pack_invoice(items),
                weight_kg=float(sum(w * q for _, w, q in items)),
            )
        )
    orders.sort(key=lambda o: (o.arrival, o.invoice))  # deterministic FIFO
    return orders


def simulate(
    stream: InvoiceStream,
    workload: WarehouseWorkload,
    layers: SyntheticLayers,
    weeks: pd.DatetimeIndex,
    n_weeks: int = WINDOW_WEEKS,
    n_pickers: int = N_PICKERS,
) -> FulfilmentLog:
    """DES of the representative window: real arrivals, synthetic crew. Deterministic."""
    window = select_window(stream, weeks, n_weeks)
    lines = window_lines(stream, window)
    orders = _build_orders(lines, workload, layers)

    t0 = orders[0].arrival if orders else pd.Timestamp(0)

    def clock(ts: pd.Timestamp) -> float:
        return float((ts - t0).total_seconds())

    # Event queue: (time_s, seq, kind, payload); kind 0 = ARRIVAL, 1 = PICK_DONE.
    # (heapq event-loop pattern adapted from logitwin/simulate.py: simulate)
    pq: list[tuple[float, int, int, object]] = []
    seq = 0
    for order in orders:
        heapq.heappush(pq, (clock(order.arrival), seq, 0, order))
        seq += 1

    backlog: list[_Order] = []
    idle = n_pickers
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    def dispatch(now: float) -> None:
        nonlocal idle, seq
        while idle > 0 and backlog:
            order = backlog.pop(0)
            idle -= 1
            started[order.invoice] = now
            heapq.heappush(pq, (now + order.pick_minutes * 60.0, seq, 1, order))
            seq += 1

    while pq:
        now, _, kind, payload = heapq.heappop(pq)
        order = payload  # type: ignore[assignment]
        if kind == 0:  # ARRIVAL
            backlog.append(order)  # type: ignore[arg-type]
            dispatch(now)
        else:  # PICK_DONE
            finished[order.invoice] = now  # type: ignore[union-attr]
            idle += 1
            dispatch(now)

    rows = []
    for order in orders:
        start_s = started[order.invoice]
        finish_s = finished[order.invoice]
        finish_ts = t0 + pd.Timedelta(seconds=finish_s)
        rows.append(
            {
                "Invoice": order.invoice,
                "Arrival": order.arrival,
                "StartMin": start_s / 60.0,
                "FinishMin": finish_s / 60.0,
                "WaitMin": (start_s - clock(order.arrival)) / 60.0,
                "PickMinutes": order.pick_minutes,
                "Lines": order.lines,
                "Units": order.units,
                "Cartons": order.cartons,
                "WeightKg": order.weight_kg,
                "Day": finish_ts.normalize(),
            }
        )
    orders_df = pd.DataFrame(
        rows,
        columns=[
            "Invoice", "Arrival", "StartMin", "FinishMin", "WaitMin",
            "PickMinutes", "Lines", "Units", "Cartons", "WeightKg", "Day",
        ],
    )

    if len(orders_df):
        grouped = orders_df.groupby("Day", observed=True)
        per_day = pd.DataFrame(
            {
                "Orders": grouped["Invoice"].nunique(),
                "Lines": grouped["Lines"].sum(),
                "Units": grouped["Units"].sum(),
                "LabourHours": grouped["PickMinutes"].sum() / 60.0,
                "Cartons": grouped["Cartons"].sum(),
                "WeightKg": grouped["WeightKg"].sum(),
            }
        ).reset_index()
    else:
        per_day = pd.DataFrame(
            columns=["Day", "Orders", "Lines", "Units", "LabourHours", "Cartons", "WeightKg"]
        )

    return FulfilmentLog(
        orders=orders_df,
        per_day=per_day,
        window_weeks=window,
        assumptions={
            "slotting": "deployed stage-3 variant (assignment-optimal) via WarehouseWorkload",
            "n_pickers": n_pickers,
            "pick_times": "stage-3 pick tours (TimeMin), synthetic geometry/speed",
            "carton_cm": (CARTON_L_CM, CARTON_W_CM, CARTON_H_CM),
            "carton_max_kg": CARTON_MAX_KG,
            "packing": "FFD 1D volume+weight relaxation (adapted from logitwin/packing.py)",
            "window_rule": f"contiguous {len(window)}-week span with most invoice lines",
            "arrivals": f"{Provenance.REAL.value} (invoice timestamps)",
            "durations": f"{Provenance.SYNTHETIC_ASSIGNED.value} (invented crew/geometry)",
        },
        provenance=Provenance.combine(stream.provenance, workload.provenance, layers.provenance),
    )
