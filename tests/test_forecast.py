"""Stage 1: MASE hand-checks, CV leakage safety, Croston behavior, output contract."""

from __future__ import annotations

import numpy as np

from chain import forecast as fc


def test_mase_hand_check():
    # train naive-walk MAE: |3-1|,|5-3|,|7-5| -> mean 2.0
    train = np.array([1.0, 3.0, 5.0, 7.0])
    actual = np.array([9.0, 11.0])
    pred = np.array([8.0, 12.0])  # MAE = (1 + 1) / 2 = 1.0
    assert fc.mase(actual, pred, train, m=1) == 0.5


def test_mase_nan_on_constant_train():
    train = np.array([4.0, 4.0, 4.0, 4.0])
    assert np.isnan(fc.mase(np.array([4.0]), np.array([4.0]), train, m=1))


def test_rolling_origin_folds_are_leakage_safe():
    folds = fc.rolling_origin_folds(100, horizon=8, n_folds=3, step=8, min_train=30)
    assert len(folds) == 3
    for fold in folds:
        assert len(fold.train_idx) >= 30
        assert fold.train_idx.max() < fold.test_idx.min()  # train strictly before test
        assert len(np.intersect1d(fold.train_idx, fold.test_idx)) == 0
        assert fold.train_idx.min() == 0  # expanding window from the start
        assert len(fold.test_idx) == 8
    # short series: folds under min_train are discarded, not padded
    assert fc.rolling_origin_folds(20, horizon=8, n_folds=3, step=8, min_train=30) == []


def test_croston_sba_hand_check():
    # constant demand 2 every week: z_hat=2, p_hat=1 -> SBA rate = 0.95 * 2
    y = np.full(20, 2.0)
    pred = fc.croston_sba(y, 3, alpha=0.1)
    assert pred.shape == (3,)
    assert np.allclose(pred, 0.95 * 2.0)
    # all-zero history forecasts zero, never NaN
    assert np.allclose(fc.croston_sba(np.zeros(10), 4), 0.0)


def test_seasonal_naive_falls_back_when_history_short():
    y = np.arange(10, dtype=float)
    assert np.allclose(fc.seasonal_naive(y, 3, m=52), fc.naive(y, 3))


def test_classify_quadrants():
    assert fc.classify(np.full(52, 5.0)) == "smooth"
    intermittent = np.zeros(52)
    intermittent[::4] = 5.0  # ADI = 4, constant sizes
    assert fc.classify(intermittent) == "intermittent"
    lumpy = np.zeros(52)
    lumpy[::4] = [1, 50, 1, 50, 1, 50, 1, 50, 1, 50, 1, 50, 1]
    assert fc.classify(lumpy) == "lumpy"


def test_forecasts_are_nonnegative_with_uncertainty(stage1):
    out = stage1.forecasts
    assert len(out) > 0, "fixture must contain forecastable SKUs"
    assert (out["Units"] >= 0).all()
    assert (out["Sigma"] >= 0).all()
    assert out["Model"].isin(fc.MODELS).all()
    # every forecast SKU has exactly horizon_weeks rows
    assert (out.groupby("StockCode", observed=True).size() == stage1.horizon_weeks).all()


def test_class_winners_are_measured_not_assumed(stage1):
    winners = stage1.class_winners
    assert len(winners) > 0
    for _, group in winners.groupby("class", observed=True):
        assert group["winner"].sum() == 1  # exactly one winner per class
        best = group.loc[group["winner"]].iloc[0]
        assert best["mean_mase"] == group["mean_mase"].min()
