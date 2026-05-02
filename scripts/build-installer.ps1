param(
    [string]$Configuration = "Release",
    [string]$Runtime = "win-x64"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$publishDir = Join-Path $root "artifacts\publish"

function Find-Iscc {
    $command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    return $null
}

& (Join-Path $PSScriptRoot "publish.ps1") -Configuration $Configuration -Runtime $Runtime

$iscc = Find-Iscc
if (-not $iscc) {
    winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
    $iscc = Find-Iscc
}

if (-not $iscc) {
    throw "Inno Setup ISCC.exe was not found after installation."
}

& $iscc "/DSourceDir=$publishDir" (Join-Path $root "installer\CodexQuotaViewerWindows.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
