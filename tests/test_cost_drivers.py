"""Cost-driver breakdown + identity (n) tests (cost_drivers.py).

Identity (n) rebuilds every stage-5 cost line from the physical driver another
stage registered, times its published synthetic rate, and checks the rebuild
equals costing's own line to the cent. These tests assert: (n) holds on the
fixture ledger AND on the committed full-run artifact; each line has a
deliberate-corruption FAIL path (a wrong rate, a miscounted driver); the
elasticity of the total to each driver equals its cost share exactly (verified
by re-running the costing engine); the shares are a valid partition; and the
emitted deliverables are byte-deterministic.
"""

from __future__ import annotations

import json

import pytest

import cost_drivers as cd
from chain import artifact as artifact_mod
from chain import costing, paths
from chain.contracts import Provenance

WINDOW_WEEKS_FIXTURE = 8  # the fixture window matches the full run's 8-week span


# --------------------------------------------------------------------------- #
# Identity (n) on the fixture-built ledger
# --------------------------------------------------------------------------- #
def _n_weeks(stage4) -> int:
    return len(stage4.window_weeks)


def test_identity_n_holds_on_fixture(full_ledger_and_checks, stage4):
    ledger, _ = full_ledger_and_checks
    check = cd.check_from_ledger(ledger, _n_weeks(stage4))
    assert check.name == "(n) cost-driver reconstruction"
    assert check.unit == "GBP"
    assert check.passed, check.note
    # lhs (rebuilt total) and rhs (ledger total) agree to the cent
    assert abs(check.lhs - check.rhs) <= cd.PENNY


def test_every_line_reconstructs_to_the_cent(full_ledger_and_checks, stage4):
    ledger, _ = full_ledger_and_checks
    values = {k: e.value for k, e in ledger.entries.items()}
    rows = cd.driver_rows(values, _n_weeks(stage4))
    assert [r.line_item for r in rows] == list(cd.COST_ITEMS)
    for r in rows:
        assert abs(r.residual_gbp) <= cd.PENNY, f"{r.line_item}: residual {r.residual_gbp}"
        # rebuilt from the driver, not copied from the ledger line
        assert r.reconstructed_gbp == pytest.approx(r.registered_gbp, abs=cd.PENNY)


def test_reconstruction_is_stronger_than_additivity(full_ledger_and_checks, stage4):
    """A wrong RATE keeps identity (l) true (total still == sum of lines) but
    must break identity (n) -- that is the seam (n) adds over (l)."""
    ledger, _ = full_ledger_and_checks
    values = {k: e.value for k, e in ledger.entries.items()}
    # Corrupt the driver quantity behind the transport line while leaving the
    # ledger's own transport GBP + total untouched: (l) is blind to this, (n) is not.
    values = dict(values)
    values["transport/cvrp_km"] = values["transport/cvrp_km"] * 1.10
    bad = cd.check_cost_driver_reconstruction(values, _n_weeks(stage4))
    assert not bad.passed
    assert "transport" in bad.note


@pytest.mark.parametrize("line_item", cd.COST_ITEMS)
def test_wrong_rate_fails_identity_n(full_ledger_and_checks, stage4, line_item, monkeypatch):
    """Each line has a FAIL path: bump its published rate and (n) must catch it."""
    ledger, _ = full_ledger_and_checks
    spec = next(s for s in cd.DRIVERS if s.line_item == line_item)
    # Double the published rate: the rebuilt line then differs from the ledger by
    # the whole line value, so (n) fails for any materially non-zero line.
    monkeypatch.setattr(costing, spec.rate_attr, getattr(costing, spec.rate_attr) * 2.0)
    check = cd.check_from_ledger(ledger, _n_weeks(stage4))
    assert not check.passed
    assert line_item in check.note


# --------------------------------------------------------------------------- #
# Identity (n) on the committed full-run artifact (the headline receipt)
# --------------------------------------------------------------------------- #
def test_identity_n_holds_on_committed_artifact():
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    check = cd.check_from_artifact(artifact)
    assert check.passed, check.note
    # the committed artifact carries exactly the 13 a-m identities; (n) is additive
    assert artifact["identities_total"] == 13
    assert check.name not in {i["name"] for i in artifact["identities"]}


def test_committed_breakdown_shares_partition_the_total():
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    rows = cd.breakdown_from_artifact(artifact)
    assert len(rows) == 4
    assert sum(r.share for r in rows) == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 <= r.share <= 1.0 for r in rows)
    # transport is the dominant driver on the committed run
    top = max(rows, key=lambda r: r.share)
    assert top.line_item == "transport"
    assert top.share > 0.5


def test_identity_n_flags_stale_or_corrupt_artifact():
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    corrupt = json.loads(json.dumps(artifact))  # deep copy, never written back
    for row in corrupt["ledger"]:
        if row["key"] == "costing/total_cost":
            row["value"] = row["value"] + 1.0  # a one-pound drift
    check = cd.check_from_artifact(corrupt)
    assert not check.passed


# --------------------------------------------------------------------------- #
# Elasticity of the total == cost share, verified by re-running the engine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("line_item", cd.COST_ITEMS)
def test_total_elasticity_equals_cost_share(
    stage0, stage2, stage4, stage5, stage6, line_item
):
    cleaned, demand, _ = stage0
    rows = {
        r.line_item: r
        for r in cd.driver_rows(
            {  # build a values map straight from the fixture costing result
                "costing/labour": stage6.line("labour").gbp,
                "costing/transport": stage6.line("transport").gbp,
                "costing/holding": stage6.line("holding").gbp,
                "costing/facility": stage6.line("facility").gbp,
                "costing/total_cost": stage6.total_cost_gbp,
                "fulfil/labour_hours": stage4.labour_hours,
                "transport/cvrp_km": stage5.total_cvrp_km,
                "inventory/safety_stock_units": float(stage2.plan["SafetyStock"].sum()),
            },
            _n_weeks(stage4),
        )
    }
    measured = cd.verify_total_elasticity(
        line_item,
        cleaned=cleaned,
        skus=demand.skus,
        plan=stage2,
        fulfilment=stage4,
        delivery=stage5,
        base_total_gbp=stage6.total_cost_gbp,
        factor=1.10,
    )
    # linear ledger => the measured elasticity equals the analytic share exactly
    assert measured == pytest.approx(rows[line_item].elasticity_of_total, abs=1e-9)


def test_cost_rate_module_constants_restored(stage0, stage2, stage4, stage5, stage6):
    cleaned, demand, _ = stage0
    before = {s.rate_attr: getattr(costing, s.rate_attr) for s in cd.DRIVERS}
    cd.verify_total_elasticity(
        "transport",
        cleaned=cleaned,
        skus=demand.skus,
        plan=stage2,
        fulfilment=stage4,
        delivery=stage5,
        base_total_gbp=stage6.total_cost_gbp,
    )
    after = {s.rate_attr: getattr(costing, s.rate_attr) for s in cd.DRIVERS}
    assert before == after  # no leaked monkeypatch on the module constants


# --------------------------------------------------------------------------- #
# Provenance + deterministic emit
# --------------------------------------------------------------------------- #
def test_every_cost_line_is_synthetic_assigned():
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    for row in cd._rows_for_csv(cd.breakdown_from_artifact(artifact)):
        assert row["provenance"] == Provenance.SYNTHETIC_ASSIGNED.value


def test_emit_is_byte_deterministic(tmp_path):
    artifact = artifact_mod.load(paths.ARTIFACT_JSON)
    rows = cd.breakdown_from_artifact(artifact)
    check = cd.check_from_artifact(artifact)
    csv1 = cd.write_csv(rows, tmp_path / "a.csv").read_bytes()
    csv2 = cd.write_csv(rows, tmp_path / "b.csv").read_bytes()
    assert csv1 == csv2
    csv1.decode("ascii")  # committed deliverable stays ASCII/diffable
    md1 = cd.write_markdown(rows, check, "src", tmp_path / "a.md").read_bytes()
    md2 = cd.write_markdown(rows, check, "src", tmp_path / "b.md").read_bytes()
    assert md1 == md2
    md1.decode("ascii")


def test_cli_runs_on_committed_artifact_and_holds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cd, "COST_DRIVERS_CSV", tmp_path / "cost_drivers.csv")
    monkeypatch.setattr(cd, "COST_DRIVERS_MD", tmp_path / "cost_drivers.md")
    rc = cd.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "identity (n)" in out.lower() or "(n) cost-driver reconstruction" in out
    assert (tmp_path / "cost_drivers.csv").exists()
    assert (tmp_path / "cost_drivers.md").exists()
