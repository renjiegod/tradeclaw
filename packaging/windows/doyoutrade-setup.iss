; DoYouTrade Windows 图形安装包（Inno Setup）。
;
; 只是把仓库根 install.ps1 的安装逻辑包进一个"下一步下一步完成"的向导：
;   1. 拷贝 install.ps1 + 启动器脚本到安装目录
;   2. 静默调用 install.ps1 -Force（检测/装 uv → uv tool install doyoutrade[qmt-proxy]）
;   3. 创建开始菜单 / 桌面快捷方式，指向启动器脚本（启动 doyoutrade + 自动开浏览器）
;
; 本地编译（需要先装 https://jrsoftware.org/isinfo.php）：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\doyoutrade-setup.iss
; CI 里由 .github/workflows/build-windows-installer.yml 传入 /DMyAppVersion=<版本号>。
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
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
Source: "..\..\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launch-doyoutrade.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\DoYouTrade"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 13
Name: "{group}\卸载 DoYouTrade"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DoYouTrade"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 13; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Force"; \
    StatusMsg: "正在安装 DoYouTrade（首次安装需要拉取依赖，可能要几分钟，请勿关闭窗口）..."; \
    Flags: runascurrentuser waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 DoYouTrade"; Flags: postinstall nowait skipifsilent shellexec

[UninstallRun]
; 卸载向导只删自己装的 install.ps1 / 启动器脚本；uv tool install 的 doyoutrade 命令
; 本体是 uv 管的，一并清掉，避免"卸载了安装包，命令还在"的困惑。uv 不在了就跳过，不报错。
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""if (Get-Command uv -ErrorAction SilentlyContinue) { uv tool uninstall doyoutrade }"""; \
    Flags: runascurrentuser waituntilterminated; RunOnceId: "UninstallDoYouTradeTool"
