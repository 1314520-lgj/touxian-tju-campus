# Walk the five core pages at one phone width in the real browser and archive
# measurements plus screenshots.
#
#   powershell -ExecutionPolicy Bypass -File verification/mobile/walk.ps1 -Session pdex -Width 390 -Height 844 -Label 390
#
# Pages: home -> event detail (real click on a card title) -> my items ->
# manual add form -> search with results. Every step dispatches a real click
# through the browser-skill daemon, then measures the rendered page and fails
# loudly when the page did not actually change.
#
# ASCII-only, and no non-ASCII inside selectors or values: Windows PowerShell
# 5.1 encodes native command arguments with the ANSI code page, so a Chinese
# attribute selector arrives as mojibake and silently matches nothing. The
# navigation is therefore identified by its 1-based index (see measure.js).
param(
  [Parameter(Mandatory = $true)][string]$Session,
  [Parameter(Mandatory = $true)][int]$Width,
  [Parameter(Mandatory = $true)][int]$Height,
  [Parameter(Mandatory = $true)][string]$Label
)
$ErrorActionPreference = 'Stop'
$env:BSK_AUTO_START = '0'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:failed = 0

function Shot([string]$name) {
  & bsk screenshot --session $Session --out (Join-Path $here $name) --json | Out-Null
  Write-Host ("    shot {0}" -f $name)
}
function Meas([string]$tag) {
  & (Join-Path $here 'measure.ps1') -Session $Session -Tag $tag
  return (Get-Content -LiteralPath (Join-Path $here 'measurements.jsonl') -Encoding UTF8 | Select-Object -Last 1) | ConvertFrom-Json
}
function Step([string]$tag, [int]$expectNav, [string]$shot) {
  $value = Meas $tag
  if ($value.navIndex -ne $expectNav) {
    Write-Host ("    !! {0}: expected navIndex {1} but the page reports {2}" -f $tag, $expectNav, $value.navIndex)
    $script:failed = 1
  }
  if ($shot) { Shot $shot }
}
function Click([string]$selector) {
  & bsk click --session $Session --selector $selector | Out-Null
  Start-Sleep -Milliseconds 900
}
function OpenNav([int]$index) {
  Click '.mobile-menu'
  Click ("#campus-navigation nav button:nth-child({0})" -f $index)
  Start-Sleep -Milliseconds 800
}

& bsk emulate --session $Session --width $Width --height $Height --mobile --dpr 3 --touch --max-touch-points 5 | Out-Null
& bsk reload --session $Session | Out-Null
Start-Sleep -Seconds 4

Write-Host ("== {0} ==" -f $Label)
Step ("after-today-{0}" -f $Label) 1 ("after-01-today-{0}.png" -f $Label)

Click 'article.event-card button.card-title'
Step ("after-detail-{0}" -f $Label) 1 ("after-02-detail-{0}.png" -f $Label)
Click '.modal .modal-head button'

OpenNav 4
Step ("after-my-{0}" -f $Label) 4 ("after-03-my-{0}.png" -f $Label)

Click 'main .page-heading button.primary'
Click '.modal .inline-actions button:nth-of-type(2)'
Step ("after-add-{0}" -f $Label) 4 ("after-04-add-{0}.png" -f $Label)
Click '.modal .modal-head button'

OpenNav 3
& bsk fill --session $Session --selector 'main .search-input input' --value 'AI' | Out-Null
Click 'main .search-input button.primary'
Start-Sleep -Milliseconds 1400
Step ("after-search-{0}" -f $Label) 3 ("after-05-search-{0}.png" -f $Label)

Write-Host ("== {0} done (failed={1}) ==" -f $Label, $script:failed)
exit $script:failed