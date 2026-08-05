"""Presentation-layer guards: the README hero + the dashboard walkthrough.

These tests do NOT touch chain/*.py (they only read repo-root markdown), so the
committed run receipt (artifacts/full_run.json, whose identity is a sha256 per
chain source) stays fresh. They lock in that the finished proof is discoverable
and narrated:

* the README hero points at the clickable dashboard file and states the
  13-identity headline;
* docs/DASHBOARD_WALKTHROUGH.md exists and names all four dashboard views, and
  keeps the honesty markers (real-vs-synthetic boundary, CC BY 4.0, the
  fixture-path label on the what-if).
"""

from __future__ import annotations

import re

from chain import paths

README = paths.ROOT / "README.md"
WALKTHROUGH = paths.ROOT / "docs" / "DASHBOARD_WALKTHROUGH.md"
DASHBOARD = "web/chain_dashboard.html"

# The four dashboard views the walkthrough must narrate, by name.
FOUR_VIEWS = ("stage flow", "13 identities", "boundary map", "scenario what-if")


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# README hero
# --------------------------------------------------------------------------- #
def test_readme_references_the_dashboard_file():
    """The README must prominently point at the clickable, self-contained
    dashboard file so a reader can open it in one click."""
    text = _read(README)
    assert DASHBOARD in text, f"README does not reference {DASHBOARD}"


def test_readme_states_the_13_identity_headline():
    """The 13-identity headline must be stated, not merely implied."""
    text = _read(README).lower()
    patterns = (
        r"13\s*/\s*13\s+identities\s+pass",  # "13 / 13 identities PASS"
        r"all\s+13\s+cross-stage\s+identities\s+pass",
    )
    assert any(re.search(p, text) for p in patterns), (
        "README is missing an explicit 13-identity PASS headline"
    )


def test_readme_links_the_walkthrough():
    assert "docs/DASHBOARD_WALKTHROUGH.md" in _read(README)


# --------------------------------------------------------------------------- #
# The narrated walkthrough
# --------------------------------------------------------------------------- #
def test_walkthrough_exists_and_is_substantial():
    assert WALKTHROUGH.exists(), "docs/DASHBOARD_WALKTHROUGH.md is missing"
    assert len(_read(WALKTHROUGH)) > 2000, "walkthrough is too thin to be a real tour"


def test_walkthrough_names_the_four_views():
    text = _read(WALKTHROUGH).lower()
    missing = [view for view in FOUR_VIEWS if view not in text]
    assert not missing, f"walkthrough does not name these views: {missing}"


def test_walkthrough_points_at_the_dashboard_file():
    assert DASHBOARD in _read(WALKTHROUGH)


def test_walkthrough_keeps_the_honesty_markers():
    """Real-vs-synthetic boundary stated, CC BY 4.0 kept, and the what-if
    honestly labelled as the fast fixture path (not the full run)."""
    text = _read(WALKTHROUGH).lower()
    assert "synthetic-assigned" in text
    assert "cc by 4.0" in text
    assert "fixture" in text
    assert "modelled, not measured" in text
