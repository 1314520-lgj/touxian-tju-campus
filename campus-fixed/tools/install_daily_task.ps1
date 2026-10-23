<#
.SYNOPSIS
    把「偷闲·天大版」的每日更新挂成 Windows 计划任务：安装 / 卸载 / 查看状态。

.DESCRIPTION
    产品本身只在有人跑命令时才更新。这个脚本把 `scripts/daily_update.py --apply`
    （采集 → 复核 → 下架）注册成每天执行一次的持久任务，并把每次执行的输出写进
    数据目录的日志，让「持续更新」不再依赖谁记得手动跑。

    四个动作（互斥，必须选一个）：

        -Install    注册或更新任务（已存在则覆盖，幂等）
        -Uninstall  删除任务（日志保留，便于事后回看）
        -Status     打印任务状态与最近一次执行的摘要
        -RunNow     立即执行一次每日更新（计划任务内部使用，也可手工排障）

    为什么用 S4U 而不是「仅当用户登录时运行」：学生合上笔记本再打开，桌面会话可能
    根本不存在；S4U 的任务由任务计划服务在会话 0 里以该用户身份运行，不依赖登录
    会话，所以重启后依然会跑。本机已实测该方式可访问外网（oaa 200）。

    日志：<数据目录>\logs\daily-YYYYMMDD.log（同一天多次执行追加）
          <数据目录>\logs\daily-YYYYMMDD.json（同一批次的机器可读摘要，-Status 读它）
          <数据目录>\logs\last-run.json（最近一次执行的时间/退出码/日志位置）

    退出码：0 成功；1 失败或状态为「未安装」；2 用法错误；3 权限不足。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install

.EXAMPLE
    # 改成每天早上 08:15 跑（重复执行即更新，不会报「任务已存在」）
    powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install -Time 08:15

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status

.NOTES
    不要改 `scripts/daily_update.py`：它自己带进程锁，本脚本只是把它交给调度器。
    路径含中文与空格（本机就是 "默认工作区"），所有命令行参数一律加引号。
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$RunNow,

    # 每天触发时刻（24 小时制 HH:mm）。重复 -Install 会按新时刻更新任务。
    [string]$Time = '07:30',

    # 装齐依赖的解释器。缺省读环境变量 CAMPUS_PYTHON，再缺省 PATH 里的 python。
    [string]$Python = '',

    # 数据目录。缺省 <仓库根>\data-operator。
    [string]$DataDir = '',

    [string]$TaskName = 'TouxianCampusDaily',

    # 计划任务默认带 --gateway-from-config（本机代理 TUN 下唯一合规通路，
    # 见 docs/REAL-DATA-RUNBOOK.md 第 2 节）。直连校园网时用 -NoGateway 关掉。
    [switch]$NoGateway
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptPath = $PSCommandPath
if (-not $ScriptPath) { $ScriptPath = $MyInvocation.MyCommand.Path }
$ToolsDir = Split-Path -Parent $ScriptPath
$RepoRoot = Split-Path -Parent $ToolsDir
$DailyUpdate = Join-Path (Join-Path $RepoRoot 'scripts') 'daily_update.py'

$ExitOk = 0
$ExitFailed = 1
$ExitUsage = 2
$ExitPermission = 3

# ---------------------------------------------------------------------------
# 输出小工具：中文诊断要能被 5.1 正常显示（本文件以 UTF-8 BOM 保存）
# ---------------------------------------------------------------------------

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    $Message" -ForegroundColor Green }
function Write-Note([string]$Message) { Write-Host "    $Message" -ForegroundColor Gray }
function Write-Warn2([string]$Message) { Write-Host "警告 $Message" -ForegroundColor Yellow }
function Write-Problem([string]$Message) { Write-Host "错误 $Message" -ForegroundColor Red }

function Fail([string]$Message, [int]$Code) {
    Write-Problem $Message
    exit $Code
}

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Argument([string]$Value) {
    # 路径可能含空格与中文；结尾的反斜杠会和收尾引号黏在一起，先去干净。
    $trimmed = $Value.TrimEnd('\')
    return '"' + $trimmed + '"'
}

function Write-Utf8([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Add-Utf8([string]$Path, [string]$Text) {
    [System.IO.File]::AppendAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-Utf8([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

# ---------------------------------------------------------------------------
# 参数解析与前置校验
# ---------------------------------------------------------------------------

$actions = @()
if ($Install) { $actions += 'Install' }
if ($Uninstall) { $actions += 'Uninstall' }
if ($Status) { $actions += 'Status' }
if ($RunNow) { $actions += 'RunNow' }

if ($actions.Count -eq 0) {
    Write-Problem '必须指定一个动作：-Install、-Uninstall、-Status 或 -RunNow。'
    Write-Note '例：powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status'
    exit $ExitUsage
}
if ($actions.Count -gt 1) {
    Write-Problem ('一次只能做一个动作，收到：' + ($actions -join ' + '))
    exit $ExitUsage
}
$Action = $actions[0]

if (-not (Test-Path -LiteralPath $DailyUpdate)) {
    Fail ("找不到每日更新脚本：$DailyUpdate`n       仓库结构被改动过？本脚本必须在 <仓库根>\tools\ 下。") $ExitFailed
}

# 数据目录：显式参数 > CAMPUS_DATA_DIR > <仓库根>\data-operator
if (-not $DataDir) {
    if ($env:CAMPUS_DATA_DIR) { $DataDir = $env:CAMPUS_DATA_DIR } else { $DataDir = Join-Path $RepoRoot 'data-operator' }
}
if (-not [System.IO.Path]::IsPathRooted($DataDir)) { $DataDir = Join-Path $RepoRoot $DataDir }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)

function Test-PythonDependencies([string]$Path) {
    # 探针必须走「子进程 + 临时文件」，不能写 & $Path -c "..." 2>&1：
    # PowerShell 5.1 在 $ErrorActionPreference='Stop' 下会把子进程的 stderr
    # 当成终止错误，于是「缺 fastapi」会变成一段原生堆栈 + NativeCommandError，
    # 而不是可读的诊断。真实踩过：裸 -Install 就是这样退出 1 的。
    $result = [pscustomobject]@{ Ok = $false; Detail = '' }
    if (-not (Test-Path -LiteralPath $Path)) {
        $result.Detail = '解释器不存在'
        return $result
    }
    $stamp = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $tempDir = [System.IO.Path]::GetTempPath()
    $probe = Join-Path $tempDir ('touxian-probe-' + $stamp + '.py')
    $outFile = Join-Path $tempDir ('touxian-probe-' + $stamp + '.out')
    $errFile = Join-Path $tempDir ('touxian-probe-' + $stamp + '.err')
    Set-Content -LiteralPath $probe -Value "import fastapi, bs4, sqlalchemy, sqlite3`nprint('ok')" -Encoding ASCII
    try {
        $process = Start-Process -FilePath $Path -ArgumentList ('"' + $probe + '"') `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile -NoNewWindow -Wait -PassThru
        if ($process.ExitCode -eq 0) {
            $result.Ok = $true
            return $result
        }
        $text = ''
        if (Test-Path -LiteralPath $errFile) { $text = [System.IO.File]::ReadAllText($errFile, [System.Text.Encoding]::UTF8) }
        $line = ($text -split "`r?`n" | Where-Object { $_ -match 'Error' } | Select-Object -First 1)
        if (-not $line) { $line = ($text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) }
        $tail = ''
        if ($line) { $tail = '：' + $line.Trim() }
        $result.Detail = '退出码 ' + $process.ExitCode + $tail
    }
    catch {
        $result.Detail = $_.Exception.Message
    }
    finally {
        Remove-Item -LiteralPath $probe, $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
    return $result
}

# 解释器：显式参数 > CAMPUS_PYTHON > 已注册任务里记录的那个 > 仓库自带 venv > PATH > 本机安装目录
#
# 这里不是「随便挑一个 python」：本机 PATH 里的 Python311 没有 fastapi，
# 用它注册的任务每天都会静默失败。所以每个候选都要真的 import 成功才采用，
# 被跳过的候选连同原因一起打印出来——不静默吞掉失败。
$PythonSource = 'param'
$Rejected = @()

if (-not $Python) {
    if ($env:CAMPUS_PYTHON) {
        $Python = $env:CAMPUS_PYTHON
        $PythonSource = 'CAMPUS_PYTHON'
    }
    else {
        $candidates = New-Object System.Collections.ArrayList
        # 1) 现有任务记录过的解释器：重复 -Install 不会把解释器换掉
        $registeredTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($registeredTask) {
            foreach ($registeredAction in $registeredTask.Actions) {
                if ($registeredAction.Arguments -and ($registeredAction.Arguments -match '-Python\s+"([^"]+)"')) {
                    [void]$candidates.Add([pscustomobject]@{ Path = $matches[1]; Source = '已注册任务' })
                }
            }
        }
        # 2) 仓库自带 venv（与 start-campus.ps1 找的是同样两个位置）
        foreach ($relative in @('.venv\Scripts\python.exe', '..\..\.venv\Scripts\python.exe')) {
            [void]$candidates.Add([pscustomobject]@{ Path = (Join-Path $RepoRoot $relative); Source = 'venv' })
        }
        # 3) PATH
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { [void]$candidates.Add([pscustomobject]@{ Path = $found.Source; Source = 'PATH' }) }
        # 4) 本机安装目录（版本号大的先试）
        #    循环变量不能叫 $install：PowerShell 变量大小写不敏感，会和脚本自己的
        #    [switch]$Install 撞名，赋值时报「无法把 DirectoryInfo 转成 SwitchParameter」。
        $installRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $installRoot) {
            $installs = Get-ChildItem -LiteralPath $installRoot -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object -Property Name -Descending
            foreach ($installDir in $installs) {
                [void]$candidates.Add([pscustomobject]@{ Path = (Join-Path $installDir.FullName 'python.exe'); Source = ('本机安装 ' + $installDir.Name) })
            }
        }
        foreach ($candidate in $candidates) {
            $resolved = [string]$candidate.Path
            if (-not $resolved) { continue }
            if (-not (Test-Path -LiteralPath $resolved)) {
                $Rejected += ($candidate.Source + '：' + $resolved + '（不存在）')
                continue
            }
            $probe = Test-PythonDependencies $resolved
            if ($probe.Ok) {
                $Python = $resolved
                $PythonSource = $candidate.Source
                break
            }
            $Rejected += ($candidate.Source + '：' + $resolved + '（' + $probe.Detail + '）')
        }
    }
}
if (-not $Python) {
    $tried = ''
    if ($Rejected.Count -gt 0) { $tried = "`n       试过但不可用：`n       " + ($Rejected -join "`n       ") }
    Fail ("找不到装齐依赖（fastapi / bs4 / sqlalchemy）的 Python 解释器。$tried`n       用 -Python ""<python.exe>"" 指定，或设置环境变量 CAMPUS_PYTHON。") $ExitUsage
}
$Python = [System.IO.Path]::GetFullPath($Python)

function Assert-Python([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail ("Python 解释器不存在：$Path`n       路径含中文或空格时也必须完整加引号传入 -Python。") $ExitFailed
    }
    if (-not $Path.ToLower().EndsWith('.exe')) {
        Write-Warn2 "解释器路径不以 .exe 结尾：$Path（继续，但计划任务可能无法直接启动它）"
    }
    # 依赖是真实门槛：本机 PATH 里的 Python311 就没有 fastapi。
    $probe = Test-PythonDependencies $Path
    if (-not $probe.Ok) {
        Fail ("解释器 $Path 缺少运行依赖（fastapi / bs4 / sqlalchemy）或无法启动：`n       " + $probe.Detail + "`n       用装齐依赖的解释器：-Python ""<python.exe>""。") $ExitFailed
    }
    return $true
}

# 每日更新的命令行参数（-RunNow 与任务定义共用，保证「装的是什么、跑的就是什么」）
function Get-UpdateArguments([string]$JsonOut) {
    $parts = @((Quote-Argument $DailyUpdate), '--apply', '--data-dir', (Quote-Argument $DataDir))
    if ($JsonOut) { $parts += @('--json-out', (Quote-Argument $JsonOut)) }
    if (-not $NoGateway) { $parts += '--gateway-from-config' }
    return ($parts -join ' ')
}

function Invoke-DailyUpdate([string]$JsonOut, [string]$LogPath, [string]$TempTag) {
    $arguments = Get-UpdateArguments $JsonOut
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("touxian-daily-" + $TempTag)
    $stdout = $scratch + '.out'
    $stderr = $scratch + '.err'

    # Python 输出到管道时默认用系统代码页；强制 UTF-8，日志才能被 -Status 正确读回。
    $previousUtf8 = $env:PYTHONUTF8
    $previousIo = $env:PYTHONIOENCODING
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $started = Get-Date
    try {
        $process = Start-Process -FilePath $Python -ArgumentList $arguments `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -NoNewWindow -Wait -PassThru
        $code = $process.ExitCode
    }
    finally {
        $env:PYTHONUTF8 = $previousUtf8
        $env:PYTHONIOENCODING = $previousIo
    }
    $finished = Get-Date

    $outText = Read-Utf8 $stdout
    $errText = Read-Utf8 $stderr
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

    if ($LogPath) {
        $logDir = Split-Path -Parent $LogPath
        if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.AppendLine('')
        [void]$builder.AppendLine('=' * 78)
        [void]$builder.AppendLine('开始 ' + $started.ToString('yyyy-MM-dd HH:mm:ss') + '  （' + $Action + '）')
        [void]$builder.AppendLine('解释器 ' + $Python)
        [void]$builder.AppendLine('数据目录 ' + $DataDir)
        [void]$builder.AppendLine('命令 ' + $Python + ' ' + $arguments)
        [void]$builder.AppendLine('=' * 78)
        if ($outText) { [void]$builder.Append($outText); if (-not $outText.EndsWith("`n")) { [void]$builder.AppendLine('') } }
        if ($errText) {
            [void]$builder.AppendLine('--- stderr ---')
            [void]$builder.Append($errText)
            if (-not $errText.EndsWith("`n")) { [void]$builder.AppendLine('') }
        }
        [void]$builder.AppendLine('结束 ' + $finished.ToString('yyyy-MM-dd HH:mm:ss') + '  退出码 ' + $code)
        Add-Utf8 $LogPath $builder.ToString()
    }

    return [pscustomobject]@{
        ExitCode = $code
        StdOut   = $outText
        StdErr   = $errText
        Started  = $started
        Finished = $finished
        LogPath  = $LogPath
        JsonPath = $JsonOut
        Command  = $arguments
    }
}

function Get-LogPaths([string]$Directory) {
    $logDir = Join-Path $Directory 'logs'
    $stamp = (Get-Date).ToString('yyyyMMdd')
    return [pscustomobject]@{
        Dir  = $logDir
        Log  = (Join-Path $logDir ("daily-" + $stamp + '.log'))
        Json = (Join-Path $logDir ("daily-" + $stamp + '.json'))
        Last = (Join-Path $logDir 'last-run.json')
    }
}

# ---------------------------------------------------------------------------
# -RunNow：计划任务实际执行的动作
# ---------------------------------------------------------------------------

function Invoke-RunNow {
    Assert-Python $Python | Out-Null
    $paths = Get-LogPaths $DataDir
    if (-not (Test-Path -LiteralPath $paths.Dir)) { New-Item -ItemType Directory -Force -Path $paths.Dir | Out-Null }

    $result = Invoke-DailyUpdate -JsonOut $paths.Json -LogPath $paths.Log -TempTag ('run-' + [guid]::NewGuid().ToString('N').Substring(0, 8))

    $record = [ordered]@{
        started_at  = $result.Started.ToString('o')
        finished_at = $result.Finished.ToString('o')
        duration_s  = [int]($result.Finished - $result.Started).TotalSeconds
        exit_code   = $result.ExitCode
        log         = $paths.Log
        json        = $paths.Json
        data_dir    = $DataDir
        python      = $Python
        task        = $TaskName
        command     = $result.Command
    }
    Write-Utf8 $paths.Last (($record | ConvertTo-Json -Depth 4) + "`n")

    if ($result.StdOut) { Write-Host $result.StdOut.TrimEnd() }
    if ($result.StdErr) { Write-Host $result.StdErr.TrimEnd() -ForegroundColor Yellow }
    Write-Note ('日志 ' + $paths.Log)
    exit $result.ExitCode
}

# ---------------------------------------------------------------------------
# -Install
# ---------------------------------------------------------------------------

function Get-TaskActionLine {
    $target = Quote-Argument $ScriptPath
    $python = Quote-Argument $Python
    $data = Quote-Argument $DataDir
    $extra = ''
    if ($NoGateway) { $extra = ' -NoGateway' }
    return ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + $target +
        ' -RunNow -Python ' + $python + ' -DataDir ' + $data + ' -TaskName ' + (Quote-Argument $TaskName) + $extra)
}

function Invoke-Install {
    Write-Step '每日定时任务安装'

    # 1) 先自检，再注册：装一个跑不起来的任务是更糟的结果。
    Assert-Python $Python | Out-Null
    Write-Ok ('解释器     ' + $Python + '（来源：' + $PythonSource + '）')
    foreach ($skip in $Rejected) { Write-Note ('已跳过     ' + $skip) }
    Write-Ok ('数据目录   ' + $DataDir)

    if (-not (Test-Elevated)) {
        Fail ("注册「不管用户是否登录都运行」的任务需要管理员权限，当前不是提升权限的会话。`n       请右键「以管理员身份运行」PowerShell 后重试。") $ExitPermission
    }

    $triggerAt = [datetime]::MinValue
    if (-not [datetime]::TryParseExact($Time, 'HH:mm', [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None, [ref]$triggerAt)) {
        Fail ("-Time 需要 24 小时制 HH:mm，收到：$Time（例：-Time 08:15）") $ExitUsage
    }
    $startBoundary = (Get-Date).Date.AddHours($triggerAt.Hour).AddMinutes($triggerAt.Minute)

    # 2) 用真实命令行做一次演练：证明「中文 + 空格」的引号确实能跑通。
    Write-Step '引号与命令行自检（演练，不写数据）'
    $paths = Get-LogPaths $DataDir
    $smokeArgs = (Quote-Argument $DailyUpdate) + ' --dry-run --data-dir ' + (Quote-Argument $DataDir)
    if (-not $NoGateway) { $smokeArgs += ' --gateway-from-config' }
    $smokeOut = Join-Path ([System.IO.Path]::GetTempPath()) ('touxian-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.out')
    $smokeErr = Join-Path ([System.IO.Path]::GetTempPath()) ('touxian-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.err')
    $previousUtf8 = $env:PYTHONUTF8
    $previousIo = $env:PYTHONIOENCODING
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $smoke = Start-Process -FilePath $Python -ArgumentList $smokeArgs -RedirectStandardOutput $smokeOut -RedirectStandardError $smokeErr -NoNewWindow -Wait -PassThru
    }
    finally {
        $env:PYTHONUTF8 = $previousUtf8
        $env:PYTHONIOENCODING = $previousIo
    }
    $smokeText = Read-Utf8 $smokeOut
    $smokeErrorText = Read-Utf8 $smokeErr
    Remove-Item -LiteralPath $smokeOut, $smokeErr -Force -ErrorAction SilentlyContinue
    if ($smoke.ExitCode -ne 0) {
        Write-Problem ('演练失败（退出码 ' + $smoke.ExitCode + '），任务未注册。')
        if ($smokeErrorText) { Write-Problem $smokeErrorText.TrimEnd() }
        if ($smokeText) { Write-Note $smokeText.TrimEnd() }
        Write-Note '常见原因：解释器缺依赖、数据目录被拒绝（data/ 是样本目录）、或路径引号被 shell 吃掉。'
        exit $ExitFailed
    }
    $firstLine = ($smokeText -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    Write-Ok ('演练通过   ' + $firstLine)

    # 3) 注册（幂等：已存在则整体更新，不报「任务已存在」）
    Write-Step ('注册计划任务 ' + $TaskName)
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (Get-TaskActionLine)
    $trigger = New-ScheduledTaskTrigger -Daily -At $startBoundary
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable:$false `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest

    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Description '偷闲·天大版 每日更新：采集 → 复核 → 下架（scripts/daily_update.py --apply）' `
            -Force -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Problem ('注册失败：' + $_.Exception.Message)
        Write-Note '诊断线索：'
        Write-Note '  · 权限不足 → 用「以管理员身份运行」的 PowerShell；本脚本已确认当前会话是否提升。'
        Write-Note '  · 路径含空格/中文 → 任务参数里的路径必须带引号；用 -Status 可看到实际注册的命令行。'
        Write-Note '  · 解释器不存在 → 用 -Python "<python.exe>" 指定装齐依赖的那个。'
        Write-Note '  · 组策略禁止登录时运行 → 改用 -Install 前先让管理员放行，或退回「仅登录时运行」。'
        exit $ExitFailed
    }

    if ($existing) {
        Write-Ok ('已更新     （任务此前已存在，本次按 ' + $startBoundary.ToString('HH:mm') + ' 覆盖）')
    }
    else {
        Write-Ok '已新建'
    }
    Write-Ok ('触发时间   每天 ' + $startBoundary.ToString('HH:mm') + '（错过不补跑：StartWhenAvailable=False）')
    Write-Ok ('运行身份   ' + $env:USERDOMAIN + '\' + $env:USERNAME + '（S4U：不依赖登录会话，重启后仍生效）')
    Write-Ok ('执行命令   powershell.exe ' + (Get-TaskActionLine))
    Write-Ok ('日志       ' + $paths.Dir + '\daily-YYYYMMDD.log')

    Write-Step '核对注册结果'
    $registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $registered) { Fail '注册后仍查不到任务，注册没有真正生效。' $ExitFailed }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Ok ('状态       ' + $registered.State)
    Write-Ok ('下次运行   ' + $info.NextRunTime)
    Write-Note '现在就验证一次？  powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status'
    Write-Note ('立刻跑一次？      schtasks /Run /TN ' + $TaskName)
    exit $ExitOk
}

# ---------------------------------------------------------------------------
# -Uninstall
# ---------------------------------------------------------------------------

function Invoke-Uninstall {
    Write-Step ('卸载计划任务 ' + $TaskName)
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Ok '任务本来就不存在，无需卸载（幂等）。'
        exit $ExitOk
    }
    if (-not (Test-Elevated)) {
        Fail '删除计划任务需要管理员权限；请用「以管理员身份运行」的 PowerShell 重试。' $ExitPermission
    }
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    catch {
        Fail ('删除失败：' + $_.Exception.Message) $ExitFailed
    }
    Write-Ok '已删除任务。'
    $paths = Get-LogPaths $DataDir
    Write-Note ('日志保留在 ' + $paths.Dir + '（想彻底清掉就手动删这个目录）。')
    Write-Note '数据目录与已发布内容不受影响：这个任务只负责触发每日更新。'
    exit $ExitOk
}

# ---------------------------------------------------------------------------
# -Status
# ---------------------------------------------------------------------------

function Format-Exit([int]$Code) {
    switch ($Code) {
        0 { return '0（成功）' }
        1 { return '1（有失败但未阻断；详见日志）' }
        2 { return '2（用法错误，未做任何改动）' }
        default { return ([string]$Code) }
    }
}

function Show-LastRun([string]$Directory) {
    $paths = Get-LogPaths $Directory
    if (-not (Test-Path -LiteralPath $paths.Last)) {
        Write-Note '最近一次执行   还没有执行记录（任务装好但没跑过，或从未 -RunNow）。'
        Write-Note ('日志目录       ' + $paths.Dir)
        return
    }
    $record = Read-Utf8 $paths.Last | ConvertFrom-Json
    $started = [datetime]::Parse($record.started_at).ToLocalTime()
    $finished = [datetime]::Parse($record.finished_at).ToLocalTime()
    Write-Ok ('最近一次       ' + $started.ToString('yyyy-MM-dd HH:mm:ss') + ' → ' + $finished.ToString('HH:mm:ss') +
        '（用时 ' + $record.duration_s + ' 秒）')
    Write-Ok ('退出码         ' + (Format-Exit ([int]$record.exit_code)))

    # 三段统计优先读机器可读摘要；缺失时退回解析文本日志。
    # 摘要必须确实属于本次执行：锁冲突的进程在写摘要前就退出了，当天的
    # daily-YYYYMMDD.json 还是上一次留下的。直接读它会把上一轮的数字冒充成
    # 本轮结果（实测踩过），所以先核对摘要里的 started_at。
    $summary = $null
    $summaryStale = $false
    if ($record.json -and (Test-Path -LiteralPath $record.json)) {
        try { $summary = Read-Utf8 $record.json | ConvertFrom-Json } catch { $summary = $null }
        if ($summary -and $summary.started_at) {
            try {
                $summaryStarted = [datetime]::Parse($summary.started_at).ToLocalTime()
                if ([math]::Abs(($summaryStarted - $started).TotalSeconds) -gt 120) {
                    $summaryStale = $true
                    $summary = $null
                }
            }
            catch { $summary = $null }
        }
    }
    if ($summaryStale) {
        Write-Warn2 ('摘要过期       本轮没有写出机器可读摘要（' + $record.json + ' 是更早一次留下的），下面改用文本日志。')
    }
    if ($summary -and $summary.stages) {
        $collect = $summary.stages.collect
        $review = $summary.stages.review
        $withdraw = $summary.stages.withdraw
        $totals = $collect.totals
        if ($totals) {
            Write-Ok ('[1/3] 采集     ' + $totals.sources + ' 个来源：成功 ' + $totals.ok + ' · 降级 ' + $totals.degraded +
                ' · 失败 ' + $totals.failed + ' · 新增快照 ' + $totals.new_snapshots + ' · 新候选 ' + $totals.candidates_new)
        }
        else {
            Write-Ok ('[1/3] 采集     来源注册 新增 ' + $collect.registration.new + ' · 复用 ' + $collect.registration.existing)
        }
        Write-Ok ('[2/3] 复核     待复核 ' + $review.pending + ' → 接受 ' + $review.accept + ' · 拒绝 ' + $review.reject +
            ' · 留给人 ' + $review.review + ' · 发布 ' + $review.published)
        Write-Ok ('[3/3] 下架     过期 ' + @($withdraw.expired).Count + ' 条 · 非通知 ' + @($withdraw.unusable).Count + ' 条')
        if (@($summary.errors).Count -gt 0) {
            $codes = @($summary.errors | ForEach-Object { '[' + $_.stage + '] ' + $_.code })
            Write-Warn2 ('本段错误       ' + ($codes -join ', '))
        }
    }
    else {
        $text = Read-Utf8 $record.log
        # 日志是追加的，一天可能有很多段。只解析「本次」那一段，否则上一轮的
        # 三段统计会被当成这一轮的结果。
        $stamp = $started.ToString('yyyy-MM-dd HH:mm:ss')
        $sections = [regex]::Split($text, '(?m)^开始 ')
        $section = $sections | Where-Object { $_.TrimStart().StartsWith($stamp) } | Select-Object -First 1
        if (-not $section) {
            # 时间戳可能被日志开头的换行挤开，退一步取最后一段。
            $section = $sections[-1]
        }
        $sectionText = '开始 ' + $section
        $collectLine = ($sectionText -split "`r?`n" | Where-Object { $_ -match '来源\s+\d+ 个：' } | Select-Object -Last 1)
        $reviewLine = ($sectionText -split "`r?`n" | Where-Object { $_ -match '待复核' } | Select-Object -Last 1)
        $withdrawLine = ($sectionText -split "`r?`n" | Where-Object { $_ -match '过期事项' } | Select-Object -Last 1)
        $errorLine = ($sectionText -split "`r?`n" | Where-Object { $_ -match '另一个每日更新正在运行|错误|失败：' } | Select-Object -First 1)
        if ($collectLine) { Write-Ok ('[1/3] 采集     ' + $collectLine.Trim()) }
        if ($reviewLine) { Write-Ok ('[2/3] 复核     ' + $reviewLine.Trim()) }
        if ($withdrawLine) { Write-Ok ('[3/3] 下架     ' + $withdrawLine.Trim()) }
        if ($errorLine) { Write-Warn2 ('本次失败原因   ' + $errorLine.Trim()) }
        if (-not $collectLine -and -not $reviewLine -and -not $errorLine) {
            Write-Warn2 '摘要缺失：机器可读摘要与文本日志都读不到三段统计。'
        }
    }
    Write-Ok ('日志           ' + $record.log)
}

function Invoke-Status {
    Write-Step ('每日定时任务状态 ' + $TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Warn2 '任务未安装。'
        Write-Note ('安装：powershell -ExecutionPolicy Bypass -File ' + (Quote-Argument $ScriptPath) + ' -Install')
        Show-LastRun $DataDir
        exit $ExitFailed
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Ok ('状态           ' + $task.State)
    Write-Ok ('下次运行       ' + $info.NextRunTime)
    Write-Ok ('上次运行       ' + $info.LastRunTime + '（结果码 ' + $info.LastTaskResult + '）')
    $trigger = $task.Triggers | Select-Object -First 1
    if ($trigger -and $trigger.StartBoundary) {
        $at = [datetime]::Parse($trigger.StartBoundary)
        Write-Ok ('触发时间       每天 ' + $at.ToString('HH:mm') + '（错过不补跑）')
    }
    $principal = $task.Principal
    Write-Ok ('运行身份       ' + $principal.UserId + '（' + $principal.LogonType + '）')

    # 注册时写死的路径可能因仓库搬家而失效：这里直接指出来，别等任务静默失败。
    $actionText = ($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join '; '
    Write-Ok ('执行命令       ' + $actionText)
    $staleScript = $false
    $stalePython = $false
    if ($actionText -match '-File\s+"([^"]+)"') {
        $referenced = $matches[1]
        if (-not (Test-Path -LiteralPath $referenced)) { $staleScript = $true }
    }
    if ($actionText -match '-Python\s+"([^"]+)"') {
        $referencedPython = $matches[1]
        if (-not (Test-Path -LiteralPath $referencedPython)) { $stalePython = $true }
    }
    if ($staleScript) {
        Write-Warn2 '任务指向的安装脚本已不存在（仓库搬家或改名？）。重新 -Install 即可修好。'
    }
    if ($stalePython) {
        Write-Warn2 '任务指向的 Python 已不存在（解释器被卸载或路径变了？）。重新 -Install -Python "<python.exe>" 即可修好。'
    }

    Show-LastRun $DataDir

    if ($task.State -ne 'Ready' -and $task.State -ne 'Running') {
        Write-Warn2 ('任务当前不可用（' + $task.State + '）；临时停用/启用用：schtasks /Change /TN ' + $TaskName + ' /ENABLE')
    }
    exit $ExitOk
}

# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

switch ($Action) {
    'RunNow' { Invoke-RunNow }
    'Install' { Invoke-Install }
    'Uninstall' { Invoke-Uninstall }
    'Status' { Invoke-Status }
}
