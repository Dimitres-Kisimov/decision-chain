"""Tests for the static CHAIN dashboard generator (web/build_dashboard.py).

These guard the offline, self-contained web view WITHOUT touching chain/*.py:
the generator only reads the committed receipts, so the run artifact stays
fresh (its identity is a sha256 per chain source file). The drift guards assert
the rendered page quotes the artifact's numbers exactly and that the real vs
synthetic boundary is rendered exactly as labelled -- nothing real shown as
synthetic or vice-versa.
"""

from __future__ import annotations

import html as html_mod
import re

import pytest

from chain import artifact as artifact_mod
from chain import paths
from web import build_dashboard as bd

VALID_PROVENANCE = {"real", "derived", "synthetic-assigned"}


@pytest.fixture(scope="module")
def art() -> dict:
    """The committed full-run receipt the dashboard renders."""
    return artifact_mod.load(paths.ARTIFACT_JSON)


@pytest.fixture(scope="module")
def scen() -> dict:
    """The committed fixture what-if receipt."""
    return bd.load_scenarios(bd.SCENARIO_JSON)


@pytest.fixture(scope="module")
def page() -> str:
    """The rendered dashboard, built from the committed receipts."""
    return bd.generate()


# --------------------------------------------------------------------------- #
# Structure + the four required views
# --------------------------------------------------------------------------- #
def test_required_sections_present(page: str):
    for section in ("sec-flow", "sec-reconciliation", "sec-boundary", "sec-scenario"):
        assert f'id="{section}"' in page, f"missing section {section}"
    # the shared provenance colour code is applied
    for cls in ("prov-real", "prov-derived", "prov-synth"):
        assert cls in page


def test_all_13_identities_rendered(page: str, art: dict):
    assert page.count('class="identity-row"') == art["identities_total"] == 13
    for letter in "abcdefghijklm":
        assert f"({letter})" in page, f"identity ({letter}) not in the DOM"
    # every identity passes -> no failure badge anywhere on the page
    assert 'class="badge fail"' not in page
    assert 'class="verdict fail"' not in page


def test_identity_numbers_match_artifact(page: str, art: dict):
    """Both sides of every identity are quoted exactly as the artifact records
    them (formatted identically to the served dashboard) -- so the page cannot
    silently drift from the receipt."""
    for check in art["identities"]:
        assert bd.fmt(check["lhs"], 2) in page, check["name"]
        assert bd.fmt(check["rhs"], 2) in page, check["name"]
        assert html_mod.escape(check["lhs_label"], quote=True) in page
        assert html_mod.escape(check["rhs_label"], quote=True) in page


def test_stage_headlines_match_artifact(page: str, art: dict):
    for stage in art["stages"]:
        assert bd.fmt(stage["headline"]["value"]) in page, stage["name"]
        assert html_mod.escape(stage["name"], quote=True) in page


# --------------------------------------------------------------------------- #
# Boundary integrity -- the honesty guard
# --------------------------------------------------------------------------- #
def test_boundary_matches_artifact_exactly(page: str, art: dict):
    """Extract every (provenance, quantity) pair the page renders and compare it,
    in order, to the artifact's boundary map. This proves nothing real is shown
    as synthetic and nothing synthetic is shown as real."""
    pairs = re.findall(
        r'<tr class="bnd-row" data-prov="([^"]+)"><td class="bnd-q">(.*?)</td>', page
    )
    rendered = [(prov, html_mod.unescape(q)) for prov, q in pairs]
    expected = [(row["provenance"], row["quantity"]) for row in art["boundary_map"]]
    assert rendered == expected
    assert all(prov in VALID_PROVENANCE for prov, _ in rendered)


def test_costing_lines_match_artifact(page: str, art: dict):
    costing = artifact_mod.stage_by_name(art, "costing")["detail"]
    for line in costing["lines"]:
        assert bd.fmt(line["gbp"], 2) in page, line["item"]


# --------------------------------------------------------------------------- #
# Scenario what-if -- matches the committed fixture receipt
# --------------------------------------------------------------------------- #
def test_scenarios_present_and_hold(page: str, scen: dict):
    assert len(scen["scenarios"]) == 2
    for s in scen["scenarios"]:
        assert html_mod.escape(s["name"], quote=True) in page
        assert s["identities_hold"] is True
        badge = f"{s['identities_passed']}/{s['identities_total']} identities still reconcile"
        assert badge in page


def test_scenario_deltas_match_receipt(page: str, scen: dict):
    """Every baseline/perturbed number in the receipt is quoted on the page."""
    for s in scen["scenarios"]:
        for d in s["deltas"]:
            assert bd.fmt(d["baseline"], 2) in page, (s["kind"], d["metric"])
            assert bd.fmt(d["perturbed"], 2) in page, (s["kind"], d["metric"])


def test_scenario_source_is_labelled_fixture(page: str, scen: dict):
    """The what-if is honestly labelled as the fixture path, not the full run."""
    assert "fixture" in scen["source"].lower()
    assert html_mod.escape(scen["source"], quote=True) in page


# --------------------------------------------------------------------------- #
# Offline / self-contained
# --------------------------------------------------------------------------- #
def test_page_is_offline(page: str):
    """No CDN, external stylesheet/font, or tracker. The only tolerated URL is
    the SVG XML namespace, an opaque identifier that is never fetched."""
    offenders = []
    for match in re.finditer(r"https?://[^\s\"'<>)]+", page):
        if match.group(0).startswith("http://www.w3.org"):
            continue
        offenders.append(match.group(0))
    for match in re.finditer(r"(?:src|href)\s*=\s*[\"']//", page):
        offenders.append(match.group(0))
    assert offenders == [], f"external references found: {offenders}"
    # everything is inlined: no <link rel=stylesheet> and no external <script src>
    assert 'rel="stylesheet"' not in page
    assert "<script src" not in page


# --------------------------------------------------------------------------- #
# Determinism + committed-output in sync
# --------------------------------------------------------------------------- #
def test_build_is_deterministic():
    assert bd.generate() == bd.generate()


def test_write_is_byte_deterministic(tmp_path):
    text = bd.generate()
    p1 = bd.write_html(text, tmp_path / "a.html")
    p2 = bd.write_html(text, tmp_path / "b.html")
    assert p1.read_bytes() == p2.read_bytes()


def test_committed_html_in_sync(page: str):
    """The committed web/chain_dashboard.html must equal a fresh build from the
    committed receipts (newline-normalised for git autocrlf), so the visible
    page can never drift from the data."""
    assert bd.DEFAULT_OUT.exists(), "web/chain_dashboard.html is not committed"
    on_disk = bd.DEFAULT_OUT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert on_disk == page


# --------------------------------------------------------------------------- #
# The committed artifact must NOT be staled by this feature
# --------------------------------------------------------------------------- #
def test_committed_artifact_not_stale(art: dict):
    """Building the dashboard touches no chain/*.py, so the committed full-run
    receipt's code fingerprint still matches the sources on disk."""
    assert artifact_mod.stale_files(art) == []
    assert not artifact_mod.is_stale(art)
