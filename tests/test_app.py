"""Phase-4 tests: the CHAIN DASHBOARD (app.py) served from a fixture artifact.

The Flask test client exercises every endpoint; the DOM checks assert the
hero reconciliation panel really renders all 13 identities; the offline guard
greps templates/ and static/ for external references (the dashboard must work
with no network at all — no CDNs, no external fonts, no trackers).
"""

from __future__ import annotations

import copy
import json
import re

import pytest

from app import create_app
from chain import paths

VALID_PROVENANCE = {"real", "derived", "synthetic-assigned"}


@pytest.fixture(scope="module")
def client(fixture_artifact_path):
    app = create_app(fixture_artifact_path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["identities_passed"] == 13
    assert body["identities_total"] == 13
    assert body["all_passed"] is True
    assert body["artifact_stale"] is False


def test_api_identities(client):
    response = client.get("/api/identities")
    assert response.status_code == 200
    identities = response.get_json()
    assert len(identities) == 13
    names = [check["name"] for check in identities]
    for letter in "abcdefghijklm":
        assert any(f"({letter})" in name for name in names), f"identity ({letter}) missing"
    for check in identities:
        assert check["passed"] is True
        assert isinstance(check["lhs"], int | float)
        assert isinstance(check["rhs"], int | float)


def test_api_stages(client):
    stages = client.get("/api/stages").get_json()
    assert len(stages) == 7
    assert all(stage["provenance"] in VALID_PROVENANCE for stage in stages)


def test_api_json_endpoints_sane(client):
    for endpoint in ("/api/ledger", "/api/boundary", "/api/costing", "/api/slotting",
                     "/api/transport", "/api/artifact"):
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        assert response.get_json() is not None, endpoint
    slotting = client.get("/api/slotting").get_json()
    assert [row["variant"] for row in slotting] == ["random", "abc", "optimal"]
    costing = client.get("/api/costing").get_json()
    assert {line["item"] for line in costing["lines"]} >= {
        "labour", "transport", "holding", "facility", "total cost", "window revenue"
    }


def test_dashboard_renders_all_13_identities(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('class="identity-row"') == 13
    for letter in "abcdefghijklm":
        assert f"({letter})" in html, f"identity ({letter}) not in DOM"
    assert html.count("PASS") >= 13
    assert "FAIL" not in html
    # the other panels are present
    assert "Boundary map" in html
    assert "Cost-to-serve" in html
    assert "Slotting comparison" in html
    assert "per-class winners" in html
    # provenance color-coding classes are applied
    for cls in ("prov-real", "prov-derived", "prov-synth"):
        assert cls in html
    # fresh artifact -> no staleness banner
    assert "STALE ARTIFACT" not in html


def test_stale_artifact_shows_banner(fixture_artifact, tmp_path):
    tampered = copy.deepcopy(fixture_artifact)
    tampered["code_fingerprint"]["chain/costing.py"] = "0" * 64
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    app = create_app(path)
    app.config["TESTING"] = True
    with app.test_client() as client:
        health = client.get("/api/health").get_json()
        assert health["artifact_stale"] is True
        assert health["stale_files"] == ["chain/costing.py"]
        html = client.get("/").get_data(as_text=True)
        assert "STALE ARTIFACT" in html
        assert "chain/costing.py" in html


def test_no_external_references_in_templates_or_static():
    """Offline guard: no CDN scripts, external stylesheets, fonts or trackers.

    The only tolerated occurrences are XML/SVG namespace identifiers
    (http://www.w3.org/...), which are opaque identifiers, never fetched.
    """
    offenders = []
    for directory in (paths.ROOT / "templates", paths.ROOT / "static"):
        for file in sorted(directory.rglob("*")):
            if not file.is_file():
                continue
            text = file.read_text(encoding="utf-8")
            for match in re.finditer(r"https?://[^\s\"'<>)]+", text):
                if match.group(0).startswith("http://www.w3.org"):
                    continue
                offenders.append(f"{file.name}: {match.group(0)}")
            # protocol-relative URLs (//cdn.example.com/...) in src/href
            for match in re.finditer(r"(?:src|href)\s*=\s*[\"']//", text):
                offenders.append(f"{file.name}: {match.group(0)}")
    assert offenders == [], f"external references found: {offenders}"
