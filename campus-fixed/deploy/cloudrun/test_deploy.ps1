# Local argument-flow checks only. gcloud is a function stub: no cloud access.
$ErrorActionPreference = 'Stop'
$taskCalls = [System.Collections.Generic.List[object]]::new()
$taskSchedulerExists = $true
$taskBucketSafe = $true
$taskFailBuild = $false
$taskFailScheduleLookup = $false
function gcloud {
    $taskArgs = @($args)
    $taskCalls.Add($taskArgs)
    $global:LASTEXITCODE = 0
    if ($taskArgs[0] -eq 'storage') {
        if ($taskBucketSafe) { return '{"iamConfiguration":{"publicAccessPrevention":"enforced","uniformBucketLevelAccess":{"enabled":true}}}' }
        return '{"iamConfiguration":{"publicAccessPrevention":"inherited","uniformBucketLevelAccess":{"enabled":false}}}'
    }
    if (($taskArgs[0..2] -join ' ') -eq 'run services describe') { return 'https://synthetic-123.asia-east2.run.app' }
    if (($taskArgs[0..2] -join ' ') -eq 'scheduler jobs list') {
        if ($taskArgs -notcontains '--format=value(name)') { throw 'Scheduler format argument split' }
        if ($taskFailScheduleLookup) { $global:LASTEXITCODE = 1; return }
        if (-not $taskSchedulerExists) { return }
        $taskFilter = @($taskArgs | Where-Object { $_ -like '--filter=name=*' })[0]
        if (-not $taskFilter) { throw 'Scheduler exact-name filter missing' }
        return $taskFilter.Substring('--filter=name='.Length)
    }
    if ($taskArgs[0] -eq 'builds' -and $taskFailBuild) { $global:LASTEXITCODE = 1 }
}
function Assert-Check([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Commands { return @($taskCalls | ForEach-Object { $_[0..2] -join ' ' }) }
$taskAstErrors = $null; $taskAstTokens = $null
[System.Management.Automation.Language.Parser]::ParseFile("$PSScriptRoot/deploy.ps1", [ref]$taskAstTokens, [ref]$taskAstErrors) | Out-Null
Assert-Check ($taskAstErrors.Count -eq 0) 'PowerShell parse errors'
. "$PSScriptRoot/parameters.example.ps1"
& "$PSScriptRoot/deploy.ps1" @CampusDeploy -EnableScheduler -OpenToStudents
Assert-Check ($taskCalls.Count -eq 0) 'Preview executed a cloud command'
$CampusDeploy.FeedSecrets = @{ CAMPUS_SOURCE_F6A3B540865049DD9DB12DDAA786C641_TOKEN = 'synthetic-feed-secret:3' }
$taskInvalid = $CampusDeploy.Clone(); $taskInvalid.FeedSecrets = @{ CAMPUS_SECRET = 'must-not-override:1' }
$taskRejected = $false
try { & "$PSScriptRoot/deploy.ps1" @taskInvalid -Apply } catch { $taskRejected = $true }
Assert-Check ($taskRejected -and $taskCalls.Count -eq 0) 'Invalid feed references mutated deployment before validation'
& "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply -EnableScheduler -OpenToStudents
$taskCommands = Commands
Assert-Check ($taskCommands -contains 'scheduler jobs pause') 'Existing scheduler not paused'
$taskBuildCommand = @($taskCommands | Where-Object { $_ -like 'builds submit *' })[0]
Assert-Check ([array]::IndexOf($taskCommands, 'scheduler jobs pause') -lt [array]::IndexOf($taskCommands, $taskBuildCommand)) 'Pause must precede build'
Assert-Check ($taskCommands -contains 'scheduler jobs update') 'Existing scheduler not updated'
Assert-Check ($taskCommands -contains 'scheduler jobs resume') 'Explicit scheduling did not resume'
$taskUpdate = @($taskCalls | Where-Object { ($_[0..2] -join ' ') -eq 'run services update' })[0]
Assert-Check ([bool]($taskUpdate | Where-Object { $_ -like '*CAMPUS_PUBLIC_ORIGIN=https://synthetic-123.asia-east2.run.app*' })) 'Canonical service URL not propagated'
$taskFeedCopies = @($taskCalls | Where-Object { ($_ -join ' ') -like '*--set-secrets=*CAMPUS_SOURCE_F6A3B540865049DD9DB12DDAA786C641_TOKEN=synthetic-feed-secret:3*' })
Assert-Check ($taskFeedCopies.Count -eq 3) 'Feed reference missing from service or either job'
Assert-Check ($taskCommands -contains 'run services add-iam-policy-binding') 'Explicit student opening missing'
$taskCalls.Clear()
& "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply
$taskCommands = Commands
Assert-Check ($taskCommands -contains 'scheduler jobs pause') 'Private deployment failed to pause schedule'
Assert-Check ($taskCommands -notcontains 'scheduler jobs resume') 'Private deployment silently resumed schedule'
Assert-Check ($taskCommands -notcontains 'run services add-iam-policy-binding') 'Private deployment silently opened student access'
$taskCalls.Clear(); $taskSchedulerExists = $false
& "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply -EnableScheduler
$taskCommands = Commands
Assert-Check ($taskCommands -contains 'scheduler jobs create') 'Missing schedule should be created'
Assert-Check ($taskCommands -notcontains 'scheduler jobs resume') 'New schedule should not require resume'
$taskCalls.Clear(); $taskSchedulerExists = $true; $taskFailBuild = $true; $taskRejected = $false
try { & "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply -EnableScheduler } catch { $taskRejected = $true }
$taskCommands = Commands
Assert-Check ($taskRejected -and $taskCommands -contains 'scheduler jobs pause') 'Failed build did not fail closed after pausing'
Assert-Check ($taskCommands -notcontains 'scheduler jobs resume') 'Failed build resumed scheduler'
Assert-Check ($taskCommands -notcontains 'run jobs deploy') 'Failed build continued deployment'
$taskCalls.Clear(); $taskFailBuild = $false; $taskBucketSafe = $false; $taskRejected = $false
try { & "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply } catch { $taskRejected = $true }
Assert-Check ($taskRejected -and $taskCalls.Count -eq 1) 'Unsafe bucket was not rejected before mutation'
$taskCalls.Clear(); $taskBucketSafe = $true; $taskFailScheduleLookup = $true; $taskRejected = $false
try { & "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply } catch { $taskRejected = $true }
Assert-Check ($taskRejected -and $taskCalls.Count -eq 2) 'Scheduler lookup failure was mistaken for an absent schedule'
$global:LASTEXITCODE = 0 # The simulated nonzero exit above is an expected, asserted failure.
Write-Host 'PASS: 8 local deployment scenarios plus PowerShell syntax. gcloud was stubbed; no cloud resources accessed.'
