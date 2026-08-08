# PACHI TOOL hidden desktop launcher
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$appUrl = "http://localhost:8000/"
$braveExe = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
$logDir = Join-Path $projectDir "data\logs"
$stdoutLog = Join-Path $logDir "pachi-tool.stdout.log"
$stderrLog = Join-Path $logDir "pachi-tool.stderr.log"
$launcherLog = Join-Path $logDir "pachi-tool.launcher.log"

function Write-LauncherError([string]$message) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    "$(Get-Date -Format o) $message" | Add-Content -LiteralPath $launcherLog -Encoding UTF8
}

function Open-PachiTool {
    if (-not (Test-Path -LiteralPath $braveExe)) {
        throw "Brave was not found: $braveExe"
    }
    Start-Process -FilePath $braveExe -ArgumentList "--app=$appUrl", "--start-maximized"
}

try {
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Runtime (.venv) was not found: $pythonExe"
    }

    # If already running, open the browser without starting another server.
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Open-PachiTool
        exit 0
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $server = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($server.HasExited) {
            throw "The server stopped before startup completed. See $stderrLog"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing "${appUrl}api/machines" -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # Wait for startup.
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $ready) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        throw "Startup check timed out. See $stderrLog"
    }

    Open-PachiTool
} catch {
    Write-LauncherError $_.Exception.Message
    Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
    [System.Windows.MessageBox]::Show(
        "PACHI TOOL could not start.`nPlease ask Codex to check the startup error.",
        "PACHI TOOL",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}
