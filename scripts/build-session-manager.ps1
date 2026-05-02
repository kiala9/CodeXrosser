param(
    [string]$OutputDir = "$(Split-Path $PSScriptRoot -Parent)\artifacts\SessionManager"
)

# DEPRECATED: the in-app Sessions tab (codex_quota_viewer/sessions) replaces
# this Node bundle. Only invoke this script via `publish.ps1 -IncludeLegacy
# SessionManager` if you intentionally want to ship the legacy Node service
# alongside the new Python implementation. The Node bundle is no longer
# loaded by the Qt app's main UI flow.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$vendor = Join-Path $root "vendor\CodexMM"
$output = Resolve-Path -LiteralPath (New-Item -ItemType Directory -Path $OutputDir -Force)
$appOutput = Join-Path $output "App"
$runtimeOutput = Join-Path $output "Runtime\bin"

if (-not (Test-Path -LiteralPath $vendor)) {
    throw "Vendored CodexMM was not found: $vendor"
}

$node = (Get-Command node -ErrorAction Stop).Source
Get-Command npm -ErrorAction Stop | Out-Null

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE."
    }
}

Push-Location $vendor
try {
    Invoke-Native "npm" @("ci", "--cache", (Join-Path $root ".npm-cache"))
    Invoke-Native "npm" @("run", "build")
    Invoke-Native "npm" @("prune", "--omit=dev")
}
finally {
    Pop-Location
}

Remove-Item -LiteralPath $output -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path (Join-Path $appOutput "dist"), (Join-Path $appOutput "node_modules"), $runtimeOutput -Force | Out-Null
Copy-Item -Path (Join-Path $vendor "dist\*") -Destination (Join-Path $appOutput "dist") -Recurse -Force
Copy-Item -Path (Join-Path $vendor "node_modules\*") -Destination (Join-Path $appOutput "node_modules") -Recurse -Force
Copy-Item -Path (Join-Path $vendor "package.json"), (Join-Path $vendor "package-lock.json") -Destination $appOutput -Force
Copy-Item -Path $node -Destination (Join-Path $runtimeOutput "node.exe") -Force

Write-Host "Session Manager bundle prepared at $output"
