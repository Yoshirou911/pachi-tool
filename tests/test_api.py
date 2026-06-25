"""API smoke test — requires server running on localhost:8000"""
import pytest
import requests

BASE = "http://localhost:8000"

def _get_first_machine() -> str:
    r = requests.get(f"{BASE}/api/machines", timeout=5)
    r.raise_for_status()
    machines = r.json()
    assert len(machines) > 0
    return machines[0]

def test_machines():
    r = requests.get(f"{BASE}/api/machines", timeout=5)
    assert r.status_code == 200
    machines = r.json()
    assert len(machines) > 0

def test_estimate():
    machine_name = _get_first_machine()
    r = requests.post(f"{BASE}/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "element_counts": {},
    }, timeout=5)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "posterior" in data
    assert data["ev_pct"] > 0
    assert "confidence" in data
    assert "confidence_label" in data

def test_estimate_with_hall():
    machine_name = _get_first_machine()
    r = requests.post(f"{BASE}/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "hall_name": "ベガスベガス大東店",
        "weekday": 6,
    }, timeout=5)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "ev_pct" in data

def test_estimate_started_from():
    """宵越し補正: started_from=1000 で観測G数が2000になるか確認"""
    machine_name = _get_first_machine()
    r1 = requests.post(f"{BASE}/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 2000,
        "element_counts": {},
    }, timeout=5)
    r2 = requests.post(f"{BASE}/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "started_from": 1000,
        "element_counts": {},
    }, timeout=5)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # 両者は同じ観測G数(2000)なので後験は同一になるはず
    p1 = r1.json()["posterior"]
    p2 = r2.json()["posterior"]
    for s in p1:
        assert abs(p1[s] - p2[s]) < 1e-9, f"setting {s}: {p1[s]} != {p2[s]}"

def test_daito():
    r = requests.get(f"{BASE}/api/hall/daito", timeout=5)
    assert r.status_code == 200
    data = r.json()
    top = data["machine_scores"][0]
    assert "machine" in top
    assert "score" in top

def test_sessions():
    # create
    r = requests.post(f"{BASE}/api/sessions", json={
        "machine_name": "テスト機",
        "hall_name": "テストホール",
        "games_total": 1000,
        "investment": 5000,
        "returns": 4000,
    }, timeout=5)
    assert r.status_code == 200
    sid = r.json()["id"]
    # get
    r = requests.get(f"{BASE}/api/sessions/{sid}", timeout=5)
    assert r.status_code == 200
    s = r.json()
    assert s["machine_name"] == "テスト機"
    # delete
    r = requests.delete(f"{BASE}/api/sessions/{sid}", timeout=5)
    assert r.status_code == 200

def test_sessions_export():
    r = requests.get(f"{BASE}/api/sessions/export", timeout=5)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")

if __name__ == "__main__":
    test_machines()
    test_estimate()
    test_estimate_with_hall()
    test_estimate_started_from()
    test_daito()
    test_sessions()
    test_sessions_export()
    print("\n=== ALL API TESTS PASSED ===")
