import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.image_analysis import ImageAnalysisStore
from api.routers import image_analysis as router


def payload(kind="data_lamp"):
    reviewed = {
        "game_count": 650, "seat_number": 501, "bb_count": None, "rb_count": None,
        "text": "", "seat_numbers": [],
    }
    if kind == "store_material":
        reviewed = {**reviewed, "game_count": None, "seat_number": None, "text": "9月19日 新台入替"}
    if kind == "floor_map":
        reviewed = {**reviewed, "game_count": None, "seat_number": None, "seat_numbers": [501, 508]}
    return {
        "kind": kind, "hall_name": "テスト店", "observed_on": "2026-09-19",
        "image": {"sha256": "a" * 64, "mime": "image/jpeg", "width": 1200, "height": 800, "size": 12345},
        "extraction_method": "text-detector",
        "extracted": [{"text": "650", "confidence": 0.5, "kind": "number"}],
        "reviewed": reviewed, "review_confirmed": True, "save_original_on_device": False,
    }


def client(tmp_path, monkeypatch):
    store = ImageAnalysisStore(tmp_path / "images.db")
    monkeypatch.setattr(router, "image_analyses", store)
    app = FastAPI()
    app.include_router(router.router)
    return TestClient(app), store


def test_status_is_read_only_and_says_images_never_leave_browser(tmp_path):
    store = ImageAnalysisStore(tmp_path / "images.db")
    status = store.status()
    assert status["saved_records"] == 0
    assert status["ocr_location"] == "browser"
    assert status["original_image_default"] == "not_saved"
    assert status["external_image_ai_enabled"] is False
    assert not store.db_path.exists()


@pytest.mark.parametrize("kind,status", [("data_lamp", "確認済み入力"), ("store_material", "確認済み入力"), ("floor_map", "未確認マップ")])
def test_reviewed_values_are_append_only_and_original_bytes_are_not_stored(tmp_path, monkeypatch, kind, status):
    api, store = client(tmp_path, monkeypatch)
    response = api.post("/api/image-analysis/records", json=payload(kind))
    assert response.status_code == 200
    record = response.json()
    assert record["verification_status"] == status
    assert record["image"]["original_location"] == "not_received"
    raw = store.db_path.read_bytes()
    assert b"data:image" not in raw and b"12345-base64-secret" not in raw
    rows = api.get("/api/image-analysis/records").json()
    assert rows[0]["reviewed"] == record["reviewed"]
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE image_analysis_record SET hall_name='changed'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM image_analysis_record")


@pytest.mark.parametrize("change", [
    {"review_confirmed": False},
    {"review_confirmed": 1},
    {"review_confirmed": "true"},
    {"image": {"sha256": "bad", "mime": "image/gif", "width": 1, "height": 1, "size": 1}},
    {"original_base64": "data:image/jpeg;base64,12345-base64-secret"},
])
def test_unreviewed_unsupported_or_raw_image_payload_is_rejected_before_db(tmp_path, monkeypatch, change):
    api, store = client(tmp_path, monkeypatch)
    response = api.post("/api/image-analysis/records", json={**payload(), **change})
    assert response.status_code == 422
    assert not store.db_path.exists()


def test_kind_specific_empty_or_duplicated_values_are_rejected(tmp_path, monkeypatch):
    api, _ = client(tmp_path, monkeypatch)
    empty = payload("data_lamp")
    empty["reviewed"] = {"text": "", "seat_numbers": []}
    assert api.post("/api/image-analysis/records", json=empty).status_code == 422
    duplicate = payload("floor_map")
    duplicate["reviewed"]["seat_numbers"] = [501, 501]
    assert api.post("/api/image-analysis/records", json=duplicate).status_code == 422


def test_device_only_flag_never_claims_server_has_original(tmp_path, monkeypatch):
    api, store = client(tmp_path, monkeypatch)
    body = payload()
    body["save_original_on_device"] = True
    result = api.post("/api/image-analysis/records", json=body).json()
    assert result["image"]["original_location"] == "not_received"
    assert result["image"]["device_copy_requested"] is True
    assert not any(path.suffix in {".jpg", ".jpeg", ".png", ".webp"} for path in tmp_path.iterdir())
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(image_analysis_record)")}
    assert not {"blob", "original", "image_bytes", "file_name"}.intersection(columns)


@pytest.mark.parametrize("send_legacy_candidates", [False, True])
def test_only_reviewed_values_survive_candidate_corrections_and_deletions(tmp_path, monkeypatch, send_legacy_candidates):
    api, store = client(tmp_path, monkeypatch)
    body = payload("store_material")
    body["reviewed"]["text"] = "訂正・確認した店舗資料"
    if send_legacy_candidates:
        body["extracted"] = [
            {"text": "incorrect-private-ocr-candidate", "confidence": 0.5, "kind": "text"},
            {"text": "deleted-private-ocr-candidate", "confidence": 0.5, "kind": "text"},
        ]
    else:
        body.pop("extracted")
    response = api.post("/api/image-analysis/records", json=body)
    assert response.status_code == 200
    assert response.json()["reviewed"] == body["reviewed"]
    assert response.json()["extracted"] == []
    assert api.get("/api/image-analysis/records").json()[0]["extracted"] == []
    with sqlite3.connect(store.db_path) as conn:
        stored = conn.execute("SELECT extracted_json,reviewed_json FROM image_analysis_record").fetchone()
    assert json.loads(stored[0]) == []
    assert json.loads(stored[1]) == body["reviewed"]
    assert b"private-ocr-candidate" not in store.db_path.read_bytes()


def test_legacy_candidates_are_hidden_without_rewriting_or_deleting_old_rows(tmp_path, monkeypatch):
    api, store = client(tmp_path, monkeypatch)
    record = store.save(payload())
    legacy = json.dumps([{"text": "old-private-candidate", "confidence": 0.5, "kind": "text"}])
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO image_analysis_record
            SELECT 'legacy-row',created_at,kind,hall_name,observed_on,image_sha256,image_mime,
                   image_width,image_height,image_size,extraction_method,?,reviewed_json,
                   local_original_saved,review_confirmed,verification_status
            FROM image_analysis_record WHERE id=?""", (legacy, record["id"]),
        )
    records = api.get("/api/image-analysis/records").json()
    assert len(records) == 2
    assert all(row["extracted"] == [] for row in records)
    assert "old-private-candidate" not in json.dumps(records)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT extracted_json FROM image_analysis_record WHERE id='legacy-row'").fetchone()[0] == legacy


@pytest.mark.parametrize("numbers", [[True], ["501"], [1.1], [100000], [0]])
def test_map_numbers_cannot_be_coerced_to_other_seats(tmp_path, monkeypatch, numbers):
    api, store = client(tmp_path, monkeypatch)
    body = payload("floor_map")
    body["reviewed"]["seat_numbers"] = numbers
    assert api.post("/api/image-analysis/records", json=body).status_code == 422
    assert not store.db_path.exists()


@pytest.mark.parametrize("kind,changes", [
    ("floor_map", {"game_count": 650}), ("store_material", {"seat_number": 501}),
    ("data_lamp", {"text": "明日は確定"}), ("data_lamp", {"seat_numbers": [501]}),
])
def test_values_from_other_image_kinds_are_rejected(tmp_path, monkeypatch, kind, changes):
    api, store = client(tmp_path, monkeypatch)
    body = payload(kind)
    body["reviewed"].update(changes)
    assert api.post("/api/image-analysis/records", json=body).status_code == 422
    assert not store.db_path.exists()
