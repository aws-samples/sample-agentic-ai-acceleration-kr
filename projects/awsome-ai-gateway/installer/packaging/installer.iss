; Inno Setup 6 script for the LLM Gateway CLI v2 Windows installer.
;
; Packages the PyInstaller output (dist\gateway-cli-suite) into a single
; self-contained setup.exe. The result is fully offline: no Python, no pip,
; no network access required on the target machine.
;
; Compile from the repository root (after running the PyInstaller build):
;   ISCC.exe packaging\installer.iss
; Override the version stamped into the installer:
;   ISCC.exe /DAppVersion=1.2.3 packaging\installer.iss
;
; End users install interactively by double-clicking, or silently with:
;   gateway-cli-setup-<version>.exe /VERYSILENT /NORESTART

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "LLM Gateway CLI"
#define AppPublisher "Your Organization"
#define DistDir "..\dist\gateway-cli-suite"

[Setup]
; Change this GUID once for your product and then never again -- it is how
; Windows recognises upgrades of the same application.
AppId={{9B7C2E64-5D1A-4F3B-8C0E-2A6F41D95B37}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\GatewayCLI
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=gateway-cli-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Allow non-admin (per-user) installs on locked-down workstations; admins
; still get a per-machine install under Program Files.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
UninstallDisplayIcon={app}\gateway-cli.exe
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add the install directory to PATH (recommended)"; \
    GroupDescription: "Command line:"

[Files]
; Ship the entire PyInstaller onedir output: the three exes plus the shared
; _internal runtime folder.
Source: "{#DistDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Gateway CLI (Command Prompt)"; \
    Filename: "{cmd}"; Parameters: "/K ""cd /d {app}"""; \
    WorkingDir: "{app}"; Comment: "Command prompt in the Gateway CLI directory"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{cmd}"; Parameters: "/K ""{app}\gateway-cli.exe"" --help"; \
    Description: "Show gateway-cli usage"; \
    Flags: postinstall skipifsilent unchecked

[Code]
// Append the install dir to PATH on install and remove it on uninstall.
// Handles both per-machine (HKLM) and per-user (HKCU) installs.

const
  MachineEnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  UserEnvKey = 'Environment';

function EnvRootKey: Integer;
begin
  if IsAdminInstallMode then
    Result := HKEY_LOCAL_MACHINE
  else
    Result := HKEY_CURRENT_USER;
end;

function EnvSubKey: string;
begin
  if IsAdminInstallMode then
    Result := MachineEnvKey
  else
    Result := UserEnvKey;
end;

function PathContains(const Path, Dir: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') > 0;
end;

procedure AddDirToPath(const Dir: string);
var
  Path: string;
begin
  if not RegQueryStringValue(EnvRootKey, EnvSubKey, 'Path', Path) then
    Path := '';
  if PathContains(Path, Dir) then
    exit;
  if (Path <> '') and (Copy(Path, Length(Path), 1) <> ';') then
    Path := Path + ';';
  Path := Path + Dir;
  RegWriteExpandStringValue(EnvRootKey, EnvSubKey, 'Path', Path);
end;

procedure RemoveDirFromPath(const Dir: string);
var
  Path, Needle: string;
  P: Integer;
begin
  if not RegQueryStringValue(EnvRootKey, EnvSubKey, 'Path', Path) then
    exit;
  Needle := ';' + Uppercase(Dir) + ';';
  P := Pos(Needle, ';' + Uppercase(Path) + ';');
  if P = 0 then
    exit;
  { P is 1-based within the ';'-wrapped string; translate to the original. }
  Delete(Path, P, Length(Dir) + 1);
  { The delete above may leave a leading/trailing separator; tidy it up. }
  if Copy(Path, 1, 1) = ';' then
    Delete(Path, 1, 1);
  if (Path <> '') and (Copy(Path, Length(Path), 1) = ';') then
    Delete(Path, Length(Path), 1);
  RegWriteExpandStringValue(EnvRootKey, EnvSubKey, 'Path', Path);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddDirToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveDirFromPath(ExpandConstant('{app}'));
end;
