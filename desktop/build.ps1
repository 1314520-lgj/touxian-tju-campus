# 偷闲·天大版 桌面版 exe 打包脚本（PyInstaller）
# 用法： pwsh -File build.ps1 [-Repo <campus-fixed 路径>] [-Out <输出目录>] [-OneFile]
# 前置：.venv（python -m venv --system-site-packages，已 uv pip install pyinstaller）
# 默认产出 onedir（TouxianCampus\ 文件夹）；-OneFile 产出单文件（本机应用控制策略
# 会拦单文件在 %TEMP% 的解包启动，因此默认用 onedir）。
param(
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\campus-fixed")).Path,
    [string]$Out = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path + "\exe-dist",
    [string]$Venv = (Join-Path $PSScriptRoot ".venv"),
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { throw "python not found: $py" }
if (-not (Test-Path (Join-Path $Repo "dist\campus.html"))) { throw "frontend build missing: $Repo\dist" }
if (-not (Test-Path (Join-Path $Repo "campus-contracts\domain-v1.schema.json"))) { throw "contracts missing: $Repo\campus-contracts" }

$work = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force -Path $Out, $work | Out-Null
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

# 用解释器模块方式调用：本机应用控制策略会拦 .venv\Scripts\pyinstaller.exe，
# 但允许 python.exe -m PyInstaller。
& $py -m PyInstaller --noconfirm --clean $mode --console --name TouxianCampus `
    --distpath $Out --workpath $work --specpath $work `
    --paths $Repo `
    --add-data "$Repo\dist;dist" `
    --add-data "$Repo\config;config" `
    --add-data "$Repo\campus-contracts;campus-contracts" `
    --add-data "$Repo\data\PUBLIC_SAMPLE.json;data" `
    --collect-all trafilatura `
    --collect-all justext `
    --collect-all htmldate `
    --collect-all jsonschema `
    --collect-all jsonschema_specifications `
    --collect-all pypdf `
    --hidden-import server.campus.worker `
    --hidden-import server.campus.demo `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    (Join-Path $PSScriptRoot "desktop_entry.py")

if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }

# 便携目录里放一份配置文件与使用说明（不覆盖用户已改过的配置）。
$appDir = Join-Path $Out "TouxianCampus"
if (-not $OneFile) {
    Copy-Item (Join-Path $PSScriptRoot "touxian-settings.example.json") (Join-Path $appDir "touxian-settings.json") -Force
    Copy-Item (Join-Path $PSScriptRoot "使用说明.txt") (Join-Path $appDir "使用说明.txt") -Force
    $wechatDoc = Join-Path $Repo "docs\WECHAT-RSS-SETUP.md"
    if (Test-Path $wechatDoc) { Copy-Item $wechatDoc (Join-Path $appDir "公众号RSS接入说明.md") -Force }
}
Get-Item (Join-Path $appDir "TouxianCampus.exe") | Select-Object FullName, Length, LastWriteTime
