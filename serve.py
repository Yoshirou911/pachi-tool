"""起動スクリプト — 環境変数 PORT / HOST / RELOAD で制御可能。"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import uvicorn
from config import PORT, HOST, RELOAD

if __name__ == "__main__":
    uvicorn.run("api.main:app", host=HOST, port=PORT, reload=RELOAD)
