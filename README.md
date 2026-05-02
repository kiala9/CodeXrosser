# CodeXrosser

CodeXrosser is a Windows desktop utility for managing multiple Codex identities and working safely across isolated Codex environments.

It provides a tray-first Qt interface for viewing account status, switching between ChatGPT/OAuth and OpenAI-compatible API-key accounts, and keeping a sandboxed Codex home separate from the real one.

## Features

- Account dashboard for ChatGPT/OAuth and API-key profiles.
- Quota/status display where the selected account supports it.
- Safe account switching with restore points.
- Sandbox mode for testing account changes without touching the real Codex home.
- Optional Real mode for applying changes to the user's actual Codex configuration.
- Session and shared asset synchronization between sandbox and real environments.
- Windows tray integration and a PySide6 desktop UI.

## Safety Model

CodeXrosser defaults to Sandbox mode. In this mode, account switches and session experiments happen inside an isolated Codex home managed by the app.

Real mode targets the user's actual Codex configuration and should be used deliberately. Before writing to Real mode, the app creates restore points so changes can be rolled back.

Do not commit local account data, generated builds, restore points, logs, virtual environments, or test artifacts. The repository `.gitignore` is set up to keep those files out of Git.

## Repository Layout

```text
src/CodexQuotaViewerWindows.Qt/   Active PySide6 application
tests/python/                     Python tests for the Qt implementation
scripts/                          Development, publish, and installer scripts
installer/                        Inno Setup installer definition
vendor/CodexMM/                   Vendored session-management source
tools/                            Project utilities
```

Some paths and build artifact names still use the earlier internal identifier. The public project name is CodeXrosser.

## Requirements

- Windows 10 or newer
- Python 3.10 or newer
- PowerShell
- Inno Setup, only when building the installer

Python dependencies are listed in `requirements.txt`.

## Development

Prepare the virtual environment without launching the app:

```powershell
.\scripts\run-dev.ps1 -NoLaunch
```

Launch the development app:

```powershell
.\scripts\run-dev.ps1
```

Launch without closing an already running development instance:

```powershell
.\scripts\run-dev.ps1 -KeepExisting
```

## Verification

Run a lightweight syntax check:

```powershell
.\.venv\Scripts\python.exe -m compileall .\src\CodexQuotaViewerWindows.Qt\codex_quota_viewer .\tests\python
```

Run targeted tests when changing behavior:

```powershell
.\.venv\Scripts\python.exe -m pytest .\tests\python
```

## Build

Publish a PyInstaller one-folder build:

```powershell
.\scripts\publish.ps1
```

The published app is written to:

```text
artifacts\publish
```

Build the Windows installer:

```powershell
.\scripts\build-installer.ps1
```

Installer output is written under:

```text
installer\Output
```

## Configuration

Useful environment variables:

```text
CQV_CODEX_COMMAND                Override the Codex CLI path
CQV_CODEX_APP_COMMAND            Override the Codex Desktop app path
CODEX_QUOTA_VIEWER_STORAGE_ROOT  Override the app storage root
```

These names are currently retained for compatibility.

## License

No license has been declared yet.
