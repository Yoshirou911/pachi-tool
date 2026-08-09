from scraper import events, minrepo


def test_minrepo_events_reject_missing_hall_page(monkeypatch):
    monkeypatch.setattr(minrepo, "fetch_report_links", lambda *_args, **_kwargs: [])
    assert events.scrape_minrepo_events("存在しない店舗") == []


def test_station_halls_have_direct_pworld_urls():
    assert "kicona-shijonawate" in events._pworld_search("キコーナ四條畷店")
    assert "himawarisijounawate" in events._pworld_search("ひま・わり四條畷店")


def test_slomap_parser_reads_only_schema_events():
    html = """
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"LocalBusiness","name":"キコーナ四條畷店"}
    </script>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Event",
       "name":"パチスロの党取材改 - キコーナ四條畷店",
       "startDate":"2026-08-13",
       "location":{"@type":"Place","name":"キコーナ四條畷店"},
       "description":"取材イベント。ランク: B。"}
    </script>
    """
    found = events.parse_slomap_events(
        html,
        "キコーナ四條畷店",
        "https://slo-map.com/halls/7964",
    )
    assert found == [{
        "hall_name": "キコーナ四條畷店",
        "event_date": "2026-08-13",
        "event_type": "その他",
        "event_title": "パチスロの党取材改（Bランク）",
        "source": "slomap",
        "source_url": "https://slo-map.com/halls/7964",
    }]


def test_slomap_parser_rejects_other_hall_and_bad_dates():
    html = """
    <script type="application/ld+json">[
      {"@type":"Event","name":"別店舗の予定","startDate":"2026-08-13",
       "location":{"name":"別店舗"}},
      {"@type":"Event","name":"日付不明","startDate":"未定"}
    ]</script>
    """
    assert events.parse_slomap_events(html, "キコーナ四條畷店", "https://example.com") == []
