[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [Parameter(Mandatory = $true)] [string]$PythonExe,
    [Parameter(Mandatory = $true)] [string]$Season,
    [Parameter(Mandatory = $true)] [string]$Cutoff,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-fA-F]{64}$')] [string]$ExpectedChecksum,
    [Parameter(Mandatory = $true)] [string]$RailwayRehearsalResult,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-fA-F]{64}$')] [string]$RailwayEvidenceChecksum,
    [string]$TaskName = 'StatsPlus Residential Collector'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
$envFile = Join-Path $root 'collector.env.ps1'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'Collector configuration is not staged.' }
. $envFile
if (-not (Test-Path -LiteralPath $RailwayRehearsalResult -PathType Leaf)) { throw 'Railway rehearsal evidence is missing.' }
$evidenceHash = (Get-FileHash -LiteralPath $RailwayRehearsalResult -Algorithm SHA256).Hash
if ($evidenceHash -ne $RailwayEvidenceChecksum) { throw 'Railway rehearsal evidence checksum failed.' }
$evidence = Get-Content -LiteralPath $RailwayRehearsalResult -Raw | ConvertFrom-Json
$missingOperations = @('credential','auth','discovery','status','ingestion') | Where-Object { $_ -notin $evidence.operations }
if ($evidence.status -ne 'passed' -or $evidence.environment -eq 'production' -or
    $evidence.endpoint -notmatch '^https://' -or $missingOperations.Count -gt 0) {
    throw 'Isolated non-production Railway compatibility rehearsal evidence is incomplete.'
}
$current = (Get-Content -LiteralPath (Join-Path $root 'current.txt') -Raw).Trim()
$releaseRoot = Join-Path (Join-Path $root 'releases') $current
if (-not (Test-Path -LiteralPath $releaseRoot -PathType Container)) { throw 'Current release is not staged.' }
$env:COLLECTOR_RELEASE_ROOT = $releaseRoot
$env:PYTHONPATH = $releaseRoot
& $PythonExe -m statsplus_collector validate-config
if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed.' }
& $PythonExe -m statsplus_collector credential-check
if ($LASTEXITCODE -ne 0) { throw 'Credential validation failed.' }
$metadata = (& $PythonExe -m statsplus_collector release | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $metadata.checksum -ne $ExpectedChecksum.ToLowerInvariant()) { throw 'Release checksum validation failed.' }
& $PythonExe -m statsplus_collector rehearsal --season $Season --cutoff $Cutoff
if ($LASTEXITCODE -ne 0) { throw 'Compatibility rehearsal failed.' }
if ($PSCmdlet.ShouldProcess($TaskName, 'promote validated collector release')) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
}
Write-Output "Promoted validated collector release $current."
