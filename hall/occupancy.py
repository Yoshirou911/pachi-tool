"""
店舗稼働率トラッキング（ワンタップ手動記録）。
DB ファイル: data/hall_reports.db（scraper/anaslo.py の scrape_hall_config /
hall_day_seat と同じファイル。hall_name 文字列キーの既存慣習に合わせる）

テーブル:
  hall_occupancy_records - 1回の巡回記録（高/中/低 + 任意の平均回転数）

設計方針:
  - recorded_at (ISO8601) を正とする。weekday/time_bucket はそこから導出した
    冗長列で、店舗×曜日×時間帯の集計を後から素直にGROUP BYできるようにする
    ためのもの。集計ロジック自体は今回実装しない（手動記録が溜まってから着手）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator, Optional

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"

LEVELS = ("high", "mid", "low")

# 巡回優先度: 直近レベルが high ほど「まだ熱いうちに再訪する価値がある」として
# 経過時間を大きめに評価し、low ほど「しばらく様子見でよい」として小さめに評価する。
_LEVEL_URGENCY = {"high": 1.4, "mid": 1.0, "low": 0.6}
_NEVER_RECORDED_URGENCY = 1.2  # 記録が一度も無いホールは「未知」としてやや優先
_LEVEL_VALUE = {"high": 0.85, "mid": 0.58, "low": 0.25}
_LEVEL_LABEL = {"high": "高", "mid": "中", "low": "低"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hall_occupancy_records (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    hall_name                    TEXT    NOT NULL,
    level                        TEXT    NOT NULL CHECK(level IN ('high','mid','low')),
    avg_rotation_games_per_hour  REAL,
    recorded_at                  TEXT    NOT NULL,
    weekday                      INTEGER NOT NULL,
    time_bucket                  TEXT,
    created_at                   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_occ_hall_recorded  ON hall_occupancy_records(hall_name, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_occ_hall_weekday   ON hall_occupancy_records(hall_name, weekday, time_bucket);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> sqlite3.Connection:
    """他の hall_reports.db 初期化関数(scraper.anaslo.init_db等)と同じく、
    呼び出し元が close() する接続を返す(api.main._init_auxiliary_databases の規約に合わせる)。"""
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _time_bucket(dt: datetime) -> str:
    start = (dt.hour // 3) * 3
    end = (start + 3) % 24
    return f"{start:02d}-{end:02d}"


def _parse_recorded_at(recorded_at: Optional[str]) -> datetime:
    if not recorded_at:
        return datetime.now()
    try:
        return datetime.fromisoformat(recorded_at)
    except ValueError:
        raise ValueError(f"recorded_at の形式が不正です(ISO8601で指定してください): {recorded_at}")


def record_occupancy(
    hall_name: str,
    level: str,
    avg_rotation_games_per_hour: Optional[float] = None,
    recorded_at: Optional[str] = None,
) -> dict:
    """ワンタップ記録: 店舗の稼働状況(高/中/低)を1件保存する。"""
    if not hall_name:
        raise ValueError("hall_name は必須です")
    if level not in LEVELS:
        raise ValueError(f"level は {LEVELS} のいずれかで指定してください: {level}")

    dt = _parse_recorded_at(recorded_at)
    recorded_at_str = dt.isoformat(timespec="seconds")
    weekday = dt.weekday()  # 0=月曜 .. 6=日曜
    time_bucket = _time_bucket(dt)

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO hall_occupancy_records
               (hall_name, level, avg_rotation_games_per_hour, recorded_at, weekday, time_bucket)
               VALUES (?,?,?,?,?,?)""",
            (hall_name, level, avg_rotation_games_per_hour, recorded_at_str, weekday, time_bucket),
        )
        row = con.execute(
            "SELECT * FROM hall_occupancy_records WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def get_patrol_list(
    hall_names: Optional[list[str]] = None,
    prefecture: Optional[str] = None,
) -> list[dict]:
    """巡回優先度順のホール一覧を返す。

    母集団は scraper.anaslo.get_hall_configs(enabled_only=True) の有効ホール
    (hall_names を渡した場合はそれで絞り込む)。各ホールの最新の稼働記録を
    LEFT JOIN し、記録が無い/古いホールほど、また直近が high だったホールほど
    priority_score が大きくなるようにする(降順ソートで上位=優先訪問先)。
    """
    try:
        from scraper.anaslo import get_hall_configs
        halls = get_hall_configs(enabled_only=True)
    except Exception:
        halls = []

    if prefecture:
        halls = [h for h in halls if h.get("prefecture") == prefecture]

    if hall_names:
        wanted = set(hall_names)
        halls = [h for h in halls if h["hall_name"] in wanted] or [
            {"hall_name": n, "prefecture": None} for n in hall_names
        ]

    now = datetime.now()
    with _conn() as con:
        latest_by_hall: dict[str, sqlite3.Row] = {}
        rows = con.execute(
            """SELECT hall_name, level, recorded_at, avg_rotation_games_per_hour
               FROM hall_occupancy_records
               WHERE id IN (
                   SELECT MAX(id) FROM hall_occupancy_records GROUP BY hall_name
               )"""
        ).fetchall()
        for r in rows:
            latest_by_hall[r["hall_name"]] = r

    result = []
    for h in halls:
        name = h["hall_name"]
        latest = latest_by_hall.get(name)
        if latest is None:
            hours_since = None
            last_level = None
            last_recorded_at = None
            urgency = _NEVER_RECORDED_URGENCY
            priority_score = 24 * 30 * urgency  # 未記録は「30日相当」として十分優先度を高くする
        else:
            last_recorded_at = latest["recorded_at"]
            last_level = latest["level"]
            try:
                hours_since = max(0.0, (now - datetime.fromisoformat(last_recorded_at)).total_seconds() / 3600)
            except ValueError:
                hours_since = 0.0
            urgency = _LEVEL_URGENCY.get(last_level, 1.0)
            priority_score = hours_since * urgency

        result.append({
            "hall_name": name,
            "prefecture": h.get("prefecture"),
            "last_level": last_level,
            "last_recorded_at": last_recorded_at,
            "hours_since": round(hours_since, 1) if hours_since is not None else None,
            "priority_score": round(priority_score, 2),
        })

    result.sort(key=lambda r: r["priority_score"], reverse=True)
    return result


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _normalize_machine(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    text = re.sub(r"[\s　・･~〜～:：!！?？_\-‐]", "", text)
    return re.sub(r"^(スマスロ|パチスロ|lb?|l)", "", text)


def _supported_machine_names() -> set[str]:
    try:
        from config import ROOT
        payload = json.loads((ROOT / "data" / "opportunity_catalog.json").read_text(encoding="utf-8"))
        return {
            _normalize_machine(row.get("machine_name", ""))
            for row in payload.get("profiles", [])
            if row.get("machine_name")
        }
    except Exception:
        return set()


def _is_supported_machine(machine_name: str, supported: set[str]) -> bool:
    key = _normalize_machine(machine_name)
    if not key:
        return False
    return any(key == item or (min(len(key), len(item)) >= 5 and (key in item or item in key)) for item in supported)


def _parse_target_at(target_at: Optional[str]) -> datetime:
    if not target_at:
        return datetime.now()
    try:
        return datetime.fromisoformat(target_at)
    except ValueError as exc:
        raise ValueError("at は ISO8601 形式で指定してください") from exc


def _level_from_ratio(value: float) -> str:
    if value >= 0.72:
        return "high"
    if value >= 0.42:
        return "mid"
    return "low"


def get_occupancy_statistics(
    hall_name: str,
    target_at: Optional[str] = None,
    lookback_days: int = 90,
) -> dict:
    """店舗×曜日×3時間帯の手動記録から稼働の目安を返す。

    同条件が2件未満なら、その店舗の直近全記録へフォールバックする。
    数値は実測稼働率ではなく high/mid/low を連続値へ変換した参考推定。
    """
    if not hall_name.strip():
        raise ValueError("hall_name は必須です")
    target = _parse_target_at(target_at)
    bucket = _time_bucket(target)
    cutoff = target - timedelta(days=max(7, min(int(lookback_days), 365)))
    with _conn() as con:
        rows = con.execute(
            """SELECT level,avg_rotation_games_per_hour,recorded_at,weekday,time_bucket
               FROM hall_occupancy_records
               WHERE hall_name=? AND recorded_at>=? ORDER BY recorded_at DESC""",
            (hall_name, cutoff.isoformat(timespec="seconds")),
        ).fetchall()
    all_rows = [dict(row) for row in rows]
    matched = [row for row in all_rows if row["weekday"] == target.weekday() and row["time_bucket"] == bucket]
    selected = matched if len(matched) >= 2 else all_rows
    weighted_total = 0.0
    weight_sum = 0.0
    rotation_total = 0.0
    rotation_weight = 0.0
    for row in selected:
        try:
            observed = datetime.fromisoformat(row["recorded_at"])
            age_days = max(0.0, (target - observed).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 90.0
        weight = 1.0 / (1.0 + age_days / 30.0)
        weighted_total += _LEVEL_VALUE.get(row["level"], 0.5) * weight
        weight_sum += weight
        if row.get("avg_rotation_games_per_hour") is not None:
            rotation_total += float(row["avg_rotation_games_per_hour"]) * weight
            rotation_weight += weight
    ratio = weighted_total / weight_sum if weight_sum else None
    predicted_level = _level_from_ratio(ratio) if ratio is not None else None
    matching_count = len(matched)
    total_count = len(all_rows)
    if matching_count >= 8:
        confidence, confidence_label = "high", "十分"
    elif matching_count >= 3:
        confidence, confidence_label = "medium", "参考"
    elif total_count >= 5:
        confidence, confidence_label = "low", "全時間帯の参考"
    else:
        confidence, confidence_label = "insufficient", "不足"
    return {
        "hall_name": hall_name,
        "target_at": target.isoformat(timespec="minutes"),
        "weekday": target.weekday(),
        "time_bucket": bucket,
        "sample_count": len(selected),
        "matching_sample_count": matching_count,
        "total_sample_count": total_count,
        "predicted_level": predicted_level,
        "predicted_label": _LEVEL_LABEL.get(predicted_level, "データ不足"),
        "predicted_ratio": round(ratio, 3) if ratio is not None else None,
        "avg_rotation_games_per_hour": round(rotation_total / rotation_weight, 1) if rotation_weight else None,
        "confidence": confidence,
        "confidence_label": confidence_label,
        "used_fallback": len(matched) < 2 and bool(all_rows),
        "latest_record": all_rows[0] if all_rows else None,
        "notice": "高・中・低の手動記録から推定した混雑目安で、実際の空き台や期待値を保証しません",
    }


def _hall_machine_metrics(con: sqlite3.Connection, hall_name: str, supported: set[str]) -> dict:
    machines: list[str] = []
    snapshot_date = None
    if _table_exists(con, "hall_machine_snapshot"):
        latest = con.execute(
            "SELECT MAX(snapshot_date) FROM hall_machine_snapshot WHERE hall_name=?", (hall_name,)
        ).fetchone()[0]
        if latest:
            snapshot_date = latest
            machines = [row[0] for row in con.execute(
                "SELECT machine_name FROM hall_machine_snapshot WHERE hall_name=? AND snapshot_date=?",
                (hall_name, latest),
            ).fetchall()]
    if not machines:
        for table in ("hall_day_machine", "hall_day_seat"):
            if not _table_exists(con, table):
                continue
            rows = con.execute(
                f"""SELECT DISTINCT machine_name FROM {table}
                    WHERE hall_name=? AND machine_name!='_NODATA_'
                    ORDER BY report_date DESC LIMIT 200""",
                (hall_name,),
            ).fetchall()
            machines.extend(row[0] for row in rows if row[0])
    supported_names = sorted({name for name in machines if _is_supported_machine(name, supported)})

    avg_games = None
    performance_days = 0
    latest_date = None
    if _table_exists(con, "hall_day_machine"):
        columns = {row[1] for row in con.execute("PRAGMA table_info(hall_day_machine)").fetchall()}
        games_column = "avg_games" if "avg_games" in columns else None
        expression = f"AVG({games_column})" if games_column else "NULL"
        row = con.execute(
            f"""SELECT {expression},COUNT(DISTINCT report_date),MAX(report_date)
                 FROM hall_day_machine WHERE hall_name=? AND machine_name!='_NODATA_'""",
            (hall_name,),
        ).fetchone()
        avg_games, performance_days, latest_date = row[0], int(row[1] or 0), row[2]
    if avg_games is None and _table_exists(con, "hall_day_seat"):
        columns = {row[1] for row in con.execute("PRAGMA table_info(hall_day_seat)").fetchall()}
        expression = "AVG(games)" if "games" in columns else "NULL"
        row = con.execute(
            f"""SELECT {expression},COUNT(DISTINCT report_date),MAX(report_date)
                 FROM hall_day_seat WHERE hall_name=? AND machine_name!='_NODATA_'""",
            (hall_name,),
        ).fetchone()
        avg_games = row[0]
        performance_days = max(performance_days, int(row[1] or 0))
        latest_date = max(filter(None, [latest_date, row[2]]), default=None)
    return {
        "machine_count": len(set(machines)),
        "supported_machine_count": len(supported_names),
        "supported_machines": supported_names[:12],
        "snapshot_date": snapshot_date,
        "avg_daily_games": round(float(avg_games), 0) if avg_games is not None else None,
        "performance_days": performance_days,
        "latest_date": latest_date,
    }


def rank_hyena_halls(
    target_at: Optional[str] = None,
    hall_names: Optional[list[str]] = None,
    limit: int = 10,
    prefecture: Optional[str] = None,
) -> dict:
    """今から巡回する価値がある店舗を、説明可能な材料だけで順位付けする。"""
    target = _parse_target_at(target_at)
    try:
        from scraper.anaslo import get_hall_configs
        halls = get_hall_configs(enabled_only=True)
    except Exception:
        halls = []

    if prefecture:
        halls = [row for row in halls if row.get("prefecture") == prefecture]
    if hall_names:
        wanted = set(hall_names)
        halls = [row for row in halls if row.get("hall_name") in wanted]
        known = {row.get("hall_name") for row in halls}
        halls.extend({"hall_name": name, "prefecture": None} for name in hall_names if name not in known)
    supported = _supported_machine_names()
    results: list[dict] = []
    with _conn() as con:
        for hall in halls:
            name = hall.get("hall_name", "")
            if not name:
                continue
            stats = get_occupancy_statistics(name, target.isoformat(), 120)
            metrics = _hall_machine_metrics(con, name, supported)
            supported_count = metrics["supported_machine_count"]
            install_score = min(30.0, supported_count / 8.0 * 30.0)
            ratio = stats["predicted_ratio"]
            # 混雑未記録を「ほどよい稼働」とみなすと候補点が過大になる。
            # 未記録時は中立点だけを与え、現地記録が増えてから評価を上げる。
            occupancy_score = (
                max(0.0, 30.0 - abs(float(ratio) - 0.60) * 65.0)
                if ratio is not None else 8.0
            )
            avg_games = metrics["avg_daily_games"]
            activity_score = min(20.0, float(avg_games or 0) / 5500.0 * 20.0)
            if avg_games is None and metrics["performance_days"] >= 14:
                activity_score = 7.0
            latest_date = metrics["latest_date"] or metrics["snapshot_date"]
            age_days = None
            if latest_date:
                try:
                    age_days = max(0, (target.date() - date.fromisoformat(latest_date)).days)
                except ValueError:
                    pass
            freshness_score = 10.0 if age_days is not None and age_days <= 7 else 6.0 if age_days is not None and age_days <= 30 else 2.0 if latest_date else 0.0
            confidence_score = {"high": 10.0, "medium": 7.0, "low": 4.0, "insufficient": 0.0}[stats["confidence"]]
            score = install_score + occupancy_score + activity_score + freshness_score + confidence_score
            latest = stats.get("latest_record")
            if latest:
                try:
                    hours = max(0.0, (target - datetime.fromisoformat(latest["recorded_at"])).total_seconds() / 3600)
                except (TypeError, ValueError):
                    hours = 999
                if hours <= 6:
                    score += {"mid": 5.0, "high": -5.0, "low": -8.0}.get(latest.get("level"), 0.0)
            score = max(0, min(100, round(score)))
            if stats["confidence"] == "insufficient":
                score = min(score, 69)
            if score >= 70 and stats["confidence"] in {"medium", "high"}:
                verdict, verdict_label = "strong", "巡回候補"
            elif score >= 50:
                verdict, verdict_label = "candidate", "候補"
            elif score >= 35:
                verdict, verdict_label = "watch", "様子見"
            else:
                verdict, verdict_label = "insufficient", "データ不足"
            reasons = []
            if supported_count:
                reasons.append(f"対応スマスロ{supported_count}機種を確認")
            else:
                reasons.append("対応機種の設置確認が不足")
            if avg_games is not None:
                reasons.append(f"過去の平均稼働は約{int(avg_games):,}G")
            if stats["predicted_level"]:
                reasons.append(f"この曜日・時間帯は稼働{stats['predicted_label']}予測（同条件{stats['matching_sample_count']}件）")
            else:
                reasons.append("曜日・時間帯の稼働記録が未蓄積")
            warnings = []
            if supported_count == 0:
                warnings.append("設置機種データを更新してください")
            if stats["confidence"] == "insufficient":
                warnings.append("稼働予測はサンプル不足です")
            if age_days is None or age_days > 30:
                warnings.append("店舗実績が古い、または未収集です")
            results.append({
                "rank": 0,
                "hall_name": name,
                "prefecture": hall.get("prefecture"),
                "score": score,
                "verdict": verdict,
                "verdict_label": verdict_label,
                "reasons": reasons,
                "warnings": warnings,
                "occupancy": stats,
                "machines": metrics,
                "score_breakdown": {
                    "supported_machines": round(install_score),
                    "occupancy_balance": round(occupancy_score),
                    "historical_activity": round(activity_score),
                    "data_freshness": round(freshness_score),
                    "occupancy_confidence": round(confidence_score),
                },
            })
    results.sort(key=lambda row: (row["score"], row["machines"]["supported_machine_count"]), reverse=True)
    selected = results[:max(1, min(int(limit), 30))]
    for index, row in enumerate(selected, 1):
        row["rank"] = index
    return {
        "target_at": target.isoformat(timespec="minutes"),
        "weekday": target.weekday(),
        "time_bucket": _time_bucket(target),
        "prefecture": prefecture,
        "halls": selected,
        "notice": "店舗内に期待値のある空き台が存在する保証ではありません。到着後は必ず個別台を判定してください",
        "method": "対応機種数・過去稼働・曜日時間帯の混雑・データ鮮度・サンプル数を合成",
    }
