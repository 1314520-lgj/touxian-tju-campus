param([switch]$Demo,[int]$Port=8000,[switch]$Setup,[switch]$Collect)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8='1'
if (Test-Path -LiteralPath '.env') {
    foreach ($line in Get-Content -LiteralPath '.env') {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1],$matches[2].Trim().Trim('"').Trim("'"),'Process') }
    }
}
$pythonCandidates=@((Join-Path $PSScriptRoot '.venv/Scripts/python.exe'),(Join-Path $PSScriptRoot '../../.venv/Scripts/python.exe'))
$python=$pythonCandidates | Where-Object {Test-Path -LiteralPath $_} | Select-Object -First 1
if (-not $python) {
    $systemPython=Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $systemPython) { throw '请安装 Python 3.12 或更新版本，并加入 PATH。' }
    & $systemPython.Source -m venv '.venv'
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
    $python=Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    $Setup=$true
}
if ($Setup) {
    # --no-cache-dir is not an optimisation: a corrupted pip cache directory made the
    # first install hang for 20 minutes on a plain machine, while a cache-free install
    # of the same wheels finished in about three minutes. CAMPUS_PIP_INDEX_URL lets an
    # operator point at a mirror (for example https://pypi.tuna.tsinghua.edu.cn/simple);
    # when the default index fails we retry once through that mirror instead of leaving
    # the student with a silent stall.
    $pipMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
    $pipIndex = $env:CAMPUS_PIP_INDEX_URL
    $pipArgs = @('-m','pip','install','--no-cache-dir','-r','requirements-campus.lock.txt')
    if ($pipIndex) { $pipArgs += @('-i',$pipIndex) }
    & $python @pipArgs
    if ($LASTEXITCODE -ne 0 -and $pipIndex -ne $pipMirror) {
        Write-Host ('依赖安装失败，改用镜像重试：' + $pipMirror)
        & $python -m pip install --no-cache-dir -i $pipMirror -r 'requirements-campus.lock.txt'
    }
    if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败。' }
    Write-Host '依赖安装完成。'
}
if (-not (Test-Path -LiteralPath 'dist/campus.html')) {
    $node=Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { throw '请安装 Node.js 24，再重新运行。' }
    & npx.cmd --yes pnpm@10.30.0 install --frozen-lockfile --ignore-scripts
    if ($LASTEXITCODE -ne 0) { throw '安装网页依赖失败。' }
    & npx.cmd --yes pnpm@10.30.0 build
    if ($LASTEXITCODE -ne 0) { throw '网页构建失败。' }
}
if (-not $env:CAMPUS_ALLOWED_ORIGINS) { $env:CAMPUS_ALLOWED_ORIGINS="http://127.0.0.1:$Port,http://localhost:$Port" }
if ($Demo) {    # An inherited production .env must never route synthetic demo data externally.
    $env:CAMPUS_DATABASE_URL=''
    $env:CAMPUS_MEILI_URL=''
    $env:CAMPUS_MEILI_KEY=''
    $env:CAMPUS_PUSH_USER_ENDPOINTS=''
    $env:CAMPUS_PUSH_ENDPOINTS=''
    $env:CAMPUS_BLOB_STORE='local'
    $env:CAMPUS_GCS_BUCKET=''
    $env:CAMPUS_PUBLIC_ORIGIN=''
    $env:CAMPUS_SECRET=''
    $env:CAMPUS_ALLOWED_ORIGINS="http://127.0.0.1:$Port,http://localhost:$Port"
    $env:CAMPUS_ENV='development'
    $env:CAMPUS_DATA_DIR=Join-Path $PSScriptRoot '.campus-demo'
    $env:CAMPUS_DEV_AUTH='true'
    $env:CAMPUS_ADMIN_EMAILS='demo-admin@tju.edu.cn'
    & $python -m server.campus_app demo
    if ($LASTEXITCODE -ne 0) { throw '演示初始化失败。' }
    Write-Host '合成演示已就绪；管理员 demo-admin@tju.edu.cn。本机登录页面会显示验证码。'
} else {
    # 单机自用档：本机浏览器打开就能登录。
    # 没有邮件服务时，平台要么发不出登录码、要么直接 503——普通学生就卡在登录页。
    # 这里补齐单用户本机默认值（服务只监听 127.0.0.1，不会对外暴露）；
    # 已经在 .env 或环境变量里显式设置过的值一律不覆盖。
    if (-not $env:CAMPUS_ENV) { $env:CAMPUS_ENV='development' }
    if (-not $env:CAMPUS_DEV_AUTH) { $env:CAMPUS_DEV_AUTH='true' }
    if (-not $env:CAMPUS_DATA_DIR) { $env:CAMPUS_DATA_DIR=Join-Path $PSScriptRoot 'data-operator' }
    if (-not $env:CAMPUS_ADMIN_EMAILS) { $env:CAMPUS_ADMIN_EMAILS='operator@tju.edu.cn' }
    if ($env:CAMPUS_ENV -match '^(production|prod)$') { throw 'CAMPUS_ENV=production 时不能用本脚本的单机默认档，请改用 app.py。' }
    & $python -m server.campus_app init-sources
    if ($LASTEXITCODE -ne 0) { throw '来源注册失败。' }
    if ($Collect) {
        & $python scripts/campus_collect.py collect --data-dir $env:CAMPUS_DATA_DIR --gateway-from-config
        Write-Host '采集结束（部分来源需授权，属正常）。'
    }
    Write-Host ''
    Write-Host '登录邮箱（页面会直接显示验证码，不需要邮箱服务）：' -ForegroundColor Cyan
    Write-Host ('  ' + $env:CAMPUS_ADMIN_EMAILS) -ForegroundColor Cyan
    Write-Host ('数据目录：' + $env:CAMPUS_DATA_DIR)
    Write-Host '如果还没采集过，先在本窗口按 Ctrl+C，再用 -Collect 重新启动一次。'
}
Write-Host "打开 http://127.0.0.1:$Port ；按 Ctrl+C 停止。"
& $python -m server.campus_app serve --port $Port
