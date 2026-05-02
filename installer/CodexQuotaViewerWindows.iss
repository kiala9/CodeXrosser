#ifndef SourceDir
#define SourceDir "..\artifacts\publish"
#endif

[Setup]
AppId={{C9D5912C-91D7-4CA8-82B0-56790A01B4D8}
AppName=Codex Quota Viewer Windows
AppVersion=0.1.0
AppPublisher=Codex Quota Viewer
DefaultDirName={localappdata}\Programs\CodexQuotaViewerWindows
DefaultGroupName=Codex Quota Viewer Windows
OutputDir=Output
OutputBaseFilename=CodexQuotaViewerWindows-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupIconFile={#SourceDir}\_internal\codex_quota_viewer\assets\cqv-app-icon.ico
UninstallDisplayIcon={app}\CodexQuotaViewerWindowsQt.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Codex Quota Viewer Windows"; Filename: "{app}\CodexQuotaViewerWindowsQt.exe"
Name: "{userdesktop}\Codex Quota Viewer Windows"; Filename: "{app}\CodexQuotaViewerWindowsQt.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\CodexQuotaViewerWindowsQt.exe"; Description: "Launch Codex Quota Viewer Windows"; Flags: nowait postinstall skipifsilent
