"""v3.45 reviewed image extractions.

The original image never enters this database or API.  The browser may keep an
opt-in copy in IndexedDB, while the server stores only a digest, dimensions and
the values a person reviewed.  Records are append-only so later OCR changes do
not silently rewrite old observations.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import IMAGE_ANALYSIS_DB

KINDS = {"data_lamp", "store_material", "floor_map"}


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS image_analysis_record (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('data_lamp','store_material','floor_map')),
          hall_name TEXT NOT NULL DEFAULT '',
          observed_on TEXT,
          image_sha256 TEXT NOT NULL,
          image_mime TEXT NOT NULL,
          image_width INTEGER NOT NULL,
          image_height INTEGER NOT NULL,
          image_size INTEGER NOT NULL,
          extraction_method TEXT NOT NULL,
          extracted_json TEXT NOT NULL,
          reviewed_json TEXT NOT NULL,
          local_original_saved INTEGER NOT NULL DEFAULT 0 CHECK(local_original_saved IN (0,1)),
          review_confirmed INTEGER NOT NULL CHECK(review_confirmed=1),
          verification_status TEXT NOT NULL CHECK(verification_status IN ('確認済み入力','未確認マップ'))
        );
        CREATE INDEX IF NOT EXISTS idx_image_analysis_created
          ON image_analysis_record(created_at DESC);
        CREATE TRIGGER IF NOT EXISTS image_analysis_no_update
          BEFORE UPDATE ON image_analysis_record BEGIN
            SELECT RAISE(ABORT,'image analysis records are immutable');
          END;
        CREATE TRIGGER IF NOT EXISTS image_analysis_no_delete
          BEFORE DELETE ON image_analysis_record BEGIN
            SELECT RAISE(ABORT,'image analysis records are immutable');
          END;
        """
    )
    conn.commit()
    return conn


def _public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "created_at": row["created_at"], "kind": row["kind"],
        "hall_name": row["hall_name"], "observed_on": row["observed_on"],
        "image": {
            "sha256": row["image_sha256"], "mime": row["image_mime"],
            "width": row["image_width"], "height": row["image_height"],
            "size": row["image_size"], "original_location": "not_received",
            "device_copy_requested": bool(row["local_original_saved"]),
        },
        "extraction_method": row["extraction_method"],
        # Legacy rows may contain unreviewed OCR. Preserve the immutable data,
        # but never return those candidates through the public API.
        "extracted": [],
        "reviewed": json.loads(row["reviewed_json"]),
        "review_confirmed": True,
        "verification_status": row["verification_status"],
    }


class ImageAnalysisStore:
    def __init__(self, db_path: Path = IMAGE_ANALYSIS_DB):
        self.db_path = Path(db_path)

    def status(self) -> dict:
        count = 0
        if self.db_path.exists():
            with _connect(self.db_path) as conn:
                count = int(conn.execute("SELECT count(*) FROM image_analysis_record").fetchone()[0])
        return {
            "available": True,
            "saved_records": count,
            "ocr_location": "browser",
            "original_image_default": "not_saved",
            "original_image_optional_location": "device_only",
            "external_image_ai_enabled": False,
            "external_image_ai_reason": "画像対応・費用・送信先が接続先ごとに異なるため、この版では外部送信しません",
            "notice": "画像は端末内で解析し、確認した抽出値だけを保存します。配置は未確認マップとして扱います。",
        }

    def save(self, payload: dict) -> dict:
        record_id = str(uuid4())
        status = "未確認マップ" if payload["kind"] == "floor_map" else "確認済み入力"
        created_at = datetime.now(timezone.utc).isoformat()
        with _connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO image_analysis_record
                (id,created_at,kind,hall_name,observed_on,image_sha256,image_mime,
                 image_width,image_height,image_size,extraction_method,extracted_json,
                 reviewed_json,local_original_saved,review_confirmed,verification_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (record_id, created_at, payload["kind"], payload.get("hall_name", ""),
                 payload.get("observed_on"), payload["image"]["sha256"],
                 payload["image"]["mime"], payload["image"]["width"],
                 payload["image"]["height"], payload["image"]["size"],
                 # Older clients may still send candidates. Only reviewed values
                 # are retained; the legacy column stays empty for new records.
                 payload["extraction_method"], _encoded([]),
                 _encoded(payload["reviewed"]), int(payload.get("save_original_on_device", False)),
                 status),
            )
            row = conn.execute("SELECT * FROM image_analysis_record WHERE id=?", (record_id,)).fetchone()
            conn.commit()
        return _public(row)

    def recent(self, limit: int = 20) -> list[dict]:
        if not self.db_path.exists():
            return []
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM image_analysis_record ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_public(row) for row in rows]


image_analyses = ImageAnalysisStore()
