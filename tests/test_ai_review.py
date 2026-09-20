import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.ai_review import (APP_VERSION, EVIDENCE_SCHEMA, PROTOCOL, PYTHON_SUITES,
                           REQUIRED_PATHS, UI_SUITES, build_review, file_hash, verify_evidence)


@pytest.fixture
def proof(tmp_path):
    root = tmp_path / "source"
    for name in REQUIRED_PATHS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("verified fixture\n", encoding="utf-8")
    stats = {"passed": 1, "failed": 0, "errors": 0, "skipped": 0}
    report = {"schema": EVIDENCE_SCHEMA, "protocol": PROTOCOL, "app_version": APP_VERSION,
        "verified_at": "2026-01-01T00:00:00+00:00", "verification_kind": "local_automated",
        "external_ai_measured": False,
        "source_hashes": {name: file_hash(root / name) for name in REQUIRED_PATHS},
        "python": {"exit_code": 0, **stats, "passed": len(PYTHON_SUITES),
                   "suites": {name: dict(stats) for name in PYTHON_SUITES}},
        "ui": {"failed": 0, "passed_suites": list(UI_SUITES),
               "syntax_checked": ["mobile/app.js", "web/js/app.js"]}}
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return root, path, report


def test_verified_review_distinguishes_local_checks_and_provider_performance(proof):
    root, path, _ = proof
    review = build_review(root=root, evidence_path=path, governance_status={"providers": [
        {"provider": "qwen", "label": "Qwen", "price_configured": True,
         "monthly_limit_usd": 10, "remaining_usd": 10, "secret": "must not echo"}]})
    assert review["verification"]["valid"] is True
    assert review["counts"] == {"adopted": 3, "assistance_only": 4, "pending": 2, "rejected": 1}
    assert review["adopted_external_provider"] is None
    assert review["changes_live_prediction"] is False
    assert review["external_calls"] == 0
    assert review["providers"][0]["adopted"] is False
    assert "must not echo" not in json.dumps(review)


def test_changed_source_or_missing_source_revokes_prior_review(proof):
    root, path, _ = proof
    source = root / REQUIRED_PATHS[0]
    source.write_text("changed", encoding="utf-8")
    for deleted in (False, True):
        if deleted:
            source.unlink()
        review = build_review(root=root, evidence_path=path)
        assert review["verification"]["status"] == "stale"
        assert review["verification"]["changed_files"] == [REQUIRED_PATHS[0]]
        assert review["counts"] == {"pending": 9, "rejected": 1}


@pytest.mark.parametrize("defect", ["schema", "version", "future", "naive", "external", "skipped",
    "suite", "count", "ui", "syntax", "hashes", "structure", "stats", "ui_type"])
def test_incomplete_or_malformed_proof_fails_closed(proof, defect):
    root, path, original = proof
    report = copy.deepcopy(original)
    if defect == "schema": report["schema"] = "unknown"
    elif defect == "version": report["app_version"] = "old"
    elif defect == "future": report["verified_at"] = "2099-01-01T00:00:00+00:00"
    elif defect == "naive": report["verified_at"] = "2026-01-01T00:00:00"
    elif defect == "external": report["external_ai_measured"] = True
    elif defect == "skipped": report["python"]["skipped"] = 1
    elif defect == "suite": del report["python"]["suites"][PYTHON_SUITES[0]]
    elif defect == "count": report["python"]["passed"] += 1
    elif defect == "ui": report["ui"]["passed_suites"].pop()
    elif defect == "syntax": report["ui"]["syntax_checked"] = []
    elif defect == "hashes": report["source_hashes"]["../private"] = "bad"
    elif defect == "structure": report = []
    elif defect == "stats": report["python"]["suites"] = []
    elif defect == "ui_type": report["ui"]["passed_suites"] = [None]
    path.write_text(json.dumps(report), encoding="utf-8")
    review = build_review(root=root, evidence_path=path)
    assert not review["verification"]["valid"]
    assert review["counts"] == {"pending": 9, "rejected": 1}


def test_missing_or_invalid_json_does_not_claim_a_pass(tmp_path):
    path = tmp_path / "missing.json"
    assert verify_evidence(path, tmp_path)["status"] == "missing"
    path.write_text("broken", encoding="utf-8")
    assert verify_evidence(path, tmp_path)["status"] == "invalid"


def test_hash_normalizes_windows_line_endings_and_bom(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"one\ntwo\n")
    second.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    assert file_hash(first) == file_hash(second)


def test_application_dependencies_are_covered_by_review():
    assert {"hall/seat_history.py", "scraper/events.py", "scraper/minrepo_archive.py",
            "api/routers/events.py", "requirements.txt"}.issubset(REQUIRED_PATHS)


def test_verification_environment_cannot_inherit_test_selection_or_credentials(tmp_path, monkeypatch):
    from scripts.verify_ai_release import test_environment
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "QWEN_API_KEY", "PACHI_AI_PROVIDER"):
        monkeypatch.setenv(name, "must not inherit")
    env = test_environment(tmp_path)
    assert not {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "QWEN_API_KEY", "PACHI_AI_PROVIDER"} & env.keys()
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert env["PACHI_AI_ALLOW_EXTERNAL"] == "false"


def test_review_api_is_read_only_and_external_requests_require_client_ids(tmp_path, monkeypatch):
    from api.routers import ai as router
    from api.ai_governance import AIGovernanceService
    service = AIGovernanceService(tmp_path / "usage.db")
    monkeypatch.setattr(router, "governance", service)
    monkeypatch.setattr(router, "build_review", lambda **kw: build_review(
        evidence_path=tmp_path / "absent.json", root=tmp_path, **kw))
    app = FastAPI()
    app.include_router(router.router)
    client = TestClient(app)
    assert client.get("/api/ai/review").json()["overall_status"] == "recheck_required"
    assert not list(tmp_path.iterdir())
    def never(**kwargs):
        pytest.fail("external request without client ID reached execution")
    monkeypatch.setattr(router, "hall_question_result", never)
    monkeypatch.setattr(router, "run_comparison", never)
    base = {"hall_name": "店舗A", "visit_date": "2026-09-20"}
    assert client.post("/api/ai/hall_ask", json={**base, "use_external_ai": True}).status_code == 422
    assert client.post("/api/ai/comparison/run", json={**base, "mode": "external",
        "providers": ["qwen"], "case_ids": ["machine"]}).status_code == 422
    monkeypatch.setattr(router, "hall_question_result", lambda **kw: {"id": kw["external_request_id"]})
    monkeypatch.setattr(router, "run_comparison", lambda **kw: {"id": kw["request_id"]})
    assert client.post("/api/ai/hall_ask", json=base).json() == {"id": None}
    assert client.post("/api/ai/comparison/run", json={**base, "providers": ["qwen"],
        "case_ids": ["machine"]}).json() == {"id": None}
