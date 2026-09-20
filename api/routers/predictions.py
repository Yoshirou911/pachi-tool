"""Server-owned prospective verification. No arbitrary client forecasts accepted."""
from datetime import datetime, timedelta
from threading import Lock

from fastapi import APIRouter, HTTPException

from api.deps import _get_reports_conn
from app_version import APP_VERSION
from hall.prediction_log import JST, POLICY, initialize, production_summary, resolve_outcomes, save_batch

router = APIRouter()
_run_lock = Lock()


@router.get("/api/predictions/comparison", tags=["predictions"])
def get_comparison():
    from api.prediction_replay import status
    from hall.prediction_benchmark import latest_replay, prospective_comparison
    conn = _get_reports_conn()
    try:
        return {"prospective": prospective_comparison(conn), "retrospective": latest_replay(conn), "job": status()}
    finally:
        if conn:
            conn.close()


@router.get("/api/predictions/model_review", tags=["predictions"])
def get_model_review():
    """Read the last immutable review; no search, replay, or writes on GET."""
    from hall.model_selection import SPEC, latest_review
    conn = _get_reports_conn()
    try:
        return {"spec": SPEC, "report": latest_review(conn)}
    finally:
        if conn:
            conn.close()


@router.get("/api/predictions/probability_validation", tags=["predictions"])
def get_probability_validation():
    """Read-only diagnostics; no forecast generation, fitting or result resolution."""
    from hall.probability_validation import validation_report
    conn = _get_reports_conn()
    try:
        return validation_report(conn)
    finally:
        if conn:
            conn.close()


@router.get("/api/predictions/candidate_evaluation", tags=["predictions"])
def get_candidate_evaluation():
    """Fixed-K diagnostics only. No client-selected K, forecasts, or outcomes."""
    from hall.candidate_evaluation import candidate_report
    conn = _get_reports_conn()
    try:
        return candidate_report(conn)
    finally:
        if conn:
            conn.close()


@router.get("/api/predictions/monitor", tags=["predictions"])
def get_prediction_monitor():
    """Read-only fixed-window diagnostics; no adoption or alert side effects."""
    from hall.prediction_monitor import monitor_report
    conn = _get_reports_conn()
    try:
        return monitor_report(conn)
    finally:
        if conn:
            conn.close()


@router.post("/api/predictions/model_review", tags=["predictions"])
def post_model_review():
    """Server-fixed dates and cohort. No client scores or adoption override."""
    from hall.model_selection import prospective_review, store_review, latest_review
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "予測保存または審査を実行中です")
    conn = None
    try:
        conn = _get_reports_conn()
        if conn is None:
            raise HTTPException(409, "実績DBがありません")
        now = datetime.now(JST)
        result = prospective_review(conn, today=now.date())
        review_id = store_review(conn, result, created_at=now.isoformat())
        return {"id": review_id, "report": latest_review(conn)}
    finally:
        if conn:
            conn.close()
        _run_lock.release()


@router.post("/api/predictions/benchmark", status_code=202, tags=["predictions"])
def post_benchmark():
    """固定7暦日・120日履歴。対象日/予測値のクライアント指定は受けない。"""
    from api.prediction_replay import start
    return start()


@router.get("/api/predictions/verification", tags=["predictions"])
def get_verification():
    conn = _get_reports_conn()
    try:
        result = production_summary(conn)
    finally:
        if conn:
            conn.close()
    from api.scheduler import get_scheduler
    scheduler = get_scheduler()
    job = scheduler.get_job("prediction_daily") if scheduler else None
    next_run = getattr(job, "next_run_time", None)
    return {**result, "running": _run_lock.locked(),
            "next_run_at": next_run.isoformat() if next_run else None,
            "schedule_notice": "毎日13:00 JST・起動時。サーバー稼働中のみ。停止中の過去予測は後付けしません。"}


def run_daily_predictions():
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "予測保存・答え合わせを実行中です")
    conn = None
    try:
        started = datetime.now(JST)
        target = (started.date() + timedelta(days=1)).isoformat()
        conn = _get_reports_conn()
        if conn is None:
            raise HTTPException(409, "実績DBがまだありません。先にデータを収集してください")
        initialize(conn)
        resolved = resolve_outcomes(conn)
        exists = conn.execute("SELECT 1 FROM prediction_batch WHERE target_date=? AND region=? AND policy=?",
                              (target, "shijonawate", POLICY)).fetchone()
        if exists:
            return {"status": "already_saved", "recorded": 0, "resolved": resolved, "target_date": target}
        from api.routers.hall import _build_target_search
        prediction = _build_target_search(target, 120, 20, "shijonawate", 70, include_inputs=True)
        result = save_batch(conn, prediction, version=APP_VERSION, started_at=started)
        return {**result, "resolved": resolved, "target_date": target}
    finally:
        if conn:
            conn.close()
        _run_lock.release()


@router.post("/api/predictions/run", tags=["predictions"])
def post_run_predictions():
    """固定条件で翌日の全算出候補を保存。再実行で既存予測は変わらない。"""
    return run_daily_predictions()
