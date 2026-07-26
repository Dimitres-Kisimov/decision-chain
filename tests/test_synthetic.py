"""Synthetic layers: determinism, coverage, labelling, and the documented rules."""

from __future__ import annotations

import pandas as pd

from chain import synthetic
from chain.contracts import Provenance


def _rebuild(stage0, stage1, seed=synthetic.SEED):
    cleaned, demand, _ = stage0
    descriptions = synthetic.modal_descriptions(cleaned.sales, demand.skus)
    classes = (
        stage1.forecasts.drop_duplicates("StockCode").set_index("StockCode")["Class"].to_dict()
    )
    return synthetic.build(demand.skus, descriptions, classes, seed=seed)


def test_synthetic_is_deterministic_under_seed(stage0, stage1, layers):
    again = _rebuild(stage0, stage1)
    pd.testing.assert_frame_equal(layers.sku_physical, again.sku_physical)
    pd.testing.assert_frame_equal(layers.lead_times, again.lead_times)
    pd.testing.assert_frame_equal(layers.geometry.slots, again.geometry.slots)


def test_synthetic_seed_actually_matters(stage0, stage1, layers):
    other = _rebuild(stage0, stage1, seed=7)
    assert not layers.sku_physical[["LengthCm", "WeightKg"]].equals(
        other.sku_physical[["LengthCm", "WeightKg"]]
    )


def test_every_tracked_sku_covered_and_sane(stage0, layers):
    _, demand, _ = stage0
    skus = set(demand.skus)
    assert set(layers.sku_physical["StockCode"]) == skus
    assert set(layers.lead_times["StockCode"]) == skus
    assert (layers.sku_physical[["LengthCm", "WidthCm", "HeightCm", "WeightKg"]] > 0).all().all()
    assert (layers.lead_times["LeadTimeWeeks"] >= 1).all()
    # geometry: capacity >= tracked SKU count, positive dispatch distances
    assert layers.geometry.n_slots >= len(skus)
    assert (layers.geometry.slots["DistanceM"] > 0).all()
    assert layers.geometry.slots["SlotId"].is_unique


def test_synthetic_outputs_are_labelled(layers):
    assert layers.provenance is Provenance.SYNTHETIC_ASSIGNED
    assert layers.geometry.provenance is Provenance.SYNTHETIC_ASSIGNED
    assert layers.assumptions["provenance"] == "synthetic-assigned"
    assert layers.assumptions["seed"] == synthetic.SEED


def test_size_class_rule_is_the_documented_one():
    assert synthetic.size_class("JUMBO BAG RED RETROSPOT") == "large"
    assert synthetic.size_class("MINI PAINT SET VINTAGE") == "small"
    assert synthetic.size_class("PARTY BUNTING") == "medium"
    # precedence: a large keyword wins over a small one
    assert synthetic.size_class("JUMBO MINI THING") == "large"
