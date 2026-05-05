param(
    [string]$Configuration = "Release",
    [string]$Runtime = "win-x64",
    [switch]$IncludeLegacySessionManager
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$publishDir = Join-Path $root "artifacts\publish"
$pyInstallerDist = Join-Path $root "artifacts\pyinstaller"
$pyInstallerWork = Join-Path $root "artifacts\pyinstaller-work"
$pyInstallerSpec = Join-Path $root "artifacts\pyinstaller-spec"
$sessionBundle = Join-Path $root "artifacts\SessionManager"
$publishedSessionBundle = Join-Path $publishDir "SessionManager"
$entry = Join-Path $root "src\CodexQuotaViewerWindows.Qt\launch.py"
$assetsDir = Join-Path $root "src\CodexQuotaViewerWindows.Qt\codex_quota_viewer\assets"
$sessionsSchema = Join-Path $root "src\CodexQuotaViewerWindows.Qt\codex_quota_viewer\sessions\schema.sql"
$iconPath = Join-Path $assetsDir "cqv-app-icon.ico"
$tempRoot = Join-Path $root ".tmp"
$binaryName = "CodeXrosser"

function Copy-TreeRobust {
    param(
        [string]$Source,
        [string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $args = @($Source, $Destination, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NP")
    if ((Test-Path -LiteralPath (Join-Path $Destination "_internal\VCRUNTIME140.dll")) -or
        (Test-Path -LiteralPath (Join-Path $Destination "_internal\VCRUNTIME140_1.dll"))) {
        $args += @("/XF", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
    }

    & robocopy @args
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        throw "robocopy failed with exit code $code while copying $Source to $Destination"
    }
    $global:LASTEXITCODE = 0
}

function Ensure-Python {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "python was not found in PATH."
    }

    return $command.Source
}

if (-not (Test-Path -LiteralPath $python)) {
    $systemPython = Ensure-Python
    & $systemPython -m venv $venv
}

New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:PIP_CACHE_DIR = Join-Path $root ".pip-cache"
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $root "requirements.txt")

if ($IncludeLegacySessionManager) {
    try {
        & (Join-Path $PSScriptRoot "build-session-manager.ps1") -OutputDir $sessionBundle
    }
    catch {
        if (-not (Test-Path -LiteralPath $sessionBundle)) {
            throw
        }

        Write-Warning "Session Manager rebuild failed; keeping the existing bundle at $sessionBundle."
    }
}
else {
    Write-Host "Skipping legacy Node Session Manager bundle (in-app Sessions tab is the default)."
}

Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pyInstallerDist -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pyInstallerWork -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pyInstallerSpec -Recurse -Force -ErrorAction SilentlyContinue

# Bake the parser fingerprint into the bundle so the runtime
# ``_compute_parser_version`` in jsonl_parser.py reads a deterministic
# per-build value when ``sys.frozen`` is true. Frozen builds can't read
# the .py source on disk, so without this they'd all share the same
# hash and users wouldn't get auto-reparse on parser upgrades.
$parserFingerprint = Join-Path $tempRoot "parser_fingerprint.json"
& $python (Join-Path $PSScriptRoot "compute-parser-fingerprint.py") --output $parserFingerprint
if ($LASTEXITCODE -ne 0) {
    throw "compute-parser-fingerprint.py failed with exit code $LASTEXITCODE."
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name $binaryName `
    --icon $iconPath `
    --paths (Join-Path $root "src\CodexQuotaViewerWindows.Qt") `
    --add-data "$assetsDir;codex_quota_viewer/assets" `
    --add-data "$sessionsSchema;codex_quota_viewer/sessions" `
    --add-data "$parserFingerprint;codex_quota_viewer/sessions" `
    --distpath $pyInstallerDist `
    --workpath $pyInstallerWork `
    --specpath $pyInstallerSpec `
    $entry

Copy-TreeRobust (Join-Path $pyInstallerDist $binaryName) $publishDir
if ($IncludeLegacySessionManager -and (Test-Path -LiteralPath $sessionBundle)) {
    Remove-Item -LiteralPath $publishedSessionBundle -Recurse -Force -ErrorAction SilentlyContinue
    Copy-TreeRobust $sessionBundle $publishedSessionBundle
}

Write-Host "Published Qt app at $publishDir"
