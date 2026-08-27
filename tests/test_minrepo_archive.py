from __future__ import annotations

import sqlite3

import pytest

from scraper import minrepo, minrepo_archive


def _report_html(hall: str, report_date: str, neighbor: str = "", published_date: str | None = None) -> str:
    link = f'<a href="{neighbor}">前日のレポート</a>' if neighbor else ""
    published_date = published_date or report_date
    return f"""
    <html><head><script type="application/ld+json">
    {{"datePublished":"{published_date}T08:00:00+09:00"}}
    </script></head><body>
      <h1>{hall} {report_date} 結果</h1>{link}
      <table>
        <tr><th>機種</th><th>平均差枚</th><th>平均G数</th><th>勝率</th><th>出率</th></tr>
        <tr><td>Lスマスロ北斗の拳 (10台)</td><td>+500</td><td>7000</td><td>6/10</td><td>102.4%</td></tr>
      </table>
    </body></html>
    """


@pytest.fixture()
def archive_db(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(minrepo, "DB_PATH", db_path)
    monkeypatch.setattr(minrepo_archive, "DB_PATH", db_path)
    monkeypatch.setattr(minrepo, "REQUEST_DELAY", 0)
    minrepo_archive.init_db()
    return db_path


def test_validate_report_url_rejects_other_hosts():
    assert minrepo_archive.validate_report_url("https://min-repo.com/123/") == "https://min-repo.com/123/"
    with pytest.raises(ValueError):
        minrepo_archive.validate_report_url("https://example.com/123/")
    with pytest.raises(ValueError):
        minrepo_archive.validate_report_url("https://min-repo.com/tag/shop/")


def test_report_heading_date_wins_over_next_day_publish_date():
    html = _report_html("ニコニコ住道店", "2026-06-05", published_date="2026-06-06")
    html = html.replace("ニコニコ住道店 2026-06-05 結果", "6/5(金) ニコニコ住道店")
    info = minrepo_archive.inspect_report(html, "ニコニコ住道店", "2025-01-01", "2026-08-09")
    assert info["report_date"] == "2026-06-05"


def test_archive_job_follows_neighbor_and_persists(archive_db, monkeypatch):
    hall = "ニコニコ住道店"
    pages = {
        "https://min-repo.com/100/": _report_html(hall, "2026-06-05", "/99/"),
        "https://min-repo.com/99/": _report_html(hall, "2026-06-04"),
    }

    class Response:
        status_code = 200

        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(minrepo, "_get_page", lambda url: Response(pages[url]))
    job_id = minrepo_archive.create_job(
        hall,
        "2026-06-04",
        "2026-06-05",
        "https://min-repo.com/100/",
        max_pages=2,
    )
    minrepo_archive.run_job(job_id)

    status = minrepo_archive.get_status()
    assert status["job"]["status"] == "completed"
    assert status["job"]["processed"] == 2
    assert status["job"]["discovered"] == 2
    assert status["job"]["machine_rows"] == 2
    with sqlite3.connect(archive_db) as conn:
        rows = conn.execute(
            "SELECT report_date,avg_diff_coins FROM hall_day_machine ORDER BY report_date"
        ).fetchall()
    assert rows == [("2026-06-04", 500), ("2026-06-05", 500)]


def test_recover_interrupted_job_makes_it_resumable(archive_db):
    job_id = minrepo_archive.create_job(
        "ニコニコ住道店",
        "2026-01-01",
        "2026-01-02",
        "https://min-repo.com/100/",
        max_pages=2,
    )
    with sqlite3.connect(archive_db) as conn:
        conn.execute("UPDATE archive_collection_job SET status='collecting' WHERE id=?", (job_id,))
        conn.execute("UPDATE archive_collection_queue SET status='running' WHERE job_id=?", (job_id,))
    minrepo_archive.recover_interrupted_jobs()
    status = minrepo_archive.get_status()
    assert status["job"]["status"] == "paused"
    assert status["job"]["queue"]["pending"] == 1


def test_archive_item_retries_temporary_failure(archive_db, monkeypatch):
    hall = "ニコニコ住道店"

    class Response:
        status_code = 200

        def __init__(self, text):
            self.text = text

    calls = {"count": 0}

    def flaky_page(_url):
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("temporary")
        return Response(_report_html(hall, "2026-06-05"))

    monkeypatch.setattr(minrepo, "_get_page", flaky_page)
    job_id = minrepo_archive.create_job(
        hall, "2026-06-05", "2026-06-05", "https://min-repo.com/100/", max_pages=1
    )

    minrepo_archive.run_job(job_id)

    status = minrepo_archive.get_status()["job"]
    assert calls["count"] == 3
    assert status["status"] == "completed"
    assert status["failed_count"] == 0
    assert status["processed"] == 1


def test_resume_requeues_failed_completed_job(archive_db, monkeypatch):
    job_id = minrepo_archive.create_job(
        "ニコニコ住道店",
        "2026-06-05",
        "2026-06-05",
        "https://min-repo.com/100/",
        max_pages=1,
    )
    with sqlite3.connect(archive_db) as conn:
        conn.execute(
            "UPDATE archive_collection_job SET status='completed',processed=1,failed_count=1 WHERE id=?",
            (job_id,),
        )
        conn.execute(
            "UPDATE archive_collection_queue SET status='failed',attempts=3,error='temporary' WHERE job_id=?",
            (job_id,),
        )
    launched = []
    monkeypatch.setattr(minrepo_archive, "launch_job", lambda value: launched.append(value) or True)

    resumed = minrepo_archive.resume_latest()

    assert resumed == job_id
    assert launched == [job_id]
    with sqlite3.connect(archive_db) as conn:
        job = conn.execute(
            "SELECT status,processed,failed_count FROM archive_collection_job WHERE id=?", (job_id,)
        ).fetchone()
        queue = conn.execute(
            "SELECT status,attempts,error FROM archive_collection_queue WHERE job_id=?", (job_id,)
        ).fetchone()
    assert job == ("queued", 0, 0)
    assert queue == ("pending", 0, "")
