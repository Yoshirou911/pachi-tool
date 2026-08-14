param(
    [string]$DataBackup = "",
    [switch]$SkipInstall,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
Set-Location -LiteralPath $projectDir

if (-not (Test-Path -LiteralPath $pythonExe)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv $venvDir
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $venvDir
    } else {
        throw "Python 3.12 was not found. Install Python first."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python virtual environment."
    }
}

if (-not $SkipInstall) {
    & $pythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to update pip." }
    & $pythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Failed to install dependencies." }
}

if ($DataBackup) {
    $resolvedBackup = (Resolve-Path -LiteralPath $DataBackup).Path
    & $pythonExe scripts\dev_data.py restore $resolvedBackup --data-dir data
    if ($LASTEXITCODE -ne 0) { throw "Failed to restore hall analysis data." }
}

Write-Host ""
Write-Host "PACHI TOOL development setup is ready." -ForegroundColor Green
Write-Host "Start: .\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000"
Write-Host "URL : http://127.0.0.1:8000/"

if ($Start) {
    & $pythonExe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
}
