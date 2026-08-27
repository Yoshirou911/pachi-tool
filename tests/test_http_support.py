from pathlib import Path

from scraper import http_support


def test_curl_ca_bundle_copies_non_ascii_cert_path(tmp_path, monkeypatch):
    source_dir = tmp_path / "日本語"
    source_dir.mkdir()
    source = source_dir / "cacert.pem"
    source.write_text("test-ca", encoding="utf-8")
    target_dir = tmp_path / "ascii-temp"
    target_dir.mkdir()

    monkeypatch.setattr(http_support.certifi, "where", lambda: str(source))
    monkeypatch.setattr(http_support.tempfile, "gettempdir", lambda: str(target_dir))

    result = Path(http_support.curl_ca_bundle())

    assert result == target_dir / "pachi-tool-cacert.pem"
    assert result.read_text(encoding="utf-8") == "test-ca"
