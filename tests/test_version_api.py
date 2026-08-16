import re

from fastapi.testclient import TestClient

from api.main import app
from app_version import APP_VERSION


client = TestClient(app)


def test_version_endpoint_exposes_current_release_and_notes():
    response = client.get("/api/version")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == APP_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", payload["version"])
    assert payload["patch_notes"][0]["version"] == APP_VERSION
    assert payload["patch_notes"][0]["items"]


def test_patch_note_versions_are_unique():
    notes = client.get("/api/version").json()["patch_notes"]
    versions = [note["version"] for note in notes]
    assert len(versions) == len(set(versions))


def test_public_iphone_pwa_origin_is_allowed_by_cors():
    response = client.options(
        "/api/version",
        headers={
            "Origin": "https://yoshirou911.github.io",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://yoshirou911.github.io"
