from scraper import events, minrepo


def test_minrepo_events_reject_missing_hall_page(monkeypatch):
    monkeypatch.setattr(minrepo, "fetch_report_links", lambda *_args, **_kwargs: [])
    assert events.scrape_minrepo_events("存在しない店舗") == []


def test_station_halls_have_direct_pworld_urls():
    assert "kicona-shijonawate" in events._pworld_search("キコーナ四條畷店")
    assert "himawarisijounawate" in events._pworld_search("ひま・わり四條畷店")
