# Reproduces the first-screen measurements for a running campus instance.
# ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less file as ANSI,
# so any non-ASCII text in this script would be mangled before it runs.
#
#   powershell -ExecutionPolicy Bypass -File verification/firstscreen/measure.ps1 `
#       -Session <bsk session id> -Port 8432 -Width 360 -Height 640 -Mobile -Label coldstart-360x640
#
# Appends one JSON line to verification/firstscreen/measurements.jsonl and saves a screenshot.
param(
    [string]$Session = 'sxcx',
    [int]$Port = 8432,
    [int]$Width = 1280,
    [int]$Height = 900,
    [switch]$Mobile,
    [string]$Label = 'desktop',
    [string]$Out = 'verification/firstscreen'
)
$ErrorActionPreference = 'Stop'
$env:BSK_AUTO_START = '0'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$probe = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'probe.js'), [System.Text.UTF8Encoding]::new($false))

if ($Mobile) {
    & bsk emulate --session $Session --width $Width --height $Height --mobile --dpr 3 --touch --max-touch-points 5 | Out-Null
} else {
    & bsk emulate --session $Session --width $Width --height $Height | Out-Null
}
Start-Sleep -Seconds 2
& bsk navigate --session $Session ("http://127.0.0.1:" + $Port + "/campus.html") | Out-Null
Start-Sleep -Seconds 4
$value = (& bsk evaluate --session $Session --json $probe | ConvertFrom-Json).value
$line = '{"label":"' + $Label + '","port":' + $Port + ',"width":' + $Width + ',"height":' + $Height + ',"probe":' + $value + '}'
$jsonl = Join-Path $root ($Out + '/measurements.jsonl')
[System.IO.File]::AppendAllText($jsonl, $line + "`n", [System.Text.UTF8Encoding]::new($false))
& bsk screenshot --session $Session --out (Join-Path $root ($Out + '/' + $Label + '.png')) | Out-Null
$value