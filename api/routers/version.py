"""アプリのバージョン・更新履歴。"""
from __future__ import annotations

from fastapi import APIRouter

from app_version import get_version_payload

router = APIRouter()


@router.get("/api/version", tags=["system"])
def read_version() -> dict[str, object]:
    return get_version_payload()
