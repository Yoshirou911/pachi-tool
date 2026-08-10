"""夜間スクレイプの状態管理と APScheduler ジョブ定義。

api/deps.py のみに依存する。ルーター側 (api/routers/scrape.py, events.py) は
このモジュールが公開する関数(is_scrape_running/set_scrape_running/get_scheduler/
_get_active_halls)経由でのみ状態を読み書きする — 生の変数を import すると
再代入がモジュール間で共有されない(Pythonのimport挙動)ため、必ず関数越しにする。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

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


# 四條畷駅を起点にしたデフォルト収集範囲（近い順）。
# 徒歩圏 → 野崎・住道 → 大東市内の大型店、の順に収集する。
_DEFAULT_HALLS = [
    {"hall_name": "キコーナ四條畷店",               "prefecture": "大阪府"},
    {"hall_name": "ひま・わり四條畷店",             "prefecture": "大阪府"},
    {"hall_name": "キコーナ野崎店",                 "prefecture": "大阪府"},
    {"hall_name": "ニコニコ住道店",                 "prefecture": "大阪府"},
    {"hall_name": "キコーナ大東店",                 "prefecture": "大阪府"},
    {"hall_name": "マルハン大東店",                 "prefecture": "大阪府"},
    {"hall_name": "スーパーコスモプレミアム大東店", "prefecture": "大阪府"},
    {"hall_name": "ベガスベガス大東店",             "prefecture": "大阪府"},
]


def _get_active_halls() -> list[dict]:
    """DBからenable=1のホール一覧を取得。失敗時はデフォルト返却"""
    try:
        from scraper.anaslo import get_hall_configs
        halls = get_hall_configs(enabled_only=True)
        if not halls:
            return _DEFAULT_HALLS
        local_order = {h["hall_name"]: index for index, h in enumerate(_DEFAULT_HALLS)}
        return sorted(halls, key=lambda h: local_order.get(h.get("hall_name", ""), 999))
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
        _run_public_machine_scrape()
        # ③ P-WORLD（現在の設置スマスロ）。差枚データがない店舗も対象機種を蓄積する。
        _run_snapshot_scrape()
    except Exception as e:
        logger.warning(f"[スクレイプ] バッチエラー: {e}")
    finally:
        set_scrape_running(False)


def _run_snapshot_scrape() -> None:
    """公開店舗ページの設置スマスロ構成を日次保存する。"""
    try:
        from scraper.pworld_snapshot import scrape_all
        results = scrape_all(_get_active_halls())
        logger.info(f"[設置機種] 日次スナップショット完了: {results}")
    except Exception as e:
        logger.warning(f"[設置機種] 日次スナップショットエラー: {e}")


def _run_public_machine_scrape() -> None:
    """公開機種別データを、6時間に1回までの低頻度で補完する。"""
    try:
        from scraper.anoslot_public import is_refresh_due, scrape_all
        if not is_refresh_due(max_age_hours=6):
            logger.info("[公開機種データ] 6時間以内に更新済みのためスキップ")
            return
        results = scrape_all()
        saved = sum(int(item.get("rows", 0)) for item in results if item.get("status") == "ok")
        logger.info(f"[公開機種データ] 更新完了: {saved}機種日")
    except Exception as e:
        logger.warning(f"[公開機種データ] 更新エラー: {e}")


def _run_opportunity_source_check() -> None:
    """期待値公開元を低頻度で確認し、差分を承認キューへ保存する。"""
    try:
        from scraper.opportunity_crawler import run_crawl
        result = run_crawl()
        logger.info(f"[期待値ソース] 確認完了: {result}")
    except Exception as e:
        logger.warning(f"[期待値ソース] 確認エラー: {e}")


def _run_startup_refresh() -> None:
    """4時にアプリが閉じていた場合も、次回起動時に直近データを補う。"""
    if is_scrape_running():
        return
    set_scrape_running(True)
    try:
        halls = _get_active_halls()
        from api.routers.hall import _run_minrepo_nightly
        _run_minrepo_nightly(halls, days=3)
        _run_public_machine_scrape()
        _run_snapshot_scrape()
    except Exception as e:
        logger.warning(f"[起動時更新] エラー: {e}")
    finally:
        set_scrape_running(False)


def _start_scrape_scheduler() -> None:
    """APSchedulerで毎夜4時(JST)にスクレイプをスケジュール"""
    global _SCHEDULER
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
        _SCHEDULER = BackgroundScheduler(timezone="Asia/Tokyo")
        _SCHEDULER.add_job(
            _run_nightly_scrape,
            CronTrigger(hour=4, minute=0, timezone="Asia/Tokyo"),
            id="nightly_scrape",
            replace_existing=True,
        )
        _SCHEDULER.add_job(
            _run_opportunity_source_check,
            CronTrigger(hour=3, minute=20, day_of_week="mon", timezone="Asia/Tokyo"),
            id="weekly_opportunity_source_check",
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
        _SCHEDULER.add_job(
            _run_snapshot_scrape,
            CronTrigger(hour=12, minute=15, timezone="Asia/Tokyo"),
            id="machine_snapshot",
            replace_existing=True,
        )
        _SCHEDULER.add_job(
            _run_startup_refresh,
            DateTrigger(run_date=datetime.now() + timedelta(seconds=20)),
            id="startup_refresh",
            replace_existing=True,
        )
        _SCHEDULER.start()
        logger.info("[スクレイプ] 期待値月曜03:20/差枚04:00/イベント12:00/設置機種12:15(JST)")
    except Exception as e:
        logger.warning(f"[スクレイプ] スケジューラー起動失敗: {e}")
