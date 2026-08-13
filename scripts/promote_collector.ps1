[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [Parameter(Mandatory = $true)] [string]$PythonExe,
    [Parameter(Mandatory = $true)] [string]$Season,
    [Parameter(Mandatory = $true)] [string]$Cutoff,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-fA-F]{64}$')] [string]$ExpectedChecksum,
    [string]$TaskName = 'StatsPlus Residential Collector'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
$envFile = Join-Path $root 'collector.env.ps1'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'Collector configuration is not staged.' }
. $envFile
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
