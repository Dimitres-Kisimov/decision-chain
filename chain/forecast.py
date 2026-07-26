"""Stage 1 — forecast: WeeklyDemand -> DemandForecast, under rolling-origin CV.

Conventions adapted from my retail-analytics-real repo (retail/forecast.py) and
my ml-models-lab repo (mllab/metrics.py): MASE as the metric, rolling-origin
cross-validation that is leakage-safe by construction (every fold trains
strictly before its origin), and honest reporting — if a fancy model loses to
naive on real data, that is the reported result.

Models (numpy only, no sklearn/statsmodels):

* naive             y_hat = last value
* seasonal-naive    y_hat(t+i) = y(t+i-52), falls back to naive when history < 52
* croston-sba       Croston's method with the Syntetos-Boylan Approximation
                    correction (the standard for intermittent demand)
* lag-linear        OLS on lags 1-4 + annual Fourier pair + trend, recursive
                    (the calendar-free regression that fits retail data;
                    adapted from my retail-analytics-real repo)

Demand classes (Syntetos-Boylan quadrants on the ADI / CV^2 plane):
ADI = mean inter-demand interval in weeks; CV2 = squared coefficient of
variation of the NONZERO demand sizes. Cutoffs ADI 1.32, CV2 0.49:

    smooth        ADI <  1.32, CV2 <  0.49
    erratic       ADI <  1.32, CV2 >= 0.49
    intermittent  ADI >= 1.32, CV2 <  0.49
    lumpy         ADI >= 1.32, CV2 >= 0.49

Per class, the winner is the model with the lowest mean MASE across all
(SKU, fold) cells of that class — measured, not assumed. MASE here is scaled
by the in-sample MAE of the one-week naive walk (m=1): with ~2 years of
weekly history per SKU there is at most one full seasonal cycle to scale by,
so the seasonal scale would be undefined for most SKUs (same convention as
the per-SKU table in retail-analytics-real).

All point forecasts are clipped at zero (demand is non-negative).
Uncertainty: Sigma per SKU is the standard deviation of the winning model's
pooled rolling-origin CV errors (what stage 2 needs for safety stock).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from chain.contracts import DemandForecast, WeeklyDemand

SEASON_WEEKS = 52
WEEKS_PER_YEAR = 365.25 / 7.0
ADI_CUTOFF = 1.32
CV2_CUTOFF = 0.49
HORIZON = 8
N_FOLDS = 3
FOLD_STEP = 8
MIN_TRAIN = 30
MIN_NONZERO = 5  # SKUs with fewer nonzero weeks are reported, not forecast


# --------------------------------------------------------------------------- #
# Models: f(train_values, horizon) -> np.ndarray of length horizon
# --------------------------------------------------------------------------- #
def naive(y: np.ndarray, h: int) -> np.ndarray:
    return np.full(h, max(float(y[-1]), 0.0))


def seasonal_naive(y: np.ndarray, h: int, m: int = SEASON_WEEKS) -> np.ndarray:
    # adapted from my retail-analytics-real repo (retail/forecast.py)
    if len(y) < m:
        return naive(y, h)
    return np.clip(np.array([y[-m + (i % m)] for i in range(h)], dtype=float), 0.0, None)


def croston_sba(y: np.ndarray, h: int, alpha: float = 0.1) -> np.ndarray:
    """Croston's method with the SBA correction factor (1 - alpha/2).

    Exponentially smooths nonzero demand sizes (z) and inter-demand intervals
    (p) separately, only updating on demand occurrences; the flat forecast is
    (1 - alpha/2) * z_hat / p_hat units/week.
    """
    y = np.asarray(y, dtype=float)
    nonzero_idx = np.flatnonzero(y > 0)
    if len(nonzero_idx) == 0:
        return np.zeros(h)
    sizes = y[nonzero_idx]
    intervals = np.diff(np.concatenate([[-1.0], nonzero_idx.astype(float)]))
    z_hat = float(sizes[0])
    p_hat = float(intervals[0])
    for z, p in zip(sizes[1:], intervals[1:], strict=True):
        z_hat = alpha * z + (1 - alpha) * z_hat
        p_hat = alpha * p + (1 - alpha) * p_hat
    rate = (1 - alpha / 2.0) * z_hat / max(p_hat, 1.0)
    return np.full(h, max(rate, 0.0))


def lag_linear(y: np.ndarray, h: int, lags: tuple[int, ...] = (1, 2, 3, 4)) -> np.ndarray:
    """OLS (numpy lstsq) on lag features + annual Fourier pair + trend; recursive.

    adapted from my retail-analytics-real repo (retail/forecast.py).
    """
    max_lag = max(lags)
    n = len(y)
    if n < max_lag + 8:
        return naive(y, h)

    def features(t: int, history: np.ndarray) -> list[float]:
        angle = 2.0 * np.pi * t / WEEKS_PER_YEAR
        row = [1.0, t / n, np.sin(angle), np.cos(angle)]
        row.extend(history[t - lag] for lag in lags)
        return row

    x_rows = [features(t, y) for t in range(max_lag, n)]
    coef, *_ = np.linalg.lstsq(np.asarray(x_rows), y[max_lag:], rcond=None)

    history = y.astype(float).copy()
    preds = []
    for i in range(h):
        t = n + i
        pred = float(np.asarray(features(t, history)) @ coef)
        preds.append(pred)
        history = np.append(history, pred)
    return np.clip(np.array(preds), 0.0, None)


MODELS = {
    "naive": naive,
    "seasonal_naive": seasonal_naive,
    "croston_sba": croston_sba,
    "lag_linear": lag_linear,
}


# --------------------------------------------------------------------------- #
# Metrics  (conventions from my ml-models-lab repo, mllab/metrics.py)
# --------------------------------------------------------------------------- #
def mae(actual: np.ndarray, pred: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(actual, float) - np.asarray(pred, float))))


def mase(actual: np.ndarray, pred: np.ndarray, train: np.ndarray, m: int = 1) -> float:
    """MAE scaled by the in-sample MAE of the m-step naive walk on ``train``."""
    train = np.asarray(train, float)
    if len(train) <= m:
        m = 1
    denom = float(np.mean(np.abs(train[m:] - train[:-m])))
    if denom == 0.0 or not np.isfinite(denom):
        return float("nan")
    return mae(actual, pred) / denom


# --------------------------------------------------------------------------- #
# Demand classification (Syntetos-Boylan quadrants)
# --------------------------------------------------------------------------- #
def classify(y: np.ndarray) -> str:
    y = np.asarray(y, dtype=float)
    nonzero = y[y > 0]
    if len(nonzero) == 0:
        return "no-demand"
    adi = len(y) / len(nonzero)
    cv2 = float(np.var(nonzero) / np.mean(nonzero) ** 2) if len(nonzero) > 1 else 0.0
    if adi < ADI_CUTOFF:
        return "smooth" if cv2 < CV2_CUTOFF else "erratic"
    return "intermittent" if cv2 < CV2_CUTOFF else "lumpy"


# --------------------------------------------------------------------------- #
# Rolling-origin cross-validation  (adapted from my retail-analytics-real repo)
# --------------------------------------------------------------------------- #
@dataclass
class Fold:
    train_idx: np.ndarray
    test_idx: np.ndarray


def rolling_origin_folds(
    n: int,
    horizon: int = HORIZON,
    n_folds: int = N_FOLDS,
    step: int = FOLD_STEP,
    min_train: int = MIN_TRAIN,
) -> list[Fold]:
    """Fold k trains on [0, origin_k) and tests on [origin_k, origin_k + horizon).

    Origins step back from the end of the series; folds whose training window
    would fall under ``min_train`` are discarded (never padded with future data).
    """
    last_origin = n - horizon
    folds = []
    for k in range(n_folds):
        origin = last_origin - (n_folds - 1 - k) * step
        if origin < min_train:
            continue
        folds.append(
            Fold(train_idx=np.arange(0, origin), test_idx=np.arange(origin, origin + horizon))
        )
    return folds


def cross_validate_sku(values: np.ndarray) -> list[dict]:
    """One row per (model, fold) for a single SKU's weekly series."""
    rows = []
    for fold_no, fold in enumerate(rolling_origin_folds(len(values)), start=1):
        train, test = values[fold.train_idx], values[fold.test_idx]
        for name, model in MODELS.items():
            pred = model(train, len(test))
            rows.append(
                {
                    "model": name,
                    "fold": fold_no,
                    "train_weeks": len(train),
                    "mase": mase(test, pred, train, m=1),
                    "mae": mae(test, pred),
                    "errors": (np.asarray(test, float) - np.asarray(pred, float)),
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# Stage 1 end-to-end
# --------------------------------------------------------------------------- #
def run(demand: WeeklyDemand, horizon: int = HORIZON) -> DemandForecast:
    """Per-SKU rolling-origin CV, per-class winners, final forecasts + uncertainty."""
    cv_rows: list[dict] = []
    sku_meta: dict[str, dict] = {}

    for sku in demand.skus:
        series = demand.series(sku)
        values = series.to_numpy(dtype=float)
        n_nonzero = int((values > 0).sum())
        demand_class = classify(values)
        sku_meta[sku] = {
            "class": demand_class,
            "values": values,
            "last_week": series.index[-1] if len(series) else None,
            "eligible": n_nonzero >= MIN_NONZERO and len(rolling_origin_folds(len(values))) > 0,
        }
        if not sku_meta[sku]["eligible"]:
            continue
        for row in cross_validate_sku(values):
            row["StockCode"] = sku
            row["class"] = demand_class
            cv_rows.append(row)

    cv = pd.DataFrame(
        [{k: v for k, v in r.items() if k != "errors"} for r in cv_rows],
        columns=["StockCode", "class", "model", "fold", "train_weeks", "mase", "mae"],
    )

    # per-class winners: lowest mean MASE over all (SKU, fold) cells of the class
    winners: dict[str, str] = {}
    winner_rows = []
    if len(cv):
        by_class = cv.groupby(["class", "model"], observed=True)["mase"].agg(
            mean_mase="mean", cells="count"
        ).reset_index()
        for cls, group in by_class.groupby("class", observed=True):
            group = group.dropna(subset=["mean_mase"]).sort_values(["mean_mase", "model"])
            if group.empty:
                continue
            best = group.iloc[0]
            winners[str(cls)] = str(best["model"])
            for _, row in group.iterrows():
                winner_rows.append(
                    {
                        "class": cls,
                        "model": row["model"],
                        "mean_mase": float(row["mean_mase"]),
                        "cells": int(row["cells"]),
                        "winner": row["model"] == best["model"],
                    }
                )
    class_winners = pd.DataFrame(
        winner_rows, columns=["class", "model", "mean_mase", "cells", "winner"]
    )

    # final forecasts: winning model of the SKU's class on the full history
    fc_rows = []
    error_pool: dict[tuple[str, str], list[np.ndarray]] = {}
    for row in cv_rows:
        error_pool.setdefault((row["StockCode"], row["model"]), []).append(row["errors"])

    for sku, meta in sku_meta.items():
        if not meta["eligible"]:
            continue
        model_name = winners.get(meta["class"], "naive")
        values = meta["values"]
        preds = MODELS[model_name](values, horizon)
        pooled = np.concatenate(error_pool.get((sku, model_name), [np.array([0.0])]))
        sigma = float(np.std(pooled))
        future_weeks = pd.date_range(
            start=meta["last_week"] + pd.Timedelta(weeks=1), periods=horizon, freq="W-SUN"
        )
        for week, units in zip(future_weeks, preds, strict=True):
            fc_rows.append(
                {
                    "StockCode": sku,
                    "Week": week,
                    "Units": float(units),
                    "Sigma": sigma,
                    "Model": model_name,
                    "Class": meta["class"],
                }
            )
    forecasts = pd.DataFrame(
        fc_rows, columns=["StockCode", "Week", "Units", "Sigma", "Model", "Class"]
    )
    return DemandForecast(
        forecasts=forecasts, cv=cv, class_winners=class_winners, horizon_weeks=horizon
    )
