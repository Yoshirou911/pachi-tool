from copy import deepcopy
from datetime import date, timedelta
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import ai_service
from api.ai_evidence import answer, evidence, metric
from api.ai_hall_analysis import build_hall_snapshot, resolve_topics
from api.routers import ai as ai_router


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hall.db"
    with sqlite3.connect(path) as c:
        c.executescript('''
          CREATE TABLE hall_day_machine (hall_name,machine_name,report_date,avg_diff_coins,source_url,scraped_at);
          CREATE TABLE hall_day_seat (hall_name,machine_name,seat_number,report_date,diff_coins,source_url,scraped_at);
          CREATE TABLE hall_layout (id,hall_name,floor_name,valid_from,valid_to,verification_status,source_kind,source_url,updated_at);
          CREATE TABLE hall_layout_seat (layout_id,seat_number,machine_name,island_name,row_name,row_order,x,y,rotation);
        ''')
        for i in range(35):
            day = (date(2026, 8, 10) + timedelta(days=i)).isoformat()
            monday = date.fromisoformat(day).weekday() == 0
            for machine, coins in [("スマスロモンキーターンV", 500 if monday else 100), ("スマスロ北斗の拳", -100)]:
                c.execute("INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?)", ("店舗A", machine, day, coins, "https://example.com/"+day, day+" 23:50:00"))
            c.execute("INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?)", ("店舗B", "別機種", day, 9999, None, None))
        c.execute("INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?)", ("店舗A", "未来機種", "2026-09-14", 99999, None, None))
        for day, machine, number, coins in [("2026-09-01", "旧機種", 101, 9999), ("2026-09-10", "新機種", 101, 100),
            ("2026-09-11", "新機種", 101, 200), ("2026-09-10", "新機種", 102, -100), ("2026-09-11", "新機種", 102, 0)]:
            c.execute("INSERT INTO hall_day_seat VALUES(?,?,?,?,?,?,?)", ("店舗A", machine, number, day, coins, "https://example.com/seat", None))
    return path


def build(db, **kwargs):
    return build_hall_snapshot(db, hall_name="店舗A", target_date="2026-09-14", days=90, **kwargs)


def value(fact, label):
    return next(m["value"] for m in fact["metrics"] if m["label"] == label)


@pytest.mark.parametrize("question,expected", [
    ("何曜日が強い？", ["weekday"]), ("7のつく日", ["digit"]),
    ("月曜日と末尾7", ["weekday", "digit"]), ("何を打てば？", ["machine"]),
    ("台番号101", ["seat"]), ("この島は？", ["layout"]),
    ("いつ行く？", ["weekday", "digit", "event"]), ("他店と比較して", ["comparison"]),
])
def test_question_topics_are_separated(question, expected):
    assert resolve_topics(question) == expected


def test_weekday_specific_answer_excludes_unrelated_days_other_halls_and_future(db):
    snap = build(db, question="月曜日は？")
    assert len(snap["evidence"]) == 1
    f = snap["evidence"][0]
    assert f["subject_label"] == "月曜日"
    assert value(f, "該当日数") == 5
    assert value(f, "日別・機種均等平均差枚") == 200
    assert f["period"]["start"] == "2026-08-10"
    assert any((s["retrieved_at"] or "").startswith("2026") for s in f["sources"])
    assert any(s["retrieved_at"] is None for s in f["sources"])
    assert "元データの取得時刻が未記録" in f["missing_information"]
    assert any(s["url"] == "https://example.com/2026-08-11" for s in f["sources"])
    assert "店舗B" not in json.dumps(snap, ensure_ascii=False)
    assert "未来機種" not in json.dumps(snap, ensure_ascii=False)


def test_machine_comparison_uses_common_days_and_never_substitutes_zero(db):
    snap = build(db, topic="machine", machine_name="スマスロモンキーターンV")
    f = snap["evidence"][0]
    assert value(f, "共通比較日数") == 35
    assert "同じ日に" in f["comparison_scope"]
    assert "設定投入" in f["interpretation"]
    new = build(db, topic="machine", machine_name="存在しない機種")
    assert new["evidence"] == []
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM hall_day_machine WHERE machine_name<>'スマスロモンキーターンV'")
        c.execute("DELETE FROM hall_day_seat")
    f = build(db, topic="machine")["evidence"][0]
    assert value(f, "共通比較日数") == 0
    assert not any(m["label"] == "同日の他機種との差" for m in f["metrics"])
    assert "比較できる" in f["interpretation"]


def test_family_question_does_not_return_other_machines(db):
    snap = build(db, question="モンキーは強い？")
    assert all("モンキー" in f["machine_name"] for f in snap["evidence"])
    combined = build(db, question="モンキーは月曜日が強い？")
    assert all("モンキー" in f["machine_name"] for f in combined["evidence"])


def test_digit_is_not_an_event_and_small_samples_do_not_claim_strength(db):
    f = build(db, question="末尾７の日")["evidence"][0]
    assert f["event_name"] is None
    assert "イベント認定ではありません" in f["subject_label"]
    assert "少数日" in f["interpretation"]
    assert f["decision"] is None


def test_seat_history_is_split_at_machine_replacement(db):
    f = build(db, question="101番台の履歴")["evidence"][0]
    assert f["seat_number"] == 101
    assert f["machine_name"] == "新機種"
    assert value(f, "平均差枚") == 150
    assert f["period"]["start"] == "2026-09-10"
    assert "着席推薦なし" in f["subject_label"]
    assert "元データの取得時刻が未記録" in f["missing_information"]


def add_layout(db, *, verified="確認済み", known="2026-09-09", valid_to=None):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO hall_layout VALUES(1,'店舗A','1階','2026-09-09',?,?, 'official','https://example.com/map',?)", (valid_to, verified, known))
        for n in (101, 102):
            c.execute("INSERT INTO hall_layout_seat VALUES(1,?,'新機種','島A','列A',?,0,0,0)", (n, n-100))


@pytest.mark.parametrize("verification,known,valid_to", [
    ("未確認", "2026-09-09", None), ("確認済み", "2026-09-20", None),
    ("確認済み", "2026-09-09", "2026-09-13"),
])
def test_unverified_future_or_expired_map_does_not_produce_island_facts(db, verification, known, valid_to):
    add_layout(db, verified=verification, known=known, valid_to=valid_to)
    assert build(db, topic="layout")["evidence"] == []


def test_verified_island_uses_all_members_on_same_days_only(db):
    add_layout(db)
    f = build(db, topic="layout")["evidence"][0]
    assert value(f, "全台共通日数") == 2
    assert value(f, "島の全台共通日・平均差枚") == 50
    assert "登録済みの島" in f["subject_label"]
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM hall_day_seat WHERE seat_number=102 AND report_date='2026-09-10'")
    assert value(build(db, topic="layout")["evidence"][0], "全台共通日数") == 1


def test_new_unverified_map_does_not_revive_old_verified_map(db):
    add_layout(db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO hall_layout VALUES(2,'店舗A','1階','2026-09-12',NULL,'未確認','manual','', '2026-09-12')")
    assert build(db, topic="layout")["evidence"] == []


def test_registered_events_keep_original_decision_and_filter_halls_and_cutoff(db):
    f = evidence(hall="店舗A", event="公式記録名", start="2026-08-01", end="2026-09-13",
        target="2026-09-16", decision="参考止まり", metrics=[metric("基準点", 40, "点")])
    wrong_hall = {**deepcopy(f), "hall_name": "店舗B"}
    future = {**deepcopy(f), "period": {"start": "2026-09-01", "end": "2026-09-14"}}
    snap = build(db, topic="event", event_facts=[f, wrong_hall, future])
    assert len(snap["evidence"]) == 1
    assert snap["evidence"][0]["decision"] == "参考止まり"
    assert f["event_name"] == "公式記録名"


def test_cross_store_question_does_not_use_within_store_ranking(db):
    snap = build(db, question="他店と比べて優良店？")
    assert snap["evidence"] == []
    assert any("他店舗との同一日" in s for s in snap["missing_information"])


def test_fabricated_ai_prose_cannot_change_hall_decisions(db):
    class Client:
        def complete(self, messages, *, max_tokens):
            return '{"summary":"店舗Bの999番台は高設定"}'
    snap = build(db, topic="machine")
    before = deepcopy(snap)
    result = answer(snap, question="何を打つ？", client=Client())
    assert result["answer_status"] == "fallback"
    assert "999番台" not in result["summary"]
    assert snap == before


def test_hall_question_api_validates_scope_and_date_without_external_ai(db, monkeypatch):
    monkeypatch.setattr(ai_service, "HALL_REPORTS_DB", db)
    monkeypatch.setattr(ai_service, "_get_client", lambda: None)
    app = FastAPI()
    app.include_router(ai_router.router)
    with TestClient(app) as c:
        request = {"hall_name": "店舗A", "visit_date": "2026-09-14", "message": "月曜日は？"}
        data = c.post("/api/ai/hall_ask", json=request).json()
        assert data["topics"] == ["weekday"]
        assert "月曜日" in data["summary"]
        assert data["ai_changed_decision"] is False
        profile = c.get("/api/ai/hall_profile", params={"hall_name": "店舗A", "visit_date": "2026-09-14", "days": 90}).json()
        assert profile["topics"] == ["overview"]
        assert {f["dimension"] for f in profile["evidence"]} == {"weekday", "digit", "machine"}
        for changes in ({"hall_name": "全店舗"}, {"hall_name": " "}, {"visit_date": "bad"}, {"days": 5000}):
            assert c.post("/api/ai/hall_ask", json={**request, **changes}).status_code == 422


def test_event_question_uses_selected_hall_and_preserves_failed_quality_gate(db, monkeypatch):
    from api.routers import events
    seen = []
    def analysis(**kwargs):
        seen.append(kwargs)
        return {"visit_date": "2026-09-14", "history_start": "2026-08-01", "reference_date": "2026-09-13",
                "upcoming": [{"hall_name": "店舗A", "event_date": "2026-09-16", "event_name": "登録済みの予定",
                    "baseline_forecast": {"score": 40, "decision": "参考止まり"},
                    "quality_gate": {"passed": False, "blockers": ["実績不足"]}}]}
    monkeypatch.setattr(events, "get_event_analysis", analysis)
    monkeypatch.setattr(ai_service, "HALL_REPORTS_DB", db)
    monkeypatch.setattr(ai_service, "_get_client", lambda: None)
    result = ai_service.hall_question_result(hall_name="店舗A", target_date="2026-09-14", question="イベントは？")
    assert len(seen) == 1 and seen[0]["hall_name"] == "店舗A"
    assert "実績不足" in result["summary"]
    assert "参考止まり" in result["summary"]
    assert result["ai_changed_decision"] is False
