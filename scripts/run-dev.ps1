param(
    [string]$Configuration = "Release",
    [string]$Runtime = "win-x64",
    [switch]$NoBuild,
    [switch]$NoLaunch,
    [switch]$KeepExisting
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
$packageRoot = Join-Path $root "src\CodexQuotaViewerWindows.Qt"
$tempRoot = Join-Path $root ".tmp"

function Ensure-Python {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "python was not found in PATH."
    }

    return $command.Source
}

function Ensure-Venv {
    if (-not (Test-Path -LiteralPath $python)) {
        $systemPython = Ensure-Python
        & $systemPython -m venv $venv
    }

    $env:PIP_CACHE_DIR = Join-Path $root ".pip-cache"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root "requirements.txt")
}

function Stop-ExistingViewer {
    $currentPid = $PID
    try {
        $processes = Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessId -ne $currentPid -and (
                $_.Name -ieq "CodexQuotaViewerWindowsQt.exe" -or
                ($_.CommandLine -and $_.CommandLine -like "*codex_quota_viewer*")
            )
        }
    } catch {
        Write-Warning "Could not inspect existing viewer processes: $($_.Exception.Message)"
        return
    }

    foreach ($process in $processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Host "Stopped existing Qt dev app (PID $($process.ProcessId))."
        } catch {
            Write-Warning "Failed to stop existing Qt dev app (PID $($process.ProcessId)): $($_.Exception.Message)"
        }
    }
}

if (-not $NoBuild) {
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    Ensure-Venv
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Dev Python environment was not found: $python"
}

if ($NoLaunch) {
    Write-Host "Prepared Qt dev app: $python -m codex_quota_viewer"
    return
}

$env:PYTHONPATH = $packageRoot
$launcher = if (Test-Path -LiteralPath $pythonw) { $pythonw } else { $python }
if (-not $KeepExisting) {
    Stop-ExistingViewer
}
Start-Process -FilePath $launcher -ArgumentList "-m", "codex_quota_viewer" -WorkingDirectory $root | Out-Null
Write-Host "Started Qt dev app with $launcher -m codex_quota_viewer"
