; Inno Setup 6 script for the LLM Gateway CLI v2 Windows installer.
;
; Packages the PyInstaller output (dist\gateway-cli-cowork-suite) into a single
; self-contained setup.exe. The result is fully offline: no Python, no pip,
; no network access required on the target machine.
;
; Compile from the repository root (after running the PyInstaller build):
;   ISCC.exe packaging\installer.iss
; Override the version stamped into the installer:
;   ISCC.exe /DAppVersion=1.2.3 packaging\installer.iss
;
; End users install interactively by double-clicking, or silently with:
;   gateway-cli-cowork-setup-<version>.exe /VERYSILENT /NORESTART

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "LLM Gateway CLI (Cowork)"
#define AppPublisher "Your Organization"
#define DistDir "..\dist\gateway-cli-cowork-suite"

[Setup]
; Change this GUID once for your product and then never again -- it is how
; Windows recognises upgrades of the same application.
AppId={{806B6437-E3FA-47E8-8AD2-D0B85D8FC60D}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Fixed installation path. The directory-selection page is hidden
; (DisableDirPage=yes) so every install lands in exactly this machine-wide
; location; the path cannot be changed from the wizard. C:\Gateway-CLI-Cowork is
; a shared, machine-readable location every user can execute — required by the
; machine-wide (HKLM) policy scope, which points all users at the helper here.
DefaultDirName=C:\Gateway-CLI-Cowork
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=gateway-cli-cowork-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Admin-only, all-users install. The fixed install path (C:\Gateway-CLI-Cowork)
; is a machine-wide, root-level directory that only an elevated install can
; create, so the per-user (non-admin) downgrade is intentionally NOT offered
; (no PrivilegesRequiredOverridesAllowed) — a per-user install would fail to
; create the fixed path and could not write the system PATH entry.
PrivilegesRequired=admin
ChangesEnvironment=yes
UninstallDisplayIcon={app}\gateway-cli-cowork.exe
; Explicit Add/Remove-Programs + uninstaller-window label. Set to the full
; product name so the Cowork entry is never confused with the Claude Code CLI
; (which ships its own, separately-named uninstaller). Cosmetic only -- unlike
; AppId, this can change freely without orphaning installs. Kept byte-identical
; to _EXPECTED_DISPLAY_NAME in cli/cowork_uninstall.py, which validates it.
UninstallDisplayName={#AppName}
; Guarantee the "Installed apps" (Add/Remove Programs) listing. Both default to
; yes in Inno; set explicitly so a future edit cannot silently drop the ARP entry.
; Uninstallable=yes builds unins###.exe; CreateUninstallRegKey=yes writes the ARP
; key HKLM\...\Uninstall\<AppId>_is1 (all-users, since this is an admin install),
; which is what Settings -> Apps -> Installed apps reads.
Uninstallable=yes
CreateUninstallRegKey=yes
; Fields shown in the Installed apps list. AppVersion -> "Version", AppPublisher ->
; "Publisher"; UninstallDisplayIcon (below) supplies the icon; InstallLocation is
; recorded automatically as {app}.
AppVerName={#AppName} {#AppVersion}
WizardStyle=modern
; Inno Setup 6 hides the Welcome page by default (DisableWelcomePage=yes). We use
; that page as the pre-install overview (see WelcomeLabel2 in [Messages]), so it
; must be explicitly re-enabled or the overview never shows.
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; Overview shown on the wizard's FIRST (Welcome) page, before installation — an
; at-a-glance summary of what this installer does and the one follow-up step the
; user must run afterwards. This reuses the built-in Welcome page (Option A) rather
; than adding a separate page, so there is no extra click. Keep it in sync with the
; actual behaviour below: fixed all-users path, automatic PATH, admin-only, and the
; registry-scope choice on the next page. [name/ver] expands to AppName + AppVersion.
; ASCII only + %n for line breaks (inline messages are not a UTF-8 file).
WelcomeLabel2=This wizard installs [name/ver] on this PC.%n%nWhat it does:%n  - Installs the CLI to C:\Gateway-CLI-Cowork (all users) and adds that folder to PATH (system + your account).%n  - On the next page you choose the gateway policy scope: machine-wide (HKLM, all users) or per-user (HKCU).%n%nAfter installing:%n  - Make sure Claude Desktop (Cowork) is installed and signed in.%n  - Run "gateway-cli-cowork setup" to point Claude Desktop at the gateway. Setup requests administrator rights automatically (UAC).%n%nThis is an all-users install and needs administrator rights. Click Next to continue.

[Tasks]
; The registry-scope group is listed FIRST so it sits directly under the
; guidance paragraph added at the top of the tasks page (see InitializeWizard).
;
; Registry policy scope. This installer does NOT write the policy itself (see the
; [Code] note) — it records this choice in {app}\registry-scope.conf, which
; `gateway-cli-cowork setup` reads to decide which hive to write. The two options
; are mutually exclusive (Flags: exclusive), so the user picks exactly one.
;
; Default: the machine-wide (HKLM) task is pre-selected — but only in a per-machine
; (admin/all-users) install, where {app} lands in a shared, machine-readable
; location (Program Files) that every user can execute. In a per-user install the
; machine task is hidden (Check: IsAdminInstallMode) and neither task is pre-checked,
; so SelectedRegistryScope falls back to the per-user (HKCU) scope by design.
Name: "scopemachine"; \
    Description: "Machine-wide policy (HKLM) — all users on this PC; overrides per-user; requires the helper-script credential"; \
    GroupDescription: "Gateway registry policy scope (applied when you run setup):"; \
    Flags: exclusive; \
    Check: IsAdminInstallMode
Name: "scopeuser"; \
    Description: "Per-user policy (HKCU) — each user runs setup in their own session"; \
    GroupDescription: "Gateway registry policy scope (applied when you run setup):"; \
    Flags: exclusive unchecked
; NOTE: there is no "add to PATH" task — the install directory is registered on
; PATH automatically (both the system and per-user environments) at post-install.
; See AddDirToPath in [Code].

[Files]
; Ship the entire PyInstaller onedir output: the two exes (gateway-cli-cowork,
; api-key-helper) plus the shared _internal runtime folder. (statusline.exe
; was dropped in the Cowork conversion; the wildcard ships whatever dist holds.)
Source: "{#DistDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; The scope preference file is written at post-install by CurStepChanged (it is not
; a [Files] entry), so remove it explicitly on uninstall.
Type: files; Name: "{app}\registry-scope.conf"

[Icons]
Name: "{group}\Gateway CLI (Command Prompt)"; \
    Filename: "{cmd}"; Parameters: "/K ""cd /d {app}"""; \
    WorkingDir: "{app}"; Comment: "Command prompt in the Gateway CLI directory"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{cmd}"; Parameters: "/K ""{app}\gateway-cli-cowork.exe"" --help"; \
    Description: "Show gateway-cli-cowork usage"; \
    Flags: postinstall skipifsilent unchecked

[Code]
// This installer deliberately writes NO Cowork policy keys. The gateway config
// lives in the LAUNCHING USER's HKCU\SOFTWARE\Policies\Claude, and this setup
// runs elevated (PrivilegesRequired=admin) — so any HKCU write here would land
// in the elevated/Administrator hive, NOT the target user's (HKCU is an alias
// to the *caller's* SID). The user therefore provisions the policy themselves
// by running `gateway-cli-cowork setup` from an elevated prompt in their
// OWN interactive session (not SSM/SYSTEM/another admin's hive), which writes
// the correct per-user hive and relaunches Claude Desktop. That also makes a
// multi-user box work: each user runs setup once. See Codex #2.
//
// What the wizard's registry-scope choice DOES: it only records the operator's
// selection (user/machine) into {app}\registry-scope.conf. The actual write still
// happens later, in the user's own elevated `setup` run — which reads that file
// and targets HKCU (per-user) or HKLM (machine-wide) accordingly. Keeping the
// write in `setup` is what lets the machine (HKLM) mode work too: HKLM is
// machine-scoped, so a later elevated setup writes it correctly for all users.
//
// Append the install dir to PATH on install and remove it on uninstall. This is
// automatic (no task/checkbox) and targets BOTH environments: the system PATH
// (HKLM, all users — written only in an elevated/admin install) and the current
// user's PATH (HKCU). Caveat: under an elevated install HKCU is the elevating
// account's hive, so the per-user PATH entry lands on that account — the same
// hive-aliasing caveat noted for the policy write above. The machine-wide system
// PATH is what makes the tool resolvable for every user of this PC.

const
  MachineEnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  UserEnvKey = 'Environment';
  RegistryScopeFile = 'registry-scope.conf';

// The Claude Desktop (Cowork) MSIX package family. The gateway config is useless
// without the app installed, so we preflight for it and warn (non-fatal) rather
// than fail — an admin may be staging the CLI ahead of the app rollout.
function ClaudeDesktopInstalled: Boolean;
var
  Output: AnsiString;
  ResultCode: Integer;
  TempFile: string;
begin
  Result := False;
  TempFile := ExpandConstant('{tmp}\claude-msix-probe.txt');
  // Query the current user's MSIX packages for the Claude family.
  if Exec('powershell.exe',
          '-NoProfile -NonInteractive -Command "if (Get-AppxPackage -Name ''*Claude*'') { ''yes'' | Out-File -Encoding ascii -FilePath ''' + TempFile + ''' }"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if LoadStringFromFile(TempFile, Output) then
      Result := Pos('yes', Lowercase(Trim(String(Output)))) > 0;
    DeleteFile(TempFile);
  end;
end;

function PathContains(const Path, Dir: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') > 0;
end;

// Add Dir to the Path value of one specific environment hive (idempotent).
procedure AddDirToPathHive(RootKey: Integer; const SubKey, Dir: string);
var
  Path: string;
begin
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Path) then
    Path := '';
  if PathContains(Path, Dir) then
    exit;
  if (Path <> '') and (Copy(Path, Length(Path), 1) <> ';') then
    Path := Path + ';';
  Path := Path + Dir;
  RegWriteExpandStringValue(RootKey, SubKey, 'Path', Path);
end;

// Remove Dir from the Path value of one specific environment hive.
procedure RemoveDirFromPathHive(RootKey: Integer; const SubKey, Dir: string);
var
  Path, Needle: string;
  P: Integer;
begin
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Path) then
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
  RegWriteExpandStringValue(RootKey, SubKey, 'Path', Path);
end;

// Register Dir on PATH in BOTH the system (HKLM) and current-user (HKCU)
// environments. The system hive is writable only in an elevated/admin install.
procedure AddDirToPath(const Dir: string);
begin
  if IsAdminInstallMode then
    AddDirToPathHive(HKEY_LOCAL_MACHINE, MachineEnvKey, Dir);
  AddDirToPathHive(HKEY_CURRENT_USER, UserEnvKey, Dir);
end;

// Unregister Dir from PATH in both the system (HKLM) and current-user (HKCU)
// environments (mirrors AddDirToPath).
procedure RemoveDirFromPath(const Dir: string);
begin
  if IsAdminInstallMode then
    RemoveDirFromPathHive(HKEY_LOCAL_MACHINE, MachineEnvKey, Dir);
  RemoveDirFromPathHive(HKEY_CURRENT_USER, UserEnvKey, Dir);
end;

// The scope the user selected in the wizard: 'machine' (HKLM) or 'user' (HKCU).
// The wizard pre-selects the machine (HKLM) task in a per-machine (admin) install,
// so that is the interactive default there. This function still resolves to 'user'
// — the safe per-user hive — whenever the machine task is unset: a per-user install
// (task hidden), a deselected machine task, or a silent install that passes neither
// /TASKS token.
//
// Defence in depth for R2-2: machine scope is ONLY honoured for a per-machine
// (admin) install. A per-user install lands the helper under the user's profile,
// which other users cannot execute, so a machine-wide policy pointing at it would
// fail auth for everyone else. Even if a silent install passes /TASKS=scopemachine
// in per-user mode, force 'user' here (the task is also hidden via Check above, and
// the CLI's build_config rejects a user-profile helper under machine scope).
function SelectedRegistryScope: string;
begin
  if WizardIsTaskSelected('scopemachine') and IsAdminInstallMode then
    Result := 'machine'
  else
    Result := 'user';
end;

// Record the choice next to the exe so `gateway-cli-cowork setup` can read it.
procedure WriteRegistryScopeFile;
var
  ScopePath: string;
begin
  ScopePath := ExpandConstant('{app}\' + RegistryScopeFile);
  if not SaveStringToFile(ScopePath, SelectedRegistryScope, False) then
    Log('Could not write registry-scope preference file: ' + ScopePath);
end;

// Show a short guidance paragraph at the top of the "Select Additional Tasks"
// selection box, explaining the registry-scope choice. Inno's task list only
// takes one-line descriptions, so the explanation lives here as a wrapped
// static label; the task checklist is nudged down to make room for it.
// Replace the static Welcome overview label with a read-only, word-wrapped memo
// that has a vertical scroll bar. WelcomeLabel2 is a plain TLabel: when the
// overview is longer than its fixed rectangle (small screens / high DPI / large
// system font) the tail is simply clipped with no way to reach it. A TNewMemo
// scrolls, so the full text is always reachable. The memo reuses WelcomeLabel2's
// caption (single source of truth = the [Messages] WelcomeLabel2 entry) and its
// on-screen rectangle, so layout is unchanged apart from gaining a scroll bar.
procedure MakeWelcomeScrollable;
var
  WelcomeMemo: TNewMemo;
begin
  if WizardForm.WelcomeLabel2 = nil then
    exit;
  WelcomeMemo := TNewMemo.Create(WizardForm);
  WelcomeMemo.Parent := WizardForm.WelcomeLabel2.Parent;
  WelcomeMemo.Left := WizardForm.WelcomeLabel2.Left;
  WelcomeMemo.Top := WizardForm.WelcomeLabel2.Top;
  WelcomeMemo.Width := WizardForm.WelcomeLabel2.Width;
  WelcomeMemo.Height := WizardForm.WelcomeLabel2.Height;
  WelcomeMemo.ReadOnly := True;
  WelcomeMemo.WordWrap := True;
  WelcomeMemo.ScrollBars := ssVertical;
  WelcomeMemo.TabStop := False;
  WelcomeMemo.Color := clWindow;
  // Reuse the already-expanded overview text (%n -> line breaks) so the wording
  // lives in exactly one place: the [Messages] WelcomeLabel2 entry.
  WelcomeMemo.Lines.Text := WizardForm.WelcomeLabel2.Caption;
  // Hide the original clipping label; the memo now carries the overview.
  WizardForm.WelcomeLabel2.Visible := False;
end;

procedure InitializeWizard;
var
  ScopeNote: TNewStaticText;
  GapY: Integer;
begin
  MakeWelcomeScrollable;

  ScopeNote := TNewStaticText.Create(WizardForm);
  ScopeNote.Parent := WizardForm.TasksList.Parent;
  ScopeNote.Left := WizardForm.TasksList.Left;
  ScopeNote.Top := WizardForm.TasksList.Top;
  ScopeNote.Width := WizardForm.TasksList.Width;
  // WordWrap + AutoSize lets the label compute its own height for the fixed
  // width, so the paragraph fits regardless of DPI/font metrics.
  ScopeNote.WordWrap := True;
  ScopeNote.AutoSize := True;
  ScopeNote.Caption :=
    'Choose where the gateway policy is applied. Machine-wide (HKLM) configures'
    + ' every user on this PC from a single elevated setup and takes precedence'
    + ' over per-user settings — choose this for shared or centrally-managed'
    + ' machines. Per-user (HKCU) applies only to the account that runs setup —'
    + ' choose this when each user manages their own configuration. The'
    + ' machine-wide option is offered only in an all-users (admin) install;'
    + ' a per-user install always uses HKCU.';
  GapY := ScopeNote.Height + ScaleY(8);
  WizardForm.TasksList.Top := WizardForm.TasksList.Top + GapY;
  WizardForm.TasksList.Height := WizardForm.TasksList.Height - GapY;
end;

// Build the "Ready to Install" summary. The dir- and group-selection pages are
// disabled (fixed install path, no start-menu choice), so Inno's default ready
// memo would be nearly empty — just the selected task. Compose an explicit,
// complete summary instead: what gets installed, where, the PATH changes, the
// registry-scope choice, and the one manual follow-up step. This is what the
// user reviews before committing, so it must fully describe the outcome.
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: string;
begin
  S := 'Program:' + NewLine
     + Space + '{#AppName} {#AppVersion}' + NewLine + NewLine;

  S := S + 'Install location (fixed, all users):' + NewLine
     + Space + ExpandConstant('{app}') + NewLine + NewLine;

  S := S + 'PATH:' + NewLine
     + Space + 'Adds the install folder to the system PATH (all users)' + NewLine
     + Space + 'and to your account PATH, so "gateway-cli-cowork" resolves' + NewLine
     + Space + 'in any new terminal.' + NewLine + NewLine;

  S := S + 'Gateway registry policy scope (recorded now, applied when you' + NewLine
     + 'run setup):' + NewLine;
  if SelectedRegistryScope = 'machine' then
    S := S + Space + 'MACHINE-WIDE (HKLM) — all users on this PC; overrides' + NewLine
       + Space + 'per-user settings.' + NewLine + NewLine
  else
    S := S + Space + 'PER-USER (HKCU) — only the account that runs setup.' + NewLine + NewLine;

  if MemoTasksInfo <> '' then
    S := S + 'Selected tasks:' + NewLine + MemoTasksInfo + NewLine + NewLine;

  S := S + 'After installing (manual step):' + NewLine
     + Space + '1. Install Claude Desktop (Cowork) and sign in.' + NewLine
     + Space + '2. From an ELEVATED prompt in your own Windows session, run:' + NewLine
     + Space + Space + 'gateway-cli-cowork setup' + NewLine
     + Space + 'to point Claude Desktop at the gateway and relaunch it.';

  Result := S;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // Automatically register the install dir on PATH (system + per-user).
  if CurStep = ssPostInstall then
    AddDirToPath(ExpandConstant('{app}'));

  // Persist the registry-scope choice for the user's later `setup` run.
  if CurStep = ssPostInstall then
    WriteRegistryScopeFile;

  // Warn (non-fatal) if Claude Desktop is not present, and only in interactive
  // installs — a silent fleet install must never block on a message box.
  if (CurStep = ssPostInstall) and (not WizardSilent) then
  begin
    if not ClaudeDesktopInstalled then
      MsgBox('Claude Desktop (Cowork) does not appear to be installed for this user.'
        + #13#10#13#10
        + 'Install it and complete its first-run sign-in, then run'
        + #13#10 + '    gateway-cli-cowork setup'
        + #13#10 + 'from an ELEVATED command prompt in your own Windows session to'
        + #13#10 + 'point it at the gateway. (Setup writes the registry policy key,'
        + #13#10 + 'which requires elevation. For the per-user/HKCU scope, run it as'
        + #13#10 + 'yourself — not via SSM/SYSTEM or a different administrator account,'
        + #13#10 + 'or it lands in the wrong hive. The machine-wide/HKLM scope applies'
        + #13#10 + 'to all users and overrides per-user policies.)'
        + #13#10#13#10
        + 'Selected registry scope: ' + Uppercase(SelectedRegistryScope),
        mbInformation, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveDirFromPath(ExpandConstant('{app}'));
end;
