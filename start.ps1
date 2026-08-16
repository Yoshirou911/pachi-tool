# PACHI TOOL desktop launcher (ASCII for Windows PowerShell 5.1 compatibility)
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$appUrl = "http://localhost:8000/"
$braveExe = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

function Open-PachiTool {
    if (-not (Test-Path -LiteralPath $braveExe)) {
        throw "Brave was not found: $braveExe"
    }
    Start-Process -FilePath $braveExe -ArgumentList "--app=$appUrl", "--start-maximized"
}

$host.UI.RawUI.WindowTitle = "PACHI TOOL"
Set-Location -LiteralPath $projectDir

Write-Host ""
Write-Host "  PACHI TOOL" -ForegroundColor Cyan
Write-Host "  -----------------------------" -ForegroundColor DarkGray

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Host "  Runtime (.venv) was not found." -ForegroundColor Red
    Write-Host "  Please ask Codex to set up the runtime." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

# If the app is already running, only open the browser.
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "  Already running. Opening the browser..." -ForegroundColor Green
    Open-PachiTool
    Start-Sleep -Seconds 1
    exit 0
}

Write-Host "  Starting server..." -ForegroundColor Yellow

$server = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $projectDir `
    -NoNewWindow `
    -PassThru

try {
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($server.HasExited) {
            throw "The server stopped before startup completed."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing "${appUrl}api/machines" -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # Waiting for startup.
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $ready) {
        throw "Startup check timed out."
    }

    Write-Host "  Ready: $appUrl" -ForegroundColor Green
    Write-Host "  Close this window to stop PACHI TOOL." -ForegroundColor DarkGray
    Write-Host ""
    Open-PachiTool
    Wait-Process -Id $server.Id
} catch {
    Write-Host "  Startup error: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "  Press Enter to close"
} finally {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
