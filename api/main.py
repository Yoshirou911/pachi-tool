"""
pachi-tool FastAPI バックエンド。

起動:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

構成:
    api/deps.py         共有ヘルパー（ロガー・キャッシュ・DB接続・パス解決）
    api/scheduler.py    夜間スクレイプの状態管理・APScheduler
    api/routers/*.py    機能ごとのエンドポイント群（機種/期待値/推測/収支/ホール分析/
                         スクレイプ/イベント/マップ/運用/AI）
    api/main.py（本ファイル） FastAPIアプリの組み立て・ミドルウェア・起動処理のみ
"""
from __future__ import annotations

import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.deps import WEB_DIR, logger
from api import scheduler
from app_version import APP_VERSION
from api.routers import (
    admin,
    ai,
    estimate,
    events,
    hall,
    layout,
    machines,
    map as map_router,
    occupancy,
    opportunity,
    scrape,
    sessions,
    version,
)
from records.models import init_db

# ---------------------------------------------------------------------------
# アクセス制御
# ---------------------------------------------------------------------------
# PACHI_ACCESS_TOKEN が設定されている環境（本番デプロイ等）でのみ、/api/* への
# アクセスにヘッダートークンを要求する。未設定（ローカル開発・デスクトップ版）
# の場合は従来どおり誰でもアクセスできる — 挙動を変えない。
_ACCESS_TOKEN = os.environ.get("PACHI_ACCESS_TOKEN", "")
_TOKEN_HEADER = "x-pachi-token"

# CORS: フロントは同一オリジン（StaticFilesで配信）なので本来クロスオリジンは
# 不要。開発などで別オリジンから叩きたい場合のみ CORS_ALLOW_ORIGINS
# (カンマ区切り) で明示的に許可する。未設定時はワイルドカードにしない。
_configured_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if o.strip()
]
# GitHub Pages で配布する iPhone PWA は別オリジンになるため、この公開元だけを常時許可する。
_cors_origins = list(dict.fromkeys([
    "https://yoshirou911.github.io",
    *_configured_cors_origins,
]))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """起動時の初期化: キャッシュウォームアップ + 夜間スクレイプスケジューラ起動"""

    # API受付開始前に、前回終了時の収集だけを確実に「再開待ち」へ戻す。
    try:
        from scraper.minrepo_archive import recover_interrupted_jobs
        recover_interrupted_jobs()
    except Exception as exc:
        logger.warning(f"[過去データ収集] 再開準備に失敗: {exc}")

    def _init() -> None:
        # デフォルトホールをDBへシード（DBが空の場合のみ）
        try:
            from scraper.anaslo import seed_hall_configs
            seed_hall_configs(scheduler._DEFAULT_HALLS)
        except Exception:
            pass
        try:
            hall.compare_halls(days=30)
        except Exception:
            pass
        scheduler._start_scrape_scheduler()

    threading.Thread(target=_init, daemon=True).start()
    yield
    # デスクトップ版の終了時にAPSchedulerのワーカースレッドを確実に止める。
    active_scheduler = scheduler.get_scheduler()
    if active_scheduler is not None and active_scheduler.running:
        try:
            active_scheduler.shutdown(wait=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
app = FastAPI(title="pachi-tool", version=APP_VERSION, docs_url="/api/docs", lifespan=_lifespan)

if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def _require_access_token(request: Request, call_next):
    if _ACCESS_TOKEN and request.url.path.startswith("/api/"):
        supplied = request.headers.get(_TOKEN_HEADER, "")
        if not secrets.compare_digest(supplied, _ACCESS_TOKEN):
            return JSONResponse({"detail": "アクセスキーが必要です"}, status_code=401)
    return await call_next(request)


init_db()


def _init_auxiliary_databases() -> None:
    """空のDATA_DIRでも分析APIが安全に起動できるよう、各スクレイパーの表を作る。"""
    initializers = []
    try:
        from scraper.anaslo import init_db as init_anaslo_db
        initializers.append(init_anaslo_db)
    except ImportError:
        pass
    try:
        from scraper.minrepo import init_db as init_minrepo_db
        initializers.append(init_minrepo_db)
    except ImportError:
        pass
    try:
        from scraper.events import get_conn as init_event_db
        initializers.append(init_event_db)
    except ImportError:
        pass
    try:
        from scraper.pworld_snapshot import init_db as init_snapshot_db
        initializers.append(init_snapshot_db)
    except ImportError:
        pass
    try:
        from api.routers.layout import init_layout_db
        initializers.append(init_layout_db)
    except ImportError:
        pass
    try:
        from hall.occupancy import init_db as init_occupancy_db
        initializers.append(init_occupancy_db)
    except ImportError:
        pass

    for initializer in initializers:
        try:
            conn = initializer()
            conn.close()
        except Exception as exc:
            logger.warning(f"[DB初期化] {initializer.__module__}: {exc}")


_init_auxiliary_databases()

# ---------------------------------------------------------------------------
# ルーター登録
# ---------------------------------------------------------------------------
app.include_router(machines.router)
app.include_router(opportunity.router)
app.include_router(estimate.router)
app.include_router(sessions.router)
app.include_router(hall.router)
app.include_router(scrape.router)
app.include_router(events.router)
app.include_router(map_router.router)
app.include_router(layout.router)
app.include_router(occupancy.router)
app.include_router(admin.router)
app.include_router(ai.router)
app.include_router(version.router)

# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
MOBILE_DIR = WEB_DIR.parent / "mobile"
if MOBILE_DIR.exists():
    app.mount("/mobile", StaticFiles(directory=str(MOBILE_DIR), html=True), name="mobile")

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
