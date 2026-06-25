# pachi-tool 起動スクリプト (Windows PowerShell)
# 実行: .\start.ps1

$host.UI.RawUI.WindowTitle = "pachi-tool"
Write-Host "=== pachi-tool ===" -ForegroundColor Cyan

# 依存チェック
try {
    python -c "import fastapi, uvicorn" 2>&1 | Out-Null
} catch {
    Write-Host "依存パッケージをインストールします..." -ForegroundColor Yellow
    pip install -r requirements.txt
}

Write-Host "サーバ起動中... http://localhost:8000" -ForegroundColor Green
Write-Host "停止: Ctrl+C" -ForegroundColor Gray
Write-Host ""

# APIサーバ起動（フロントエンドも一緒に配信）
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
