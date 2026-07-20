; DoYouTrade Windows GUI installer (Inno Setup).
;
; Wraps repo-root install-win.ps1 (ASCII) -> install.ps1 (UTF-8 no BOM):
;   1. Copy install-win.ps1 + install.ps1 + launcher bat into {app}
;   2. From [Code], run install-win.ps1 -Force and abort on non-zero exit
;   3. Create Start Menu / desktop shortcuts to the launcher bat
;   4. Offer "立即启动" only when install succeeded (Check: InstallSucceeded)
;
; Build locally (requires https://jrsoftware.org/isinfo.php):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\doyoutrade-setup.iss
; CI passes /DMyAppVersion=<version> from .github/workflows/build-windows-installer.yml.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

#define MyAppName "DoYouTrade"
#define MyAppPublisher "DoYouTrade"
#define MyAppURL "https://github.com/renjiegod/doyoutrade"
#define MyAppExeName "launch-doyoutrade.bat"

[Setup]
AppId={{6F6E9E3E-3D7B-4C2B-9B7E-2B9C6D9A6E9A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\DoYouTrade
DefaultGroupName=DoYouTrade
DisableProgramGroupPage=yes
; uv tool install 装到当前用户目录（~/.local/bin），不需要管理员权限；
; 装成需要管理员的话，普通用户账户在公司电脑上大概率装不了。
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=dist
OutputBaseFilename=DoYouTrade-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\doyoutrade.ico
SetupIconFile=doyoutrade.ico
SetupLogging=yes

[Languages]
; 简体中文属于 Inno Setup「非官方翻译」，官方安装包不自带，故随仓库 vendored
; 一份 ChineseSimplified.isl（与本 .iss 同目录），用相对路径引用，避免依赖 CI 联网下载。
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
; install-win.ps1 is the Windows PowerShell 5.1 -File entrypoint (ASCII).
; It re-encodes install.ps1 (UTF-8 no BOM, required for irm|iex) to a
; UTF-8-BOM temp copy so Chinese Windows CP936 does not ParserError.
Source: "..\..\install-win.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launch-doyoutrade.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "doyoutrade.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\DoYouTrade"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\doyoutrade.ico"
Name: "{group}\卸载 DoYouTrade"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DoYouTrade"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\doyoutrade.ico"; Tasks: desktopicon

[Run]
; install-win.ps1 is invoked from [Code] (exit code checked). Only the
; optional post-install launch remains here, gated on InstallSucceeded.
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 DoYouTrade"; Flags: postinstall nowait skipifsilent shellexec; Check: InstallSucceeded

[UninstallRun]
; 卸载向导只删自己装的 install.ps1 / 启动器脚本；uv tool install 的 doyoutrade 命令
; 本体是 uv 管的，一并清掉，避免"卸载了安装包，命令还在"的困惑。uv 不在了就跳过，不报错。
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""if (Get-Command uv -ErrorAction SilentlyContinue) {{ uv tool uninstall doyoutrade }"""; \
    Flags: runascurrentuser waituntilterminated; RunOnceId: "UninstallDoYouTradeTool"

[Code]
var
  GInstallSucceeded: Boolean;

function InitializeSetup(): Boolean;
begin
  GInstallSucceeded := False;
  Result := True;
end;

function InstallSucceeded: Boolean;
begin
  Result := GInstallSucceeded;
end;

function RunInstallWinPs1(): Boolean;
var
  ResultCode: Integer;
  PsExe: String;
  Params: String;
begin
  PsExe := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install-win.ps1') + '" -Force -Version "{#MyAppVersion}"';
  if not Exec(PsExe, Params, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := False;
    Exit;
  end;
  Result := (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := '正在安装 DoYouTrade（首次安装需要拉取依赖，可能要几分钟，请勿关闭窗口）...';
    if not RunInstallWinPs1() then
    begin
      GInstallSucceeded := False;
      MsgBox(
        'DoYouTrade 安装失败。' + #13#10 + #13#10 +
        '失败原因与环境信息已直接打印在刚才的命令行窗口' + #13#10 +
        '（标题含「[诊断]」的黄色段落）。请根据该输出排查后重试。' + #13#10 + #13#10 +
        '常见原因：无法访问 GitHub / astral.sh，或杀毒软件拦截了脚本。',
        mbError, MB_OK);
      RaiseException('DoYouTrade 组件安装失败，安装已中止。');
    end;
    GInstallSucceeded := True;
  end;
end;
