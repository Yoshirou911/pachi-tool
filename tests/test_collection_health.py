from scraper import collection_health


def test_collection_health_records_success_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_health, "DB_PATH", tmp_path / "health.db")
    result = collection_health.run_logged("dmm", lambda: {"hall": {"machines": 12, "floor_maps": 2}})
    assert result["hall"]["machines"] == 12
    health = collection_health.get_health()
    assert health["overall"] == "healthy"
    assert health["sources"][0]["records"] == 14

    try:
        collection_health.run_logged("broken", lambda: (_ for _ in ()).throw(ValueError("network")))
    except RuntimeError:
        pass
    health = collection_health.get_health()
    assert health["overall"] == "warning"
    assert any(item["source"] == "broken" and item["status"] == "failed" for item in health["sources"])


def test_collection_health_does_not_treat_empty_or_error_results_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_health, "DB_PATH", tmp_path / "health.db")
    collection_health.run_logged("empty", lambda: {"hall_a": 0, "hall_b": 0})
    collection_health.run_logged(
        "mixed",
        lambda: [
            {"hall_name": "hall_a", "status": "ok", "rows": 3},
            {"hall_name": "hall_b", "status": "error", "rows": 0},
        ],
    )
    collection_health.run_logged(
        "all_failed",
        lambda: [{"hall_name": "hall_a", "status": "error", "rows": 0}],
    )

    by_source = {item["source"]: item for item in collection_health.get_health()["sources"]}
    assert by_source["empty"]["status"] == "partial"
    assert by_source["mixed"]["status"] == "partial"
    assert by_source["all_failed"]["status"] == "failed"


def test_collection_health_counts_hall_number_mapping_and_marks_missing_sources_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_health, "DB_PATH", tmp_path / "health.db")
    collection_health.run_logged("pworld", lambda: {"hall_a": 53, "hall_b": 49})
    collection_health.run_logged(
        "public_daily",
        lambda: [
            {"hall_name": "hall_a", "status": "ok", "rows": 10},
            {"hall_name": "hall_b", "status": "no_public_data", "rows": 0},
        ],
    )
    by_source = {item["source"]: item for item in collection_health.get_health()["sources"]}
    assert by_source["pworld"]["status"] == "success"
    assert by_source["pworld"]["records"] == 102
    assert by_source["public_daily"]["status"] == "partial"
