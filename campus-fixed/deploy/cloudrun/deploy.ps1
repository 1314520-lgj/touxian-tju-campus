param(
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9-]{4,28}[a-z0-9]$')][string]$ProjectId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z]+-[a-z]+[0-9]$')][string]$Region,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9-]{0,48}$')][string]$Service,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$')][string]$Bucket,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9-]+:[a-z]+-[a-z]+[0-9]:[a-z][a-z0-9-]+$')][string]$CloudSqlInstance,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$')][string]$RuntimeAccount,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$')][string]$SchedulerAccount,
    [Parameter(Mandatory)][ValidatePattern('^[a-z]+-[a-z]+[0-9]-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/[a-z0-9._/-]+:[a-zA-Z0-9._-]+$')][string]$Image,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]+:[1-9][0-9]*$')][string]$DatabaseSecret,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]+:[1-9][0-9]*$')][string]$AppSecret,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]+:[1-9][0-9]*$')][string]$SmtpPasswordSecret,
    [Parameter(Mandatory)][ValidatePattern('^[^\s|]+@[^\s|]+(?:,[^\s|]+@[^\s|]+)*$')][string]$AdminEmails,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9.-]+$')][string]$SmtpHost,
    [Parameter(Mandatory)][ValidatePattern('^[^\s|]+$')][string]$SmtpUsername,
    [Parameter(Mandatory)][ValidatePattern('^[^\s|]+@[^\s|]+$')][string]$SmtpFrom,
    [hashtable]$FeedSecrets = @{},
    [switch]$Apply,
    [switch]$EnableScheduler,
    [switch]$OpenToStudents
)
$ErrorActionPreference = 'Stop'
$taskApplicationRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$taskCloudProject, $taskSqlRegion, $taskSqlName = $CloudSqlInstance.Split(':')
if ($taskCloudProject -ne $ProjectId -or $taskSqlRegion -ne $Region) { throw 'Cloud SQL must be in the selected project and region.' }
if ($Image -notlike "$Region-docker.pkg.dev/$ProjectId/*") { throw 'Image must use the selected regional Artifact Registry project.' }
if ($RuntimeAccount -eq $SchedulerAccount) { throw 'Use distinct runtime and scheduler service accounts.' }
$taskJob = "$Service-worker"
$taskInitJob = "$Service-init"
$taskSchedule = "$Service-tick"
$taskExistingSchedule = $false
function Invoke-CloudCommand {
    param([string[]]$Arguments, [switch]$Capture)
    # Display argument JSON for review. Actual execution passes an array, never a built shell string.
    Write-Host ('gcloud ' + (ConvertTo-Json -InputObject $Arguments -Compress))
    if (-not $Apply) { return }
    $taskResult = & gcloud @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "gcloud failed: $($Arguments[0..1] -join ' '). Inspect the Cloud console; no secret values are printed by this script." }
    if ($Capture) { return ($taskResult -join "`n") }
    $taskResult | ForEach-Object { Write-Host $_ }
}
$taskSecrets = "CAMPUS_DATABASE_URL=$DatabaseSecret,CAMPUS_SECRET=$AppSecret,CAMPUS_SMTP_PASSWORD=$SmtpPasswordSecret"
foreach ($taskFeedVariable in @($FeedSecrets.Keys | Sort-Object)) {
    if ($taskFeedVariable -cnotmatch '^CAMPUS_SOURCE_[0-9A-F]{32}_TOKEN$' -or
        $FeedSecrets[$taskFeedVariable] -notmatch '^[A-Za-z0-9_-]+:[1-9][0-9]*$') {
        throw 'FeedSecrets must map CAMPUS_SOURCE_<32 uppercase UUID hex>_TOKEN to secret-name:numeric-version.'
    }
    $taskSecrets += ",$taskFeedVariable=$($FeedSecrets[$taskFeedVariable])"
}
Write-Host 'Default is review only. -Apply builds/deploys against existing resources and can incur charges.'
Write-Host 'Scheduler and public access each require their separate switch, after private acceptance.'
if ($Apply) {
    Get-Command gcloud -ErrorAction Stop | Out-Null
    $taskBucket = Invoke-CloudCommand -Capture -Arguments @('storage','buckets','describe',"gs://$Bucket",'--raw','--format=json',"--project=$ProjectId") | ConvertFrom-Json
    if ($taskBucket.iamConfiguration.publicAccessPrevention -ne 'enforced' -or -not $taskBucket.iamConfiguration.uniformBucketLevelAccess.enabled) {
        throw 'Bucket must enforce public access prevention and uniform bucket-level access.'
    }
    # A previous schedule must not start old-image workers while this update is
    # switching origins/configuration. Resume is explicit after acceptance.
    # An empty successful list means absent. Permission/network errors must abort,
    # rather than being mistaken for a missing schedule that is safe to ignore.
    $taskScheduleName = "projects/$ProjectId/locations/$Region/jobs/$taskSchedule"
    $taskListedSchedule = [string](Invoke-CloudCommand -Capture -Arguments @('scheduler','jobs','list',"--location=$Region","--project=$ProjectId","--filter=name=$taskScheduleName",'--format=value(name)'))
    if ($taskListedSchedule.Trim() -and $taskListedSchedule.Trim() -ne $taskScheduleName) { throw 'Unexpected Scheduler lookup result.' }
    $taskExistingSchedule = $taskListedSchedule.Trim() -eq $taskScheduleName
    if ($taskExistingSchedule) {
        Invoke-CloudCommand -Arguments @('scheduler','jobs','pause',$taskSchedule,"--location=$Region","--project=$ProjectId",'--quiet')
        Write-Host 'Existing scheduler paused for deployment; -EnableScheduler explicitly resumes it.'
    }
}
function Get-CampusEnvironment([string]$Origin) {
    return "^|^CAMPUS_ENV=production|CAMPUS_DEV_AUTH=false|CAMPUS_DEMO=false|CAMPUS_EMBEDDED_WORKER=false|CAMPUS_BLOB_STORE=gcs|CAMPUS_GCS_BUCKET=$Bucket|CAMPUS_DATA_DIR=/tmp/campus|CAMPUS_PUBLIC_ORIGIN=$Origin|CAMPUS_ALLOWED_ORIGINS=$Origin|CAMPUS_ADMIN_EMAILS=$AdminEmails|CAMPUS_SMTP_HOST=$SmtpHost|CAMPUS_SMTP_PORT=587|CAMPUS_SMTP_STARTTLS=true|CAMPUS_SMTP_SSL=false|CAMPUS_SMTP_USERNAME=$SmtpUsername|CAMPUS_SMTP_FROM=$SmtpFrom|CAMPUS_SOURCE_REFRESH_SECONDS=1800|CAMPUS_MEILI_URL="
}
Invoke-CloudCommand -Arguments @('builds','submit',$taskApplicationRoot,"--config=$PSScriptRoot/cloudbuild.yaml","--ignore-file=$PSScriptRoot/.gcloudignore","--substitutions=_IMAGE=$Image","--project=$ProjectId",'--quiet')
# Bootstrap with a deliberately unusable exact origin while IAM keeps the service private.
Invoke-CloudCommand -Arguments @('run','deploy',$Service,"--image=$Image","--project=$ProjectId","--region=$Region",'--execution-environment=gen2','--port=8080',"--service-account=$RuntimeAccount","--add-cloudsql-instances=$CloudSqlInstance","--set-secrets=$taskSecrets",("--set-env-vars="+(Get-CampusEnvironment 'https://bootstrap.invalid')),'--min=0','--max=3','--concurrency=16','--cpu=1','--memory=1Gi','--timeout=60s','--no-allow-unauthenticated','--quiet')
$taskOrigin = 'https://SERVICE-PROJECT_NUMBER.REGION.run.app'
if ($Apply) {
    $taskOrigin = (Invoke-CloudCommand -Capture -Arguments @('run','services','describe',$Service,"--project=$ProjectId","--region=$Region",'--format=value(status.url)')).Trim()
    if ($taskOrigin -notmatch '^https://[a-z0-9.-]+\.run\.app$') { throw 'Could not read a valid fixed Cloud Run service URL.' }
}
Invoke-CloudCommand -Arguments @('run','services','update',$Service,"--project=$ProjectId","--region=$Region",("--set-env-vars="+(Get-CampusEnvironment $taskOrigin)),'--quiet')
foreach ($taskSpec in @(@{Name=$taskJob;Command='worker';ArgsPrefix=',--max-seconds,240,--max-rounds,500,--max-jobs,100'},@{Name=$taskInitJob;Command='init-sources';ArgsPrefix=''})) {
    $taskArguments = @('run','jobs','deploy',$taskSpec.Name,"--image=$Image","--project=$ProjectId","--region=$Region","--service-account=$RuntimeAccount","--set-cloudsql-instances=$CloudSqlInstance","--set-secrets=$taskSecrets",("--set-env-vars="+(Get-CampusEnvironment $taskOrigin)),'--command=python',"--args=-m,server.campus.cloud_run,$($taskSpec.Command)$($taskSpec.ArgsPrefix)",'--tasks=1','--parallelism=1','--max-retries=0','--task-timeout=1800s','--cpu=1','--memory=1Gi','--quiet')
    Invoke-CloudCommand -Arguments $taskArguments
}
Write-Host "Fixed service URL: $taskOrigin"
Write-Host "Before opening: execute $taskInitJob once, then $taskJob; inspect jobs and test private login, snapshots and retrieval."
if ($EnableScheduler) {
    # Google Run Jobs API is a *.googleapis.com target: OAuth, not OIDC.
    $taskSchedulerVerb = 'create'
    if ($Apply -and $taskExistingSchedule) { $taskSchedulerVerb = 'update' }
    Invoke-CloudCommand -Arguments @('scheduler','jobs',$taskSchedulerVerb,'http',$taskSchedule,"--project=$ProjectId","--location=$Region",'--schedule=* * * * *','--time-zone=Asia/Shanghai','--http-method=POST',"--uri=https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/${taskJob}:run","--oauth-service-account-email=$SchedulerAccount",'--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform','--attempt-deadline=30s','--max-retry-attempts=0','--quiet')
    if ($taskSchedulerVerb -eq 'update') {
        Invoke-CloudCommand -Arguments @('scheduler','jobs','resume',$taskSchedule,"--location=$Region","--project=$ProjectId",'--quiet')
    }
}
if ($OpenToStudents) {
    Invoke-CloudCommand -Arguments @('run','services','add-iam-policy-binding',$Service,"--project=$ProjectId","--region=$Region",'--member=allUsers','--role=roles/run.invoker','--quiet')
}
if (-not $Apply) { Write-Host 'Review completed. No cloud command was executed.' }
