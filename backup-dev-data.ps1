param(
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Run setup-dev.ps1 first."
}

Set-Location -LiteralPath $projectDir
$arguments = @("scripts\dev_data.py", "backup", "--data-dir", "data")
if ($Output) {
    $arguments += @("--output", $Output)
}
& $pythonExe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Development data backup failed."
}
