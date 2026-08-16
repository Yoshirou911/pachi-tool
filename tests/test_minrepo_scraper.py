from scraper import minrepo


def test_hall_name_normalizer_matches_matsumoto_alias():
    assert minrepo._normalize_hall_name("ラッシュMATSUMOTO#59") == minrepo._normalize_hall_name("ラッシュ松本#59")


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _Cookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **_kwargs):
        self.values[name] = value


class _ChallengeSession:
    def __init__(self):
        self.calls = 0
        self.cookies = _Cookies()

    def get(self, _url, timeout=15):
        self.calls += 1
        if self.calls == 1:
            return _Response("<script>$.cookie('_d2', 'token123', { path: '/' });</script>")
        return _Response("<html><h1>取得成功</h1></html>")


def test_cookie_challenge_is_retried(monkeypatch):
    session = _ChallengeSession()
    monkeypatch.setattr(minrepo, "_SESSION", session)
    response = minrepo._get_page("https://min-repo.com/example/")
    assert "取得成功" in response.text
    assert session.calls == 2
    assert session.cookies.values["_d2"] == "token123"


def test_report_links_are_unique_by_date(monkeypatch):
    html = """
      <a href="/100/">8/8(土)</a>
      <a href="/101/">8/8(土)</a>
      <a href="/99/">8/7(金)</a>
    """
    monkeypatch.setattr(minrepo, "_get_page", lambda _url: _Response(html))
    links = minrepo.fetch_report_links("https://min-repo.com/tag/example/", max_pages=1)
    assert links == [
        ("8/8(土)", "https://min-repo.com/100/"),
        ("8/7(金)", "https://min-repo.com/99/"),
    ]


def test_missing_hall_tag_does_not_use_global_fallback(monkeypatch):
    html = '<h1>別店舗</h1><a href="/100/">8/8(土)</a>'
    monkeypatch.setattr(minrepo, "_get_page", lambda _url: _Response(html))
    links = minrepo.fetch_report_links(
        "https://min-repo.com/tag/missing/",
        max_pages=1,
        expected_hall_name="対象店舗",
    )
    assert links == []


def test_special_hall_slug():
    assert minrepo.build_tag_url("スーパーコスモプレミアム大東店").endswith(
        "/tag/super-cosmo-premium-%E5%A4%A7%E6%9D%B1%E5%BA%97/"
    )


def test_report_parser_reads_all_horizontal_machine_and_seat_groups():
    html = """
      <table>
        <tr>
          <th>機種</th><th>平均差枚</th><th>平均G数</th><th>勝率</th><th>出率</th>
          <th>機種</th><th>平均差枚</th><th>平均G数</th><th>勝率</th><th>出率</th>
        </tr>
        <tr>
          <td>Lスマスロ北斗の拳</td><td>+1,200</td><td>6,000</td><td>2/3</td><td>106.7%</td>
          <td>スマスロ モンキーターンV</td><td>-300</td><td>4,500</td><td>1/2</td><td>97.8%</td>
        </tr>
      </table>
      <table>
        <tr>
          <th>機種</th><th>台番</th><th>差枚</th><th>G数</th><th>出率</th>
          <th>機種</th><th>台番</th><th>差枚</th><th>G数</th><th>出率</th>
        </tr>
        <tr>
          <td>Lスマスロ北斗の拳</td><td>101</td><td>+2,000</td><td>7,000</td><td>109.5%</td>
          <td>スマスロ モンキーターンV</td><td>201</td><td>-500</td><td>3,000</td><td>94.4%</td>
        </tr>
      </table>
    """

    machines, seats = minrepo.parse_report_page(html, "https://min-repo.com/1/")

    assert [row["machine_name"] for row in machines] == [
        "Lスマスロ北斗の拳",
        "スマスロ モンキーターンV",
    ]
    assert [row["unit_count"] for row in machines] == [3, 2]
    assert [row["avg_diff_coins"] for row in machines] == [1200, -300]
    assert [row["seat_number"] for row in seats] == [101, 201]
    assert [row["diff_coins"] for row in seats] == [2000, -500]


def test_report_parser_reads_unit_count_without_dai_suffix():
    html = """
      <table>
        <tr><th>機種</th><th>平均差枚</th><th>平均G数</th><th>出率</th></tr>
        <tr><td>L東京喰種 (13)</td><td>+500</td><td>5,000</td><td>103.3%</td></tr>
      </table>
    """

    machines, _ = minrepo.parse_report_page(html, "https://min-repo.com/2/")

    assert machines[0]["machine_name"] == "L東京喰種"
    assert machines[0]["unit_count"] == 13


def test_all_zero_placeholder_table_is_not_meaningful_performance():
    rows = [
        {"avg_diff_coins": 0, "win_rate_pct": None, "ev_pct": 100.0},
        {"avg_diff_coins": 0, "win_rate_pct": None, "ev_pct": 100.0},
    ]
    assert minrepo._has_meaningful_performance(rows) is False
    rows[0]["avg_diff_coins"] = 1
    assert minrepo._has_meaningful_performance(rows) is True
