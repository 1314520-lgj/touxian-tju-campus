; 偷闲·天大版 安装脚本（Inno Setup 6）
; 安装：官网采集（34 个官方栏目）+ 微信公众号（本机 weread-omni 导出 feed，全程不经第三方中转）
; 用 ISCC.exe 编译。启动器文件名保持 ASCII（cmd.exe 按控制台代码页解析 .bat）。

#define MyAppName "偷闲·天大版"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "偷闲"
#define MyBundle "..\exe-dist"

[Setup]
AppId={{8D3A2F61-6E4B-4B7E-9E59-0F2B2C1A7D31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\TouxianCampus
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#MyBundle}
OutputBaseFilename=TouxianCampus-Setup-{#MyAppVersion}
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\TouxianCampus\TouxianCampus.exe
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupWindowTitle=安装 - %1
WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=现在将安装 [name/ver] 到您的电脑。%n%n建议先关闭其它应用程序，然后继续。
WizardSelectDir=选择安装位置
SelectDirLabel3=安装程序将把 [name] 安装到以下文件夹。
SelectDirBrowseLabel=点击「下一步」继续；如需换一个文件夹，请点击「浏览」。
WizardSelectTasks=选择附加任务
SelectTasksDesc=请选择安装程序要执行的附加任务。
WizardReady=准备安装
ReadyLabel1=安装程序已准备好在您的电脑上安装 [name]。
ReadyLabel2a=点击「安装」开始安装；如需检查或修改设置，请点击「上一步」。
WizardInstalling=正在安装
InstallingLabel=请稍候，安装程序正在把 [name] 安装到您的电脑。
FinishedHeadingLabel=[name] 安装完成
FinishedLabelNoIcons=安装程序已完成 [name] 的安装。
FinishedLabel=安装程序已完成 [name] 的安装。
ClickFinish=点击「完成」退出安装程序。
ButtonNext=下一步(&N)
ButtonBack=上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消(&C)
ButtonBrowse=浏览(&R)...
ButtonYes=是(&Y)
ButtonNo=否(&N)
ExitSetupTitle=退出安装
ExitSetupMessage=安装尚未完成。现在退出将不会安装 [name]。%n%n确定要退出吗？
UninstallAppFullTitle=卸载 %1
ConfirmUninstall=确定要完全卸载 %1 及其组件吗？
UninstalledAll=%1 已成功从您的电脑上卸载。
UninstallStatusLabel=正在从您的电脑上卸载 %1，请稍候。
StatusExtractFiles=正在解压文件...
StatusCreateIcons=正在创建快捷方式...
StatusCreateRegistryEntries=正在创建注册表项...
StatusRegisterFiles=正在注册文件...
StatusSavingUninstall=正在保存卸载信息...
StatusRollback=正在回滚更改...
StatusRunProgram=正在完成安装...

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; 主程序（onedir）
Source: "{#MyBundle}\TouxianCampus\*"; DestDir: "{app}\TouxianCampus"; Flags: recursesubdirs createallsubdirs ignoreversion
; 公众号 feed 组件（weread-omni + 自带 node 运行时，全程本机）
Source: "{#MyBundle}\weread-feed\*"; DestDir: "{app}\weread-feed"; Flags: recursesubdirs createallsubdirs ignoreversion
; 启动器（ASCII 文件名，内容 UTF-8 BOM / 纯 ASCII）
Source: "{#MyBundle}\start.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyBundle}\launcher.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\偷闲·天大版"; Filename: "{app}\start.bat"; WorkingDir: "{app}"
Name: "{group}\公众号登录（微信扫码）"; Filename: "{app}\weread-feed\login.bat"; WorkingDir: "{app}\weread-feed"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\偷闲·天大版"; Filename: "{app}\start.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\start.bat"; Description: "立即启动偷闲·天大版"; Flags: postinstall shellexec nowait

[UninstallDelete]
Type: filesandordirs; Name: "{app}\TouxianCampus\data"
Type: filesandordirs; Name: "{app}\weread-feed\feed.json"
Type: filesandordirs; Name: "{app}\weread-feed\wechat-login-qr.png"
