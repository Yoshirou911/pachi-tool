"""夜間スクレイプの状態管理と APScheduler ジョブ定義。

api/deps.py のみに依存する。ルーター側 (api/routers/scrape.py, events.py) は
このモジュールが公開する関数(is_scrape_running/set_scrape_running/get_scheduler/
_get_active_halls)経由でのみ状態を読み書きする — 生の変数を import すると
再代入がモジュール間で共有されない(Pythonのimport挙動)ため、必ず関数越しにする。
"""
from __future__ import annotations

import time

from api.deps import logger

_SCHEDULER = None
_SCRAPE_RUNNING = False  # 多重実行防止フラグ


def get_scheduler():
    return _SCHEDULER


def is_scrape_running() -> bool:
    return _SCRAPE_RUNNING


def set_scrape_running(value: bool) -> None:
    global _SCRAPE_RUNNING
    _SCRAPE_RUNNING = value


# デフォルトホール一覧（DBが空の場合のシード用）
_DEFAULT_HALLS = [
    {"hall_name": "ベガスベガス大東店",             "prefecture": "大阪府"},
    {"hall_name": "マルハン大東店",                 "prefecture": "大阪府"},
    {"hall_name": "ニコニコ住道店",                 "prefecture": "大阪府"},
    {"hall_name": "スーパーコスモプレミアム大東店", "prefecture": "大阪府"},
    {"hall_name": "マルハン枚方店",                 "prefecture": "大阪府"},
    {"hall_name": "ニコニコ枚方店",                 "prefecture": "大阪府"},
    {"hall_name": "ベガビック1700枚方店",           "prefecture": "大阪府"},
    {"hall_name": "G-ONE枚方宮之阪店",             "prefecture": "大阪府"},
    {"hall_name": "キコーナ寝屋川南店",             "prefecture": "大阪府"},
    {"hall_name": "ニコニコ寝屋川南インター店",     "prefecture": "大阪府"},
    {"hall_name": "マルハン寝屋川店",               "prefecture": "大阪府"},
    {"hall_name": "ベラジオ寝屋川店",               "prefecture": "大阪府"},
    {"hall_name": "ニコニコ寝屋川店スロット館",     "prefecture": "大阪府"},
    {"hall_name": "123交野店",                      "prefecture": "大阪府"},
    {"hall_name": "キコーナ守口店",                 "prefecture": "大阪府"},
    {"hall_name": "テキサス門真",                   "prefecture": "大阪府"},
]


def _get_active_halls() -> list[dict]:
    """DBからenable=1のホール一覧を取得。失敗時はデフォルト返却"""
    try:
        from scraper.anaslo import get_hall_configs
        halls = get_hall_configs(enabled_only=True)
        return halls if halls else _DEFAULT_HALLS
    except Exception:
        return _DEFAULT_HALLS


def _run_nightly_scrape() -> None:
    """全対象ホールのスクレイプを順番に実行（夜間バッチ用）"""
    if is_scrape_running():
        logger.info("[スクレイプ] 前回の実行がまだ進行中のためスキップ")
        return
    set_scrape_running(True)
    try:
        halls = _get_active_halls()
        # ① アナスロ（台番BB/RB）
        from scraper.anaslo import scrape_hall
        logger.info(f"[アナスロ] 夜間バッチ開始: {len(halls)}店舗")
        for h in halls:
            hname = h["hall_name"] if isinstance(h, dict) else h
            pref = h.get("prefecture", "大阪府") if isinstance(h, dict) else "大阪府"
            try:
                scrape_hall(hname, prefecture=pref, max_days=5, unlimited=True)
            except Exception as e:
                logger.warning(f"[アナスロ] {hname} エラー: {e}")
            time.sleep(30)
        logger.info("[アナスロ] 夜間バッチ完了")
        # ② みんレポ（機種別差枚） - 循環import回避のため遅延import
        from api.routers.hall import _run_minrepo_nightly
        _run_minrepo_nightly(halls, days=3)
    except Exception as e:
        logger.warning(f"[スクレイプ] バッチエラー: {e}")
    finally:
        set_scrape_running(False)


def _start_scrape_scheduler() -> None:
    """APSchedulerで毎夜4時(JST)にスクレイプをスケジュール"""
    global _SCHEDULER
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        _SCHEDULER = BackgroundScheduler(timezone="Asia/Tokyo")
        _SCHEDULER.add_job(
            _run_nightly_scrape,
            CronTrigger(hour=4, minute=0, timezone="Asia/Tokyo"),
            id="nightly_scrape",
            replace_existing=True,
        )
        # イベント自動取得: 毎日12:00(JST)
        def _run_event_scrape():
            try:
                from scraper.events import scrape_all_halls
                halls = _get_active_halls()
                logger.info(f"[イベント] 自動取得開始: {len(halls)}店舗")
                scrape_all_halls(halls)
                logger.info("[イベント] 自動取得完了")
            except Exception as e:
                logger.warning(f"[イベント] 自動取得エラー: {e}")

        _SCHEDULER.add_job(
            _run_event_scrape,
            CronTrigger(hour=12, minute=0, timezone="Asia/Tokyo"),
            id="event_scrape",
            replace_existing=True,
        )
        _SCHEDULER.start()
        logger.info("[スクレイプ] スケジューラー起動: みんレポ04:00/イベント12:00(JST)")
    except Exception as e:
        logger.warning(f"[スクレイプ] スケジューラー起動失敗: {e}")
