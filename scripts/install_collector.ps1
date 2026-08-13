[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [Parameter(Mandatory = $true)] [string]$PythonExe,
    [Parameter(Mandatory = $true)] [string]$RailwayUrl,
    [Parameter(Mandatory = $true)] [ValidateSet('development','testing','staging','production','historical_rehearsal')] [string]$Environment,
    [Parameter(Mandatory = $true)] [string]$IdentityId,
    [string]$TaskName = 'StatsPlus Residential Collector',
    [Parameter(Mandatory = $true)] [string]$RunAsUser,
    [string]$ReleaseVersion = '0.1.0'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
if ($ReleaseVersion -notmatch '^[A-Za-z0-9._-]+$') { throw 'ReleaseVersion is malformed.' }
New-Item -ItemType Directory -Force -Path $root, (Join-Path $root 'logs'), (Join-Path $root 'data') | Out-Null
& "$PSScriptRoot\validate_collector_config.ps1" -RailwayUrl $RailwayUrl -Environment $Environment -IdentityId $IdentityId -InstallRoot $root -PythonExe $PythonExe -ReleaseVersion $ReleaseVersion

# The package is staged atomically by the release process.  This installer
# does not accept or write a machine secret; provision it in Windows Credential
# Manager with target StatsPlus/Collector/<identity> as a separate protected step.
$envFile = Join-Path $root 'collector.env.ps1'
function ConvertTo-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}
$envLines = @(
    ('$env:COLLECTOR_RAILWAY_URL = ' + (ConvertTo-PowerShellLiteral $RailwayUrl)),
    ('$env:COLLECTOR_ENVIRONMENT = ' + (ConvertTo-PowerShellLiteral $Environment)),
    ('$env:COLLECTOR_IDENTITY_ID = ' + (ConvertTo-PowerShellLiteral $IdentityId)),
    ('$env:COLLECTOR_OUTBOX_PATH = ' + (ConvertTo-PowerShellLiteral (Join-Path $root 'data\outbox.sqlite3'))),
    ('$env:COLLECTOR_LOG_PATH = ' + (ConvertTo-PowerShellLiteral (Join-Path $root 'logs\collector.log'))),
    ('$env:COLLECTOR_RELEASE_VERSION = ' + (ConvertTo-PowerShellLiteral $ReleaseVersion)),
    ('$env:COLLECTOR_ALLOW_INSECURE_LOCALHOST = ' + (ConvertTo-PowerShellLiteral 'false'))
)
$envLines | Set-Content -LiteralPath $envFile -Encoding UTF8
$wrapper = Join-Path $root 'collector_task_wrapper.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'collector_task_wrapper.ps1') -Destination $wrapper -Force

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonExe "{1}" -InstallRoot "{2}" -EnvFile "{3}"' -f $wrapper,$PythonExe,$root,$envFile) -WorkingDirectory $root
$daily = New-ScheduledTaskTrigger -Daily -At '04:00'
$startup = New-ScheduledTaskTrigger -AtStartup
# S4U stores no password in the task definition and runs when the account is
# not interactively logged on. Grant the dedicated account "Log on as a batch
# job" during workstation provisioning.
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
if ($PSCmdlet.ShouldProcess($TaskName, 'register one non-overlapping scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($daily,$startup) -Principal $principal -Settings $settings -Force | Out-Null
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
}
Write-Output "Installed disabled staged collector $ReleaseVersion. Use promote_collector.ps1 after all gates pass."
