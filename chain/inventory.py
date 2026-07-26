"""Stage 2 — inventory: DemandForecast + synthetic lead times -> ReplenishmentPlan.

Policy: periodic-review (weekly) order-up-to / base-stock at a target cycle
service level. The order-up-to level is the newsvendor / service-level
quantile of demand over the protection interval P = lead time + review period
under a normal approximation (adapted from my revops-optimizer repo,
revops/optimize/inventory.py: the critical-fractile quantile logic), and
safety stock follows the square-root law (adapted from my supply-network-opt
repo, supplynet/safetystock.py):

    z   = Phi^-1(service_level)
    SS  = z * sqrt(P) * sigma          (sigma = weekly forecast-error std)
    S   = mu * P + SS                  (order-up-to)
    ROP = mu * L + z * sqrt(L) * sigma (reorder point, continuous-review view)

with mu = mean weekly point forecast over the stage-1 horizon and sigma the
std of the winning model's pooled rolling-origin CV errors — i.e. the
uncertainty is MEASURED out-of-sample, not assumed.

Provenance: Provenance.derive(DERIVED forecast, SYNTHETIC_ASSIGNED lead times)
= SYNTHETIC_ASSIGNED. The demand signal is real-derived, but the plan stands
on invented lead times and says so on every output.

Honesty note (the cost of unforecastability): the lumpy class is where stage 1
measured that NOTHING beats the naive walk (MASE > 1) — so sigma is wide by
measurement, and the square-root law converts that directly into safety stock.
``class_summary`` quantifies it: safety-stock weeks (SS / weekly demand) per
class, so the report can show how many extra weeks of buffer lumpy demand
costs relative to the smooth class.

Assumptions carried in the plan (all labelled): initial on-hand is zero at the
plan start (OrderQty is the initial fill to S), demand over the protection
interval is normal, lead times are the synthetic layer's.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pandas as pd

from chain.contracts import DemandForecast, Provenance, ReplenishmentPlan
from chain.synthetic import SyntheticLayers

SERVICE_LEVEL = 0.95
REVIEW_WEEKS = 1  # weekly review cycle, same clock as the demand series


def z_score(service_level: float) -> float:
    """Standard-normal quantile (adapted from my supply-network-opt repo)."""
    if not 0.0 < service_level < 1.0:
        raise ValueError("service_level must be strictly between 0 and 1")
    return float(NormalDist().inv_cdf(service_level))


def safety_stock(sigma: float, protection_weeks: float, service_level: float) -> float:
    """Square-root-law safety stock (adapted from my supply-network-opt repo,
    supplynet/safetystock.py: base_stock_safety)."""
    return z_score(service_level) * math.sqrt(protection_weeks) * sigma


def order_up_to(mu: float, sigma: float, protection_weeks: float, service_level: float) -> float:
    """Service-level quantile of protection-interval demand, normal approx
    (the newsvendor logic adapted from my revops-optimizer repo)."""
    return mu * protection_weeks + safety_stock(sigma, protection_weeks, service_level)


def run(
    forecast: DemandForecast,
    layers: SyntheticLayers,
    service_level: float = SERVICE_LEVEL,
) -> ReplenishmentPlan:
    """Per-SKU replenishment plan for every forecasted SKU (identity (e))."""
    z = z_score(service_level)
    fc = forecast.forecasts
    rows = []
    for sku in sorted(fc["StockCode"].unique()):
        sku_fc = fc.loc[fc["StockCode"] == sku]
        mu = float(sku_fc["Units"].mean())  # units/week over the horizon
        sigma = float(sku_fc["Sigma"].iloc[0])
        lead = layers.lead_time_weeks(sku)
        protection = lead + REVIEW_WEEKS
        ss = safety_stock(sigma, protection, service_level)
        level = mu * protection + ss
        rop = mu * lead + z * math.sqrt(lead) * sigma
        rows.append(
            {
                "StockCode": sku,
                "Class": str(sku_fc["Class"].iloc[0]),
                "Model": str(sku_fc["Model"].iloc[0]),
                "MuWeekly": mu,
                "Sigma": sigma,
                "LeadTimeWeeks": lead,
                "SafetyStock": ss,
                "ReorderPoint": rop,
                "OrderUpTo": level,
                # Initial fill from the documented zero on-hand start.
                "OrderQty": level,
                "OrderWeek": sku_fc["Week"].min(),
                # Long-run expected on-hand: half a review cycle + the buffer.
                "ExpectedStock": mu * REVIEW_WEEKS / 2.0 + ss,
            }
        )
    plan = pd.DataFrame(
        rows,
        columns=[
            "StockCode", "Class", "Model", "MuWeekly", "Sigma", "LeadTimeWeeks",
            "SafetyStock", "ReorderPoint", "OrderUpTo", "OrderQty", "OrderWeek",
            "ExpectedStock",
        ],
    )
    provenance = Provenance.derive(forecast.provenance, layers.provenance)
    return ReplenishmentPlan(
        plan=plan,
        assumptions={
            "service_level": service_level,
            "z": z,
            "review_weeks": REVIEW_WEEKS,
            "initial_on_hand": 0,
            "demand_model": "normal over protection interval, sigma from rolling-origin CV",
            "lead_times": f"{Provenance.SYNTHETIC_ASSIGNED.value} (chain/synthetic.py, seed {layers.seed})",
            "seed": layers.seed,
        },
        provenance=provenance,
    )


def class_summary(plan: ReplenishmentPlan) -> pd.DataFrame:
    """Per demand class: how much buffer the forecast quality costs.

    SafetyStockWeeks = total SS / total weekly demand — the number of weeks of
    demand held purely as uncertainty buffer. The lumpy row IS the honesty
    note: naive forecast -> wide measured sigma -> big buffer.
    """
    df = plan.plan
    grouped = df.groupby("Class", observed=True)
    out = pd.DataFrame(
        {
            "SKUs": grouped["StockCode"].nunique(),
            "MuWeekly": grouped["MuWeekly"].sum(),
            "SafetyStock": grouped["SafetyStock"].sum(),
            "OrderUpTo": grouped["OrderUpTo"].sum(),
            "MeanLeadWeeks": grouped["LeadTimeWeeks"].mean(),
            # Pooled, not per-SKU averaged: lumpy naive forecasts can be ~0,
            # and a per-SKU ratio would explode instead of informing.
            "SigmaOverMu": grouped["Sigma"].sum() / grouped["MuWeekly"].sum().clip(lower=1e-9),
        }
    )
    out["SafetyStockWeeks"] = out["SafetyStock"] / out["MuWeekly"].clip(lower=1e-9)
    return out.reset_index()
