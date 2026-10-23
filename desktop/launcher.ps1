# 偷闲·天大版 启动器
# 1) 起本机公众号 feed 服务（weread-omni 导出 JSON Feed，端口 4011）
# 2) 起偷闲本体（自动把公众号 feed 接上）
# 3) 偷闲退出后把 feed 服务一起停掉

$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$feedDir = Join-Path $here "weread-feed"
$app = Join-Path $here "TouxianCampus"
$node = Join-Path $feedDir "runtime\node\node.exe"
$feedUrl = "http://127.0.0.1:4011/feeds/all.json"

function Wait-Feed {
    param([int]$TimeoutSeconds = 40)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $feedUrl -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 600
    }
    return $false
}

# 让偷闲的配置指向本机 feed（只在没有配置时写入，保留用户其它设置）
function Ensure-FeedSetting {
    $settingsPath = Join-Path $app "touxian-settings.json"
    try {
        if (Test-Path $settingsPath) {
            $settings = (Get-Content $settingsPath -Raw -Encoding UTF8) | ConvertFrom-Json
        } else {
            $settings = [pscustomobject]@{
                port = 8765
                admin_email = "operator@touxian.local"
                wechat_feed_enabled = $true
                wechat_feed_auto = $true
                wechat_feeds = @()
                trusted_origins = @()
            }
        }
        if (-not $settings.wechat_feeds -or @($settings.wechat_feeds).Count -eq 0) {
            $settings | Add-Member -NotePropertyName wechat_feeds -NotePropertyValue @(
                [pscustomobject]@{ name = "天津大学公众号（微信读书）"; url = $feedUrl }
            ) -Force
        }
        $settings | Add-Member -NotePropertyName wechat_feed_enabled -NotePropertyValue $true -Force
        ($settings | ConvertTo-Json -Depth 6) | Set-Content $settingsPath -Encoding UTF8
        # 去掉可能的 UTF-8 BOM（产品的 json.loads 不吃 BOM）
        $text = [System.IO.File]::ReadAllText($settingsPath).TrimStart([char]0xFEFF)
        [System.IO.File]::WriteAllText($settingsPath, $text, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-Host "      设置文件写入失败（不影响启动）：$($_.Exception.Message)" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "偷闲·天大版" -ForegroundColor Cyan
Write-Host "----------------------------------------"

$feedProcess = $null
$serverScript = Join-Path $feedDir "server.mjs"
if ((Test-Path $node) -and (Test-Path $serverScript)) {
    Write-Host "[1/2] 启动本机公众号 feed 服务 ..." -ForegroundColor Yellow
    $feedProcess = Start-Process -FilePath $node -ArgumentList @($serverScript) `
        -WorkingDirectory $feedDir -WindowStyle Hidden -PassThru
    if (Wait-Feed 40) {
        Write-Host "      feed 已就绪：$feedUrl" -ForegroundColor Green
    } else {
        Write-Host "      feed 服务没起来；本次只采官网。" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "[1/2] 未安装公众号组件；本次只采官网。" -ForegroundColor DarkYellow
}

Ensure-FeedSetting

Write-Host "[2/2] 启动偷闲 ..." -ForegroundColor Yellow
Push-Location $app
try {
    & (Join-Path $app "TouxianCampus.exe")
} finally {
    Pop-Location
    if ($feedProcess -and -not $feedProcess.HasExited) {
        Write-Host "正在停止公众号 feed 服务 ..." -ForegroundColor DarkGray
        Stop-Process -Id $feedProcess.Id -Force
    }
}
