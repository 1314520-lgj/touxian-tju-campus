# Mobile acceptance probe: inject verification/mobile/measure.js into the real
# browser through `bsk evaluate` and archive the result.
#
#   powershell -ExecutionPolicy Bypass -File verification/mobile/measure.ps1 -Session pdex -Tag today-390
#
# Prints one summary line and appends the full JSON to verification/mobile/measurements.jsonl.
# ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less file with the ANSI
# code page, so non-ASCII comments would corrupt the parse.
param(
  [Parameter(Mandatory = $true)][string]$Session,
  [Parameter(Mandatory = $true)][string]$Tag
)
$ErrorActionPreference = 'Stop'
$env:BSK_AUTO_START = '0'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# Read as UTF-8 explicitly: PS 5.1 would otherwise decode a BOM-less file with
# the ANSI code page and hand mojibake to the browser.
$js = [System.IO.File]::ReadAllText((Join-Path $here 'measure.js'), [System.Text.Encoding]::UTF8)
$raw = bsk evaluate --session $Session --json $js
if ($LASTEXITCODE -ne 0) { throw "bsk evaluate failed: $raw" }
$outer = $raw | ConvertFrom-Json
if (-not $outer.ok) { throw "bsk evaluate returned not-ok: $raw" }
$value = $outer.value | ConvertFrom-Json
$value | Add-Member -NotePropertyName tag -NotePropertyValue $Tag -Force
($value | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath (Join-Path $here 'measurements.jsonl') -Encoding utf8
Write-Host ("[{0}] {1}x{2} scrollW={3} hScroll={4} overflow={5} smallTargets={6} cardsAboveHalf={7} firstCardTop={8} deadlineOnFirstScreen={9} navIndex={10}" -f `
  $Tag, $value.iw, $value.ih, $value.sw, $value.hScroll, $value.overflowCount, $value.smallCount, $value.cardsAboveHalf, $value.firstCardTop, $value.firstDeadlineOnFirstScreen, $value.navIndex)
if ($value.overflowCount -gt 0) { $value.overflow | ForEach-Object { Write-Host ("    overflow {0} [{1}..{2}] w={3} :: {4}" -f $_.sel, $_.left, $_.right, $_.w, $_.text) } }
if ($value.smallCount -gt 0) { $value.small | ForEach-Object { Write-Host ("    small {0}.{1} [{2}] {3}x{4} :: {5}" -f $_.tag, $_.cls, $_.target, $_.w, $_.h, $_.text) } }
exit 0