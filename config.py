"""
共通設定。DATA_DIR 環境変数でデータ保存先を切り替え可能。

  ローカル: DATA_DIR 未設定 → data/ ディレクトリ
  Railway:  DATA_DIR=/data  → マウントされたボリューム
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# プロジェクトルート
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

# データディレクトリ: 環境変数で上書き可能
if getattr(sys, "frozen", False):
    _default_data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PachiTool" / "data"
else:
    _default_data_dir = ROOT / "data"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_default_data_dir)))

# 機種データ (コードと一緒に管理 → 常にROOT/data/machines)
MACHINES_DIR = ROOT / "data" / "machines"

# SQLite DBs (永続化対象 → DATA_DIR 以下)
SESSIONS_DB  = DATA_DIR / "sessions.db"
OPPORTUNITIES_DB = DATA_DIR / "opportunities.db"
HALL_REPORTS_DB = DATA_DIR / "hall_reports.db"

# サーバー設定
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")
RELOAD = os.environ.get("RELOAD", "false").lower() == "true"
