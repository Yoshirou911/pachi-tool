"""みんレポの前日・翌日リンクを使う、再開可能な過去データ収集器。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scraper import minrepo

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


KNOWN_SEEDS = {
    "ニコニコ住道店": "https://min-repo.com/3147225/",
    "ベガスベガス大東店": "https://min-repo.com/3148366/",
    "スーパーコスモプレミアム大東店": "https://min-repo.com/3203873/",
}

_RUN_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    minrepo.init_db().close()
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS archive_collection_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hall_name TEXT NOT NULL,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                seed_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                max_pages INTEGER NOT NULL DEFAULT 120,
                discovered INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                machine_rows INTEGER NOT NULL DEFAULT 0,
                seat_rows INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS archive_collection_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                report_date TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                machine_rows INTEGER NOT NULL DEFAULT 0,
                seat_rows INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(job_id, source_url),
                FOREIGN KEY(job_id) REFERENCES archive_collection_job(id)
            );
            CREATE INDEX IF NOT EXISTS idx_archive_queue_job_status
            ON archive_collection_queue(job_id, status, id);
        """)


def validate_report_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "min-repo.com":
        raise ValueError("取得元URLは https://min-repo.com/数字/ のみ指定できます")
    match = re.fullmatch(r"/(\d+)/?", parsed.path)
    if not match or parsed.query or parsed.fragment:
        raise ValueError("取得元URLは https://min-repo.com/数字/ の形式で指定してください")
    return f"https://min-repo.com/{match.group(1)}/"


def _date_from_page(soup: BeautifulSoup, date_from: str, date_to: str) -> str:
    # レポートは翌日に公開されることがある。月日は見出しを正とし、
    # 公開メタデータは見出しに年がない場合の「年候補」にだけ使う。
    metadata_dates: list[date] = []
    for tag in soup.find_all("time"):
        raw = tag.get("datetime") or tag.get_text(" ", strip=True)
        match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", raw or "")
        if match:
            try:
                metadata_dates.append(date(*map(int, match.groups())))
            except ValueError:
                pass
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        for match in re.finditer(r'"datePublished"\s*:\s*"(20\d{2})-(\d{2})-(\d{2})', script.string or ""):
            try:
                metadata_dates.append(date(*map(int, match.groups())))
            except ValueError:
                pass

    heading = soup.find("h1")
    text = heading.get_text(" ", strip=True) if heading else ""
    full = re.search(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})", text)
    if full:
        return f"{int(full.group(1)):04d}-{int(full.group(2)):02d}-{int(full.group(3)):02d}"
    short = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
    if not short:
        return ""
    month, day = map(int, short.groups())
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    midpoint = start.toordinal() + (end.toordinal() - start.toordinal()) / 2
    candidates = []
    candidate_years = set(range(start.year - 1, end.year + 2))
    candidate_years.update(item.year for item in metadata_dates)
    for year in candidate_years:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        # 指定範囲内の候補を優先し、次に範囲中央へ近い年を選ぶ。
        in_range = start <= candidate <= end
        metadata_distance = min(
            (abs(candidate.toordinal() - item.toordinal()) for item in metadata_dates),
            default=10_000,
        )
        candidates.append((0 if in_range else 1, metadata_distance, abs(candidate.toordinal() - midpoint), candidate))
    return min(candidates)[3].isoformat() if candidates else ""


def inspect_report(html: str, expected_hall: str, date_from: str, date_to: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.find("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    if minrepo._normalize_hall_name(expected_hall) not in minrepo._normalize_hall_name(heading_text):
        raise ValueError(f"店舗名が一致しません: {heading_text or '見出しなし'}")
    report_date = _date_from_page(soup, date_from, date_to)
    if not report_date:
        raise ValueError("レポートの日付を確認できません")
    neighbors: list[str] = []
    previous_urls: list[str] = []
    next_urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        label = re.sub(r"\s+", "", anchor.get_text(" ", strip=True))
        if "前日" not in label and "翌日" not in label:
            continue
        try:
            neighbor = validate_report_url(urljoin(minrepo.BASE_URL, anchor["href"]))
        except ValueError:
            continue
        if neighbor not in neighbors:
            neighbors.append(neighbor)
        target = previous_urls if "前日" in label else next_urls
        if neighbor not in target:
            target.append(neighbor)
    return {
        "report_date": report_date,
        "neighbors": neighbors,
        "previous_urls": previous_urls,
        "next_urls": next_urls,
        "heading": heading_text,
    }


def create_job(hall_name: str, date_from: str, date_to: str, seed_url: str = "", max_pages: int = 120) -> int:
    init_db()
    if hall_name not in KNOWN_SEEDS and not seed_url:
        raise ValueError("この店舗は起点URLが未登録です。みんレポのレポートURLを指定してください")
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if start > end:
        raise ValueError("開始日は終了日以前にしてください")
    if (end - start).days > 730:
        raise ValueError("一度に収集できる期間は最大2年です")
    if not 1 <= max_pages <= 800:
        raise ValueError("最大ページ数は1〜800で指定してください")
    seed = validate_report_url(seed_url or KNOWN_SEEDS[hall_name])
    with _connect() as conn:
        active = conn.execute(
            "SELECT id FROM archive_collection_job WHERE status IN ('queued','collecting','paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            raise RuntimeError("進行中または一時停止中の収集があります")
        cur = conn.execute(
            """INSERT INTO archive_collection_job
               (hall_name,date_from,date_to,seed_url,status,max_pages,discovered)
               VALUES (?,?,?,?, 'queued', ?, 1)""",
            (hall_name, date_from, date_to, seed, max_pages),
        )
        job_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO archive_collection_queue(job_id,source_url) VALUES (?,?)",
            (job_id, seed),
        )
    return job_id


def _queue_neighbors(conn: sqlite3.Connection, job: sqlite3.Row, urls: list[str]) -> None:
    current = conn.execute(
        "SELECT COUNT(*) FROM archive_collection_queue WHERE job_id=?", (job["id"],)
    ).fetchone()[0]
    for url in urls:
        if current >= job["max_pages"]:
            break
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO archive_collection_queue(job_id,source_url) VALUES (?,?)",
            (job["id"], url),
        )
        current += conn.total_changes - before
    conn.execute(
        "UPDATE archive_collection_job SET discovered=?,updated_at=datetime('now','localtime') WHERE id=?",
        (current, job["id"]),
    )


def run_job(job_id: int) -> None:
    if not _RUN_LOCK.acquire(blocking=False):
        return
    try:
        init_db()
        with _connect() as conn:
            conn.execute(
                "UPDATE archive_collection_queue SET status='pending' WHERE job_id=? AND status='running'",
                (job_id,),
            )
            conn.execute(
                """UPDATE archive_collection_job SET status='collecting',pause_requested=0,error='',
                   updated_at=datetime('now','localtime') WHERE id=?""",
                (job_id,),
            )
        while True:
            with _connect() as conn:
                job = conn.execute("SELECT * FROM archive_collection_job WHERE id=?", (job_id,)).fetchone()
                if not job:
                    return
                if job["pause_requested"]:
                    conn.execute(
                        "UPDATE archive_collection_job SET status='paused',updated_at=datetime('now','localtime') WHERE id=?",
                        (job_id,),
                    )
                    return
                item = conn.execute(
                    "SELECT * FROM archive_collection_queue WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1",
                    (job_id,),
                ).fetchone()
                if not item:
                    conn.execute(
                        """UPDATE archive_collection_job SET status='completed',finished_at=datetime('now','localtime'),
                           updated_at=datetime('now','localtime') WHERE id=?""",
                        (job_id,),
                    )
                    return
                conn.execute(
                    "UPDATE archive_collection_queue SET status='running',attempts=attempts+1,updated_at=datetime('now','localtime') WHERE id=?",
                    (item["id"],),
                )

            try:
                response = minrepo._get_page(item["source_url"])
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                info = inspect_report(response.text, job["hall_name"], job["date_from"], job["date_to"])
                report_date = info["report_date"]
                with _connect() as conn:
                    fresh_job = conn.execute("SELECT * FROM archive_collection_job WHERE id=?", (job_id,)).fetchone()
                    if report_date > job["date_to"]:
                        next_pages = info["previous_urls"]
                    elif report_date < job["date_from"]:
                        next_pages = info["next_urls"]
                    else:
                        next_pages = info["neighbors"]
                    _queue_neighbors(conn, fresh_job, next_pages)
                    in_range = job["date_from"] <= report_date <= job["date_to"]
                    machine_rows = seat_rows = 0
                    status = "out_of_range"
                    if in_range:
                        result = minrepo.save_report_html(
                            response.text,
                            item["source_url"],
                            job["hall_name"],
                            report_date,
                            conn,
                            preserve_existing=True,
                        )
                        if not result["valid"]:
                            raise ValueError("レポートを検証できません")
                        machine_rows = int(result["machine_rows"])
                        seat_rows = int(result["seat_rows"])
                        status = "done"
                    conn.execute(
                        """UPDATE archive_collection_queue SET report_date=?,status=?,machine_rows=?,seat_rows=?,error='',
                           updated_at=datetime('now','localtime') WHERE id=?""",
                        (report_date, status, machine_rows, seat_rows, item["id"]),
                    )
                    conn.execute(
                        """UPDATE archive_collection_job SET processed=processed+1,machine_rows=machine_rows+?,
                           seat_rows=seat_rows+?,updated_at=datetime('now','localtime') WHERE id=?""",
                        (machine_rows, seat_rows, job_id),
                    )
            except Exception as exc:
                with _connect() as conn:
                    conn.execute(
                        """UPDATE archive_collection_queue SET status='failed',error=?,updated_at=datetime('now','localtime')
                           WHERE id=?""",
                        (str(exc)[:300], item["id"]),
                    )
                    conn.execute(
                        """UPDATE archive_collection_job SET processed=processed+1,failed_count=failed_count+1,error=?,
                           updated_at=datetime('now','localtime') WHERE id=?""",
                        (str(exc)[:300], job_id),
                    )
            time.sleep(minrepo.REQUEST_DELAY)
    finally:
        _RUN_LOCK.release()


def launch_job(job_id: int) -> bool:
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        previous_worker = _WORKER

        def _start_after_previous() -> None:
            previous_worker.join(timeout=30)
            run_job(job_id)

        _WORKER = threading.Thread(
            target=_start_after_previous,
            daemon=True,
            name="minrepo-archive-resume",
        )
        _WORKER.start()
        return True
    _WORKER = threading.Thread(target=run_job, args=(job_id,), daemon=True, name="minrepo-archive")
    _WORKER.start()
    return True


def pause_latest() -> bool:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM archive_collection_job WHERE status IN ('queued','collecting') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE archive_collection_job SET pause_requested=1,updated_at=datetime('now','localtime') WHERE id=?",
            (row["id"],),
        )
        return True


def resume_latest() -> int | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM archive_collection_job WHERE status='paused' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        job_id = int(row["id"])
        conn.execute(
            "UPDATE archive_collection_job SET status='queued',pause_requested=0,error='',updated_at=datetime('now','localtime') WHERE id=?",
            (job_id,),
        )
    launch_job(job_id)
    return job_id


def recover_interrupted_jobs() -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE archive_collection_queue SET status='pending' WHERE status='running'")
        conn.execute(
            """UPDATE archive_collection_job SET status='paused',pause_requested=0,
               error='アプリ終了により一時停止しました。再開できます。',updated_at=datetime('now','localtime')
               WHERE status IN ('queued','collecting')"""
        )


def get_status() -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM archive_collection_job ORDER BY id DESC LIMIT 1").fetchone()
        job = dict(row) if row else None
        if job:
            job["progress_pct"] = round(job["processed"] / max(job["discovered"], 1) * 100, 1)
            queue = conn.execute(
                "SELECT status,COUNT(*) AS count FROM archive_collection_queue WHERE job_id=? GROUP BY status",
                (job["id"],),
            ).fetchall()
            job["queue"] = {item["status"]: item["count"] for item in queue}
        coverage = []
        for hall_name in KNOWN_SEEDS:
            stats = conn.execute(
                """SELECT COUNT(DISTINCT report_date) AS days,MIN(report_date) AS oldest,MAX(report_date) AS newest
                   FROM hall_day_machine WHERE hall_name=? AND machine_name!='_NODATA_'""",
                (hall_name,),
            ).fetchone()
            coverage.append({"hall_name": hall_name, **dict(stats)})
    return {
        "job": job,
        "supported_halls": [{"hall_name": hall, "seed_url": seed} for hall, seed in KNOWN_SEEDS.items()],
        "coverage": coverage,
        "worker_running": bool(_WORKER and _WORKER.is_alive()),
    }
