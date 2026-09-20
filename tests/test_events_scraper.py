import json
import sqlite3

from scraper import events, minrepo


def test_minrepo_events_reject_missing_hall_page(monkeypatch):
    monkeypatch.setattr(minrepo, "fetch_report_links", lambda *_args, **_kwargs: [])
    assert events.scrape_minrepo_events("存在しない店舗") == []


def test_station_halls_have_direct_pworld_urls():
    assert "kicona-shijonawate" in events._pworld_search("キコーナ四條畷店")
    assert "himawarisijounawate" in events._pworld_search("ひま・わり四條畷店")


def test_all_active_shijonawate_halls_have_public_event_pages():
    expected = {
        "キコーナ四條畷店", "ひま・わり四條畷店", "キコーナ野崎店",
        "ニコニコ住道店", "キコーナ大東店", "マルハン大東店",
        "スーパーコスモプレミアム大東店", "ベガスベガス大東店",
    }
    assert expected <= set(events.SLOMAP_HALL_URLS)


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


def _next_flight_html(payload: str) -> str:
    return f"<script>self.__next_f.push({json.dumps([1, payload], ensure_ascii=False)})</script>"


def test_slomap_public_data_separates_hall_totals_from_schedule_only():
    payload = (
        '0:{"dailyMedalDiff":{"2026-08-25":{"avg_medal_diff":123,'
        '"total_medal_diff":12300,"total_machines":100}}}'
        '\n1:{"events":[{"event_date":"2026-08-25","store_name":"検証店",'
        '"syuzai_name":"取材A","syuzai_rank":"B","media_name":"媒体"},'
        '{"event_date":"2026-08-26","store_name":"検証店",'
        '"syuzai_name":"取材B","syuzai_rank":"C","media_name":"媒体"}]}'
    )
    html = _next_flight_html(payload)
    days = events.parse_slomap_day_summaries(html, "検証店", "https://example.com")
    evidence = events.parse_slomap_event_evidence(
        html, "検証店", "https://example.com", days
    )

    assert days[0]["avg_diff_coins"] == 123
    assert days[0]["evidence_scope"] == "hall_all_reported_units"
    assert evidence[0]["analysis_eligible"] == 1
    assert evidence[0]["avg_diff_coins"] == 123
    assert evidence[1]["analysis_eligible"] == 0
    assert evidence[1]["evidence_scope"] == "schedule_only"


def test_slomap_public_data_rejects_hall_wide_masked_zero_rows():
    values = ",".join(
        f'"2026-08-{day:02d}":{{"avg_medal_diff":0,"total_medal_diff":0,"total_machines":100}}'
        for day in range(1, 21)
    )
    html = _next_flight_html(f'0:{{"dailyMedalDiff":{{{values}}}}}')
    assert events.parse_slomap_day_summaries(
        html, "マスク店", "https://example.com"
    ) == []


def test_event_migration_quarantines_report_days_without_deleting_them(tmp_path):
    database = tmp_path / "events.db"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE hall_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_type TEXT,
            event_title TEXT,
            source TEXT,
            source_url TEXT,
            created_at TEXT
        );
        INSERT INTO hall_event
          (hall_name,event_date,event_type,event_title,source,source_url)
        VALUES
          ('検証店','2026-08-01','通常イベント','みんレポ記録日（8/1）','minrepo','https://min-repo.com/1'),
          ('検証店','2026-08-03','その他','取材予定','slomap','https://slo-map.com/halls/1');
        """
    )

    events.init_event_db(conn)
    rows = conn.execute(
        """SELECT event_title,event_kind,source_trust,prediction_eligible
             FROM hall_event ORDER BY event_date"""
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0] == ("みんレポ記録日（8/1）", "report_day", "対象外", 0)
    assert rows[1] == ("取材予定", "media_schedule", "C", 1)


def test_event_source_classification_is_safe_by_default():
    assert events.classify_event_record("pworld", "新台入替")["source_trust"] == "A"
    assert events.classify_event_record("google", "イベント候補")["prediction_eligible"] == 0
