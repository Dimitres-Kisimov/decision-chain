"""Contract invariants: the provenance inheritance rule and stage-object tags."""

from __future__ import annotations

import pytest

from chain.contracts import STAGES, Provenance


def test_provenance_ordering():
    assert Provenance.REAL.strength > Provenance.DERIVED.strength
    assert Provenance.DERIVED.strength > Provenance.SYNTHETIC_ASSIGNED.strength


def test_combine_returns_weakest():
    assert (
        Provenance.combine(Provenance.REAL, Provenance.SYNTHETIC_ASSIGNED)
        is Provenance.SYNTHETIC_ASSIGNED
    )
    assert Provenance.combine(Provenance.REAL, Provenance.DERIVED) is Provenance.DERIVED
    assert Provenance.combine(Provenance.REAL, Provenance.REAL) is Provenance.REAL
    with pytest.raises(ValueError):
        Provenance.combine()


def test_derive_caps_at_derived():
    # a computation over purely real inputs is derived, never real
    assert Provenance.derive(Provenance.REAL) is Provenance.DERIVED
    assert Provenance.derive(Provenance.REAL, Provenance.REAL) is Provenance.DERIVED
    # anything touching a synthetic input stays synthetic-assigned downstream
    assert (
        Provenance.derive(Provenance.DERIVED, Provenance.SYNTHETIC_ASSIGNED)
        is Provenance.SYNTHETIC_ASSIGNED
    )


def test_stage_objects_carry_declared_tags(stage0, stage1):
    cleaned, demand, stream = stage0
    assert cleaned.provenance is Provenance.REAL
    assert demand.provenance is Provenance.REAL
    assert stream.provenance is Provenance.REAL
    assert stage1.provenance is Provenance.DERIVED
    assert stage1.provenance.tag() == "[derived]"


def test_all_seven_stages_declared():
    assert list(STAGES) == [0, 1, 2, 3, 4, 5, 6]
    assert STAGES[0] == "ingest" and STAGES[6] == "reconcile"
