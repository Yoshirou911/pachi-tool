param(
    [string]$OutputPath = "SOURCE_SNAPSHOT.txt"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputPath

$topLevelFiles = @(
    ".dockerignore",
    ".gitignore",
    "app_version.py",
    "backup-dev-data.ps1",
    "build-desktop.ps1",
    "config.py",
    "desktop_app.py",
    "Dockerfile",
    "fly.toml",
    "pachi-tool.spec",
    "Procfile",
    "railway.toml",
    "requirements-desktop.txt",
    "requirements.txt",
    "serve.py",
    "setup_task.ps1",
    "setup-dev.ps1",
    "start-hidden.ps1",
    "start.ps1",
    "sync_scrape.py"
)

$sourceDirectories = @(
    ".github/workflows",
    "api",
    "core",
    "date",
    "hall",
    "juggler",
    "mobile",
    "opportunity",
    "records",
    "scraper",
    "scripts",
    "tests",
    "value",
    "web"
)

$allowedExtensions = @(
    ".py", ".js", ".mjs", ".html", ".css", ".json",
    ".ps1", ".toml", ".yml", ".yaml", ".txt", ".spec"
)

$excludedDirectoryNames = @(
    ".git", ".venv", ".next", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build"
)

function Get-RelativePath([string]$BasePath, [string]$TargetPath) {
    # Windows PowerShell 5.1 の .NET Framework には Path.GetRelativePath がない。
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd("\", "/")
    $targetFull = [IO.Path]::GetFullPath($TargetPath)
    if (-not $targetFull.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Snapshot target is outside the repository: $targetFull"
    }
    return $targetFull.Substring($baseFull.Length).TrimStart("\", "/")
}

function Test-ExcludedPath([string]$RelativePath) {
    $normalized = $RelativePath.Replace("\", "/")
    $segments = $normalized.Split("/")
    foreach ($segment in $segments) {
        if ($excludedDirectoryNames -contains $segment) { return $true }
        if ($segment -like "dist-*") { return $true }
        if ($segment -like "build-*") { return $true }
    }
    $name = [IO.Path]::GetFileName($normalized)
    if ($name -like ".env*") { return $true }
    if ($name -match "(?i)(^|[._-])(secret|credentials?)([._-]|$)") { return $true }
    if ($name -match "(?i)(package-lock|pnpm-lock|yarn\.lock|poetry\.lock|Pipfile\.lock)") { return $true }
    if ($name -eq "SOURCE_SNAPSHOT.txt") { return $true }
    if ($normalized -eq "desktop/version_info.txt") { return $true }
    return $false
}

$files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
foreach ($relative in $topLevelFiles) {
    $path = Join-Path $root $relative
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $files.Add((Get-Item -LiteralPath $path))
    }
}

foreach ($relativeDirectory in $sourceDirectories) {
    $directory = Join-Path $root $relativeDirectory
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
    foreach ($file in Get-ChildItem -LiteralPath $directory -Recurse -File) {
        $relative = Get-RelativePath $root $file.FullName
        if (Test-ExcludedPath $relative) { continue }
        if ($allowedExtensions -notcontains $file.Extension.ToLowerInvariant()) { continue }
        $files.Add($file)
    }
}

foreach ($relative in @("data/hall_coords.json", "data/opportunity_catalog.json")) {
    $path = Join-Path $root $relative
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $files.Add((Get-Item -LiteralPath $path))
    }
}

$machineDirectory = Join-Path $root "data/machines"
if (Test-Path -LiteralPath $machineDirectory -PathType Container) {
    foreach ($file in Get-ChildItem -LiteralPath $machineDirectory -File -Filter "*.json") {
        if ($file.Length -gt 0) { $files.Add($file) }
    }
}

$files = $files |
    Sort-Object FullName -Unique |
    Sort-Object { (Get-RelativePath $root $_.FullName).Replace("\", "/") }

$encoding = [Text.UTF8Encoding]::new($false)
$writer = [IO.StreamWriter]::new($output, $false, $encoding)
try {
    foreach ($file in $files) {
        $relative = (Get-RelativePath $root $file.FullName).Replace("\", "/")
        $content = [IO.File]::ReadAllText($file.FullName, $encoding)

        # Literal token/private-key patterns are never copied. Environment variable names and empty placeholders remain.
        $content = [regex]::Replace($content, "(?i)(sk-|gsk_|gh[pousr]_)[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]")
        $content = [regex]::Replace($content, "(?s)-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]")

        $writer.WriteLine("===== $relative =====")
        $writer.Write($content.TrimEnd("`r", "`n"))
        $writer.WriteLine()
        $writer.WriteLine()
    }
}
finally {
    $writer.Dispose()
}

$size = (Get-Item -LiteralPath $output).Length
Write-Output ("Created {0} files / {1} bytes: {2}" -f $files.Count, $size, $output)
