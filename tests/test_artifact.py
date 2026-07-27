"""Phase-4 tests: the run artifact (chain/artifact.py).

Round-trip equality, byte determinism, schema content, staleness detection,
and the schema-version gate — all on the committed real-row fixture.
"""

from __future__ import annotations

import copy
import json

import pytest

from chain import artifact as artifact_mod

VALID_PROVENANCE = {"real", "derived", "synthetic-assigned"}


def test_round_trip_save_load_equality(fixture_artifact, tmp_path):
    path = artifact_mod.save(fixture_artifact, tmp_path / "a.json")
    assert artifact_mod.load(path) == fixture_artifact


def test_save_is_byte_deterministic(fixture_artifact, tmp_path):
    p1 = artifact_mod.save(fixture_artifact, tmp_path / "a.json")
    p2 = artifact_mod.save(copy.deepcopy(fixture_artifact), tmp_path / "b.json")
    assert p1.read_bytes() == p2.read_bytes()
    # ASCII-only on disk (the artifact is a committed, diffable receipt)
    p1.read_bytes().decode("ascii")


def test_schema_content(fixture_artifact):
    art = fixture_artifact
    assert art["schema_version"] == artifact_mod.SCHEMA_VERSION
    assert art["source"] == "fixture"
    # all 13 identities present and PASS, with both sides recorded
    assert art["identities_total"] == 13
    assert art["identities_passed"] == 13
    assert art["all_passed"] is True
    for check in art["identities"]:
        assert {"name", "lhs", "rhs", "lhs_label", "rhs_label", "tolerance", "passed"} <= set(check)
    # the 7 stages, each with a headline number and a valid provenance tag
    assert [s["name"] for s in art["stages"]] == [
        "ingest", "forecast", "inventory", "warehouse",
        "fulfilment (DES)", "transport (CVRP)", "costing",
    ]
    for stage in art["stages"]:
        assert stage["provenance"] in VALID_PROVENANCE
        assert isinstance(stage["headline"]["value"], int | float)
    # ledger entries carry provenance; the boundary map is embedded
    assert len(art["ledger"]) > 20
    assert all(e["provenance"] in VALID_PROVENANCE for e in art["ledger"])
    assert len(art["boundary_map"]) == 10
    # code fingerprint covers the chain package
    assert "chain/reconcile.py" in art["code_fingerprint"]
    assert "chain/artifact.py" in art["code_fingerprint"]


def test_fresh_artifact_is_not_stale(fixture_artifact):
    assert artifact_mod.stale_files(fixture_artifact) == []
    assert not artifact_mod.is_stale(fixture_artifact)


def test_staleness_detects_changed_source(fixture_artifact):
    tampered = copy.deepcopy(fixture_artifact)
    tampered["code_fingerprint"]["chain/costing.py"] = "0" * 64
    assert artifact_mod.stale_files(tampered) == ["chain/costing.py"]
    assert artifact_mod.is_stale(tampered)


def test_staleness_detects_new_and_removed_files(fixture_artifact):
    tampered = copy.deepcopy(fixture_artifact)
    del tampered["code_fingerprint"]["chain/ingest.py"]  # file is new on disk
    tampered["code_fingerprint"]["chain/removed.py"] = "f" * 64  # file is gone
    assert set(artifact_mod.stale_files(tampered)) == {"chain/ingest.py", "chain/removed.py"}


def test_load_rejects_unknown_schema_major(fixture_artifact, tmp_path):
    bad = copy.deepcopy(fixture_artifact)
    bad["schema_version"] = "999.0"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        artifact_mod.load(path)
