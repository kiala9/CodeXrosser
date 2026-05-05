#ifndef SourceDir
#define SourceDir "..\artifacts\publish"
#endif

[Setup]
AppId={{C9D5912C-91D7-4CA8-82B0-56790A01B4D8}
AppName=CodeXrosser
AppVersion=0.2.0
AppPublisher=CodeXrosser
DefaultDirName={localappdata}\Programs\CodeXrosser
DefaultGroupName=CodeXrosser
OutputDir=Output
OutputBaseFilename=CodeXrosser-Setup
UsePreviousGroup=no
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupIconFile={#SourceDir}\_internal\codex_quota_viewer\assets\cqv-app-icon.ico
UninstallDisplayIcon={app}\CodeXrosser.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\CodexQuotaViewerWindowsQt.exe"
Type: files; Name: "{app}\CodeXross.exe"
Type: files; Name: "{userdesktop}\Codex Quota Viewer Windows.lnk"
Type: files; Name: "{userdesktop}\CodeXross.lnk"
Type: files; Name: "{userprograms}\Codex Quota Viewer Windows\Codex Quota Viewer Windows.lnk"
Type: files; Name: "{userprograms}\CodeXross\CodeXross.lnk"
Type: dirifempty; Name: "{userprograms}\Codex Quota Viewer Windows"
Type: dirifempty; Name: "{userprograms}\CodeXross"

[Icons]
Name: "{group}\CodeXrosser"; Filename: "{app}\CodeXrosser.exe"
Name: "{userdesktop}\CodeXrosser"; Filename: "{app}\CodeXrosser.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\CodeXrosser.exe"; Description: "Launch CodeXrosser"; Flags: nowait postinstall skipifsilent
