"""手動収集の店舗・都道府県解決テスト。"""
from api.routers import scrape


def test_resolve_hall_list_keeps_registered_prefecture(monkeypatch):
    monkeypatch.setattr(
        scrape.scheduler,
        "_get_active_halls",
        lambda: [
            {"hall_name": "大阪店", "prefecture": "大阪府"},
            {"hall_name": "松本店", "prefecture": "長野県"},
        ],
    )

    assert scrape._resolve_hall_list(" 松本店,大阪店 ") == [
        {"hall_name": "松本店", "prefecture": "長野県"},
        {"hall_name": "大阪店", "prefecture": "大阪府"},
    ]


def test_resolve_hall_list_defaults_unknown_hall_to_osaka(monkeypatch):
    monkeypatch.setattr(scrape.scheduler, "_get_active_halls", lambda: [])
    assert scrape._resolve_hall_list("未登録店") == [
        {"hall_name": "未登録店", "prefecture": "大阪府"}
    ]
