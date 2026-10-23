# Copy and replace with your EXISTING resource identifiers. These are illustrative, not real resources.
$CampusDeploy = @{
    ProjectId = 'your-campus-project'
    Region = 'asia-east2'
    Service = 'tju-campus'
    Bucket = 'your-private-campus-snapshots'
    CloudSqlInstance = 'your-campus-project:asia-east2:campus-postgres'
    RuntimeAccount = 'campus-runtime@your-campus-project.iam.gserviceaccount.com'
    SchedulerAccount = 'campus-scheduler@your-campus-project.iam.gserviceaccount.com'
    Image = 'asia-east2-docker.pkg.dev/your-campus-project/campus/tju:1.2.0'
    DatabaseSecret = 'campus-database-url:1'
    AppSecret = 'campus-app-key:1'
    SmtpPasswordSecret = 'campus-smtp-password:1'
    AdminEmails = 'admin@your-school.example'
    SmtpHost = 'smtp.your-provider.example'
    SmtpUsername = 'campus-sender'
    SmtpFrom = 'campus@your-school.example'
    # Optional protected feed references. Use the actual source UUID without hyphens:
    FeedSecrets = @{
        # CAMPUS_SOURCE_F6A3B540865049DD9DB12DDAA786C641_TOKEN = 'campus-college-feed:1'
    }
}
# No commands run without -Apply. First review deployment and access changes:
& "$PSScriptRoot/deploy.ps1" @CampusDeploy
# After reading DEPLOY.md and preparing existing resources, explicitly deploy privately:
# & "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply
# Complete private acceptance, then explicitly enable scheduling and public student access:
# & "$PSScriptRoot/deploy.ps1" @CampusDeploy -Apply -EnableScheduler -OpenToStudents
