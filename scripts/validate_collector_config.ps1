[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$RailwayUrl,
    [Parameter(Mandatory = $true)] [ValidateSet('development','testing','test','staging','production','historical_rehearsal')] [string]$Environment,
    [Parameter(Mandatory = $true)] [string]$IdentityId,
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [string]$PythonExe = 'python.exe',
    [string]$ReleaseVersion = '0.1.0'
)

$ErrorActionPreference = 'Stop'
$uri = [Uri]$RailwayUrl
if ($uri.Scheme -ne 'https' -and -not ($Environment -in @('development','testing','test','historical_rehearsal') -and $uri.Host -in @('localhost','127.0.0.1'))) {
    throw 'The collector endpoint must use HTTPS outside an explicit local rehearsal.'
}
if ([string]::IsNullOrWhiteSpace($IdentityId) -or [string]::IsNullOrWhiteSpace($ReleaseVersion)) {
    throw 'IdentityId and ReleaseVersion are required.'
}
$root = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw "Install root does not exist: $root"
}
$python = Get-Command $PythonExe -ErrorAction Stop
Write-Output ('Collector configuration valid: environment={0}; identity={1}; release={2}; python={3}' -f $Environment,$IdentityId,$ReleaseVersion,$python.Source)
