; Inno Setup script for the Netlithic desktop app (TEST NETWORK).
[Setup]
AppId={{6B1E5C2A-4F0D-4E7B-9C1A-0A1B2C3D4E5F}
AppName=Netlithic (Test Network)
AppVersion=0.5.0
AppPublisher=Netlithic
DefaultDirName={localappdata}\Programs\Netlithic
DefaultGroupName=Netlithic
PrivilegesRequired=lowest
OutputBaseFilename=Netlithic-Setup
OutputDir=Output
SetupIconFile=netlithic.ico
UninstallDisplayIcon={app}\Netlithic.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
CloseApplicationsFilter=*.exe
RestartApplications=no
LicenseFile=..\LICENSE

[Files]
Source: "..\dist\Netlithic\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Netlithic"; Filename: "{app}\Netlithic.exe"
Name: "{userdesktop}\Netlithic"; Filename: "{app}\Netlithic.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Put a Netlithic icon on my desktop"

[Run]
Filename: "{app}\Netlithic.exe"; Description: "Open Netlithic now"; Flags: nowait postinstall skipifsilent
; Uninstalling keeps %LOCALAPPDATA%\Netlithic (the node data and the saved wallet) on purpose.

[Code]
// Make sure no Netlithic.exe (window or node) is still running before files are replaced or removed.
procedure KillNetlithic();
var Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM Netlithic.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillNetlithic();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  KillNetlithic();
  Result := True;
end;
