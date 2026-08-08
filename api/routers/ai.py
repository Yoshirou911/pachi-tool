"""AIチャット・レポートエンドポイント"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import (
    HALL_REPORTS_DB,
    MACHINES_DIR,
    WEB_DIR,
    _cache_get,
    _cache_invalidate_prefix,
    _cache_set,
    _get_event_conn,
    _get_machine_path,
    _get_reports_conn,
    logger,
)

router = APIRouter()

try:
    from api.ai_service import chat as ai_chat, generate_report, comment_estimate
    AI_AVAILABLE = True
except ImportError:
    try:
        from ai_service import chat as ai_chat, generate_report, comment_estimate
        AI_AVAILABLE = True
    except ImportError:
        AI_AVAILABLE = False


class ChatRequest(BaseModel):
    message: str
    hall_name: str = "ベガスベガス大東店"
    history: list = Field(default_factory=list)


@router.post("/api/ai/chat")
def api_ai_chat(req: ChatRequest):
    if not AI_AVAILABLE:
        return {"reply": "AIサービスが利用できません。"}
    reply = ai_chat(req.message, req.hall_name, req.history)
    return {"reply": reply}


@router.get("/api/ai/report")
def api_ai_report(hall_name: str = "ベガスベガス大東店"):
    if not AI_AVAILABLE:
        return {"report": "AIサービスが利用できません。"}
    report = generate_report(hall_name)
    return {"report": report}


@router.post("/api/ai/estimate_comment")
def api_ai_estimate_comment(body: dict):
    if not AI_AVAILABLE:
        return {"comment": ""}
    comment = comment_estimate(
        machine_name=body.get("machine_name", ""),
        games=body.get("games", 0),
        element_counts=body.get("element_counts", {}),
        posterior=body.get("posterior", {}),
        ev=body.get("ev", 0),
        recommendation=body.get("recommendation", ""),
        element_analysis=body.get("element_analysis", []),
        credible_interval=body.get("credible_interval"),
        element_powers=body.get("element_powers"),
        correlated_elements=body.get("correlated_elements"),
    )
    return {"comment": comment}


@router.get("/api/ai/status")
def api_ai_status():
    import os
    has_key = bool(os.environ.get("GROQ_API_KEY", ""))
    return {"available": has_key and AI_AVAILABLE}

