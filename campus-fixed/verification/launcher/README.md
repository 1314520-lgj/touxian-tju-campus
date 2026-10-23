# t16：一键启动脚本（start-campus.ps1）修复记录

任务：让「右键 start-campus.ps1 → 用 PowerShell 运行」在 **Windows 自带的 PowerShell 5.1** 下真的可用。
修复前：脚本一份都不执行（解析阶段就挂）；首装依赖会卡死。

## 一、根因

| 现象 | 根因 |
|---|---|
| PS 5.1 报 11 个解析错误，脚本一行都不执行 | 文件是**无 BOM 的 UTF-8**。PS 5.1 只在有 BOM 时才按 UTF-8 读，否则按系统 ANSI 代码页解码 → 中文串变成乱码、引号不闭合 → 行 15/16/24 报「意外的标记」 |
| 首装依赖卡死 20 分钟 | pip 默认缓存目录 `%LOCALAPPDATA%\pip` 在本机是一个指向损坏 junction 的目录（实测目标 912,493,976 字节 / 1453 个文件）。pip 在坏缓存上会长时间不返回；网络本身正常（PyPI 可达） |

## 二、修法与实测（真实执行记录）

### 1. 编码：字节级加 UTF-8 BOM

- 修改前：4323 字节、无 BOM、74 个 CRLF、11 个解析错误（PS 5.1.26100）。
- 加了 pip 相关的代码之后（编辑工具写入会**丢掉 BOM**，所以顺序是：先用编辑工具改内容 → 最后用字节级写入补 BOM）：5305 字节、BOM 存在、89 个 CRLF、**0 个解析错误**。
- 字节级写入脚本只做一件事：在文件最前面加 3 个字节 `EF BB BF`，并断言「除这 3 字节外负载逐字节相同、CRLF 数量不变」。（普通文本写入不会加 BOM；本次实测编辑工具确实会把 BOM 去掉，所以 BOM 必须是最后一步。）
- BOM 存活验证：
  - `Copy-Item` 复制后 sha256 相同；
  - 打包成 zip 再解压后 sha256 相同（`db2634142166e59b` → 见下方复现命令）；
  - PS 5.1 `Get-Content -Raw` 能匹配到中文串 `请安装 Python 3.12 或更新版本，并加入 PATH。`（不乱码）。

### 2. 执行：脚本在 PS 5.1 下真的跑起来了

在 `C:\Windows\Temp`（故意用另一个工作目录，模拟解压后右键运行）执行：

```text
已注册官方来源；尚未实际采集。

登录邮箱（页面会直接显示验证码，不需要邮箱服务）：
  operator@tju.edu.cn
数据目录：<scratch-data-dir>
如果还没采集过，先在本窗口按 Ctrl+C，再用 -Collect 重新启动一次。
打开 http://127.0.0.1:8427 ；按 Ctrl+C 停止。
INFO:     Uvicorn running on http://127.0.0.1:8427
```

- 中文提示全部正常显示，无乱码；
- `init-sources` 真的执行了（说明解析与语句执行都正常）；
- 服务起来后 `GET /campus.html` → 200；
- 脚本里的 `Set-Location -LiteralPath $PSScriptRoot` 让它在别的工作目录下也能正确找到仓库；
  `.env`、`requirements-campus.lock.txt`、`data-operator` 等路径都走 `-LiteralPath`，含中文/空格的工作区路径实测正常（本机工作区路径含中文「默认工作区」）。
- 注意（非脚本问题）：父进程用 `*> file` 捕获输出时，PS 5.1 会把子进程 stderr 包成 NativeCommandError 记录；脚本本身继续执行（服务照常起来），双击运行时 stderr 直接进控制台，不受影响。

### 3. 首装依赖：`--no-cache-dir` + 镜像兜底

脚本里安装依赖的那段现在：

```powershell
$pipMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$pipIndex = $env:CAMPUS_PIP_INDEX_URL
$pipArgs = @('-m','pip','install','--no-cache-dir','-r','requirements-campus.lock.txt')
if ($pipIndex) { $pipArgs += @('-i',$pipIndex) }
& $python @pipArgs
if ($LASTEXITCODE -ne 0 -and $pipIndex -ne $pipMirror) {
    Write-Host ('依赖安装失败，改用镜像重试：' + $pipMirror)
    & $python -m pip install --no-cache-dir -i $pipMirror -r 'requirements-campus.lock.txt'
}
```

实测（venv 从零：`python -m venv` 6 秒，Python 3.12.14；58 个包）：

| 场景 | 命令 | 结果 |
|---|---|---|
| 从零安装，默认索引 + `--no-cache-dir` | `pip install --no-cache-dir -r requirements-campus.lock.txt` | **65 秒，退出码 0**（对照：不加 `--no-cache-dir` 时实测卡死 20 分钟） |
| 索引不可达（`CAMPUS_PIP_INDEX_URL` 指向无效域名） | 脚本 `-Setup` | 第一次失败 → 打印「依赖安装失败，改用镜像重试：https://pypi.tuna.tsinghua.edu.cn/simple」→ 镜像重试 `Successfully installed annotated-doc-0.0.5` → 继续到启动成功 |
| 安装完成后再跑 `-Setup` | 脚本 `-Setup` | `依赖安装完成。`，不重复下载 |

也就是说：坏缓存不再是问题（`--no-cache-dir` 绕开它），网络索引不可达时脚本会自己换清华镜像再试一次，而不是把学生丢在一个静默卡死的窗口里。

## 三、复现命令

```powershell
# 1) 解析错误必须为 0（PS 5.1，Windows 自带版本）
powershell -NoProfile -Command "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'start-campus.ps1'), [ref]$null, [ref]$e); Write-Host ('parse errors: ' + $e.Count); if ($e.Count) { exit 1 } else { exit 0 }"

# 2) BOM 存在、负载未被改动
$b = [System.IO.File]::ReadAllBytes('start-campus.ps1'); ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)

# 3) 中文不乱码
powershell -NoProfile -Command "(Get-Content -LiteralPath 'start-campus.ps1' -Raw) -match '请安装 Python 3.12 或更新版本'"

# 4) 真跑一次（在别的工作目录，模拟解压后右键运行；用一个没人占用的端口）
Set-Location $env:TEMP
$env:CAMPUS_DATA_DIR = '<scratch-data-dir>'
powershell -NoProfile -ExecutionPolicy Bypass -File "<repo>\start-campus.ps1" -Port 8427
```

## 四、边界与未验证

- 只在本机 Windows（PowerShell 5.1.26100）实测；没有在第二台干净 Windows 上复跑。
- **真机/全新机器**的「零依赖环境 + 双击运行」由 t13 的冷启动验收覆盖；这里验证的是脚本本身在 PS 5.1 下可解析、可执行、依赖安装不再卡死。
- `run-demo.bat` 未改（.bat 不受 PS 编码影响；实测它本身是纯 ASCII）。
- 仓库里其它 .ps1 全部复核过：只有 start-campus.ps1 曾经是「有中文 + 无 BOM」；`tools/install_daily_task.ps1`、`deploy/cloudrun/deploy.ps1` 本来就有 BOM，`verification/mobile/*.ps1`、`deploy/cloudrun/*.ps1` 是纯 ASCII，扫描后 0 个解析错误。
- 首装依赖耗时受网络影响：本机默认索引 65 秒；换镜像路径只验证了「失败后能自动切换并装成一个包」，没有在全新 venv 上完整跑一遍镜像安装。