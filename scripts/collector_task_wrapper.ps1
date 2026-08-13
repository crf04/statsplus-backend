[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$PythonExe,
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [Parameter(Mandatory = $true)] [string]$EnvFile,
    [int]$RecoveryHours = 6,
    [int]$RetryMinutes = 30
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "Collector environment file is missing: $EnvFile" }
. $EnvFile
$currentFile = Join-Path $root 'current.txt'
$releaseRoot = $root
if (Test-Path -LiteralPath $currentFile -PathType Leaf) {
    $releaseVersion = (Get-Content -LiteralPath $currentFile -Raw).Trim()
    if ($releaseVersion -notmatch '^[A-Za-z0-9._-]+$') { throw 'Current release marker is malformed.' }
    $candidate = Join-Path (Join-Path $root 'releases') $releaseVersion
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { throw "Current release directory is missing: $releaseVersion" }
    $releaseRoot = $candidate
}
$env:COLLECTOR_RELEASE_ROOT = $releaseRoot
$existingPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { '' }
$env:PYTHONPATH = $releaseRoot + [IO.Path]::PathSeparator + $existingPythonPath
$deadline = (Get-Date).ToUniversalTime().AddHours([Math]::Max(1, $RecoveryHours))
do {
    & $PythonExe -m statsplus_collector run
    $exitCode = $LASTEXITCODE
    if ($exitCode -notin @(10, 11)) { exit $exitCode }
    if (((Get-Date).ToUniversalTime()) -ge $deadline) { exit $exitCode }
    Start-Sleep -Seconds ([Math]::Max(60, $RetryMinutes * 60))
} while ($true)
