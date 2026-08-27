from scraper import pachireview


def test_discovers_published_months_and_daily_links():
    html = """
      <button>2026年8月</button><button>2026年7月</button><span>2025年20月</span>
      <a href="/shops/osaka/daitoushi/4340/data/20260823/">day</a>
      <a href="/shops/osaka/daitoushi/4340/data/20260823/L-machine/">machine</a>
    """
    assert pachireview.discover_months(html) == ["202608", "202607"]
    assert pachireview.discover_daily_links(
        html, "https://pachireview.com/shops/osaka/daitoushi/4340/data/"
    ) == ["https://pachireview.com/shops/osaka/daitoushi/4340/data/20260823/"]


def test_parses_supported_machine_grid():
    html = """
      <div class="shop-machine-grid">
        <div>MODEL / 機種名</div><div>AVG</div><div>TOTAL</div>
        <div>WIN</div><div>PAYOUT</div><div>GAMES</div>
      </div>
      <div class="shop-machine-grid">
        <a>スマスロモンキーターンV</a><div>+190.9 枚 / 台</div>
        <div>+2,100 枚</div><div>27.3% 3/11</div><div>103.1%</div><div>2054G</div>
      </div>
    """
    report_date, rows = pachireview.parse_daily_page(
        html, "https://pachireview.com/shops/osaka/daitoushi/4340/data/20260823/"
    )
    assert report_date == "2026-08-23"
    assert rows == [{
        "report_date": "2026-08-23",
        "machine_name": "スマスロモンキーターンV",
        "unit_count": 11,
        "avg_diff_coins": 191,
        "total_diff_coins": 2100,
        "win_rate_pct": 27.3,
        "ev_pct": 103.1,
        "avg_games": 2054,
        "source_url": "https://pachireview.com/shops/osaka/daitoushi/4340/data/20260823/",
    }]


def test_refresh_due_without_saved_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(pachireview, "DB_PATH", tmp_path / "reports.db")
    assert pachireview.is_refresh_due()
