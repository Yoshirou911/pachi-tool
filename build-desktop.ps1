# Build the Windows desktop app as a one-folder distribution.
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Runtime was not found: $pythonExe"
}

Set-Location -LiteralPath $projectDir
& $pythonExe -m PyInstaller --noconfirm --clean "pachi-tool.spec"
if ($LASTEXITCODE -ne 0) {
    throw "Desktop build failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $projectDir\dist\PACHI TOOL\PACHI TOOL.exe"
