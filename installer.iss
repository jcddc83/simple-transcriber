[Setup]
AppName=Simple Transcriber
AppVersion=1.1
AppPublisher=James daSilva
DefaultDirName={localappdata}\SimpleTranscriber
DefaultGroupName=Simple Transcriber
OutputBaseFilename=SimpleTranscriber-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\SimpleTranscriber.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Simple Transcriber"; Filename: "{app}\SimpleTranscriber.exe"
Name: "{userdesktop}\Simple Transcriber"; Filename: "{app}\SimpleTranscriber.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Code]
function WebView2Installed(): Boolean;
var V: String;
begin
  Result := RegQueryStringValue(HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'pv', V) and (V <> '') and (V <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(HKCU,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; \
  Parameters: "/silent /install"; \
  StatusMsg: "Installing WebView2 runtime..."; \
  Check: not WebView2Installed
Filename: "{app}\SimpleTranscriber.exe"; \
  Description: "Launch Simple Transcriber"; \
  Flags: nowait postinstall skipifsilent
