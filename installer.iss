#define MyAppName "VoicePad"
#define MyAppVersion "1.0"

[Setup]
AppId={{B7C3A5E2-4D1F-4C8A-9E6B-2F0D7A1C5E93}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer
OutputBaseFilename=VoicePad-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\VoicePad.exe

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\VoicePad.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VoicePad"; Filename: "{app}\VoicePad.exe"
Name: "{group}\Uninstall VoicePad"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VoicePad"; Filename: "{app}\VoicePad.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VoicePad.exe"; Description: "{cm:LaunchProgram,VoicePad}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\vbcable"

[Code]
function Tr(const Ru, En: string): string;
begin
  if ActiveLanguage = 'russian' then
    Result := Ru
  else
    Result := En;
end;

function CableSetup(const Base: string): string;
begin
  Result := AddBackslash(Base) + 'vbcable\VBCABLE_Setup_x64.exe';
end;

procedure RemoveCable(const SetupPath: string);
var
  Code: Integer;
begin
  ShellExec('runas', SetupPath, '-u -h', ExtractFileDir(SetupPath), SW_SHOWNORMAL, ewWaitUntilTerminated, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  S1, S2, Data: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    S1 := CableSetup(ExpandConstant('{app}'));
    S2 := CableSetup(ExpandConstant('{%USERPROFILE}\.voicepad'));
    if (FileExists(S1) or FileExists(S2)) and (not UninstallSilent) then
    begin
      if MsgBox(Tr('Удалить также виртуальный микрофон VB-CABLE, который установила программа? Если им пользуются другие программы, они его потеряют. Windows попросит подтвердить удаление драйвера.',
                   'Also remove the VB-CABLE virtual microphone installed by this program? Other programs using it will lose it. Windows will ask you to confirm removing the driver.'),
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        if FileExists(S1) then RemoveCable(S1);
        if FileExists(S2) then RemoveCable(S2);
      end;
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{%USERPROFILE}\.voicepad');
    if DirExists(Data) and (not UninstallSilent) then
    begin
      if MsgBox(Tr('Удалить также настройки, список голосов и озвученные фразы?',
                   'Also delete settings, voice list and generated phrases?'),
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(Data, True, True, True);
    end;
  end;
end;
