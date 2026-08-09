from scraper import minrepo


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
