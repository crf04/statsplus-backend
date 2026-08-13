[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [string]$TaskName = 'StatsPlus Residential Collector'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
$previousFile = Join-Path $root 'previous.txt'
if (-not (Test-Path -LiteralPath $previousFile)) { throw 'No explicitly staged previous release is available.' }
$previous = (Get-Content -LiteralPath $previousFile -Raw).Trim()
if ($previous -notmatch '^[A-Za-z0-9._-]+$') { throw 'Previous release marker is malformed.' }
$target = Join-Path (Join-Path $root 'releases') $previous
if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Previous release directory is missing: $previous" }
if ($PSCmdlet.ShouldProcess($root, "rollback to $previous")) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    $current = Join-Path $root 'current.txt'
    $currentValue = if (Test-Path -LiteralPath $current) { Get-Content -LiteralPath $current -Raw } else { '' }
    Set-Content -LiteralPath (Join-Path $root 'previous.txt') -Value $currentValue.Trim() -Encoding ASCII
    Set-Content -LiteralPath $current -Value $previous -Encoding ASCII
}
Write-Output "Rolled back the staged pointer to $previous with task disabled. Promote only after validation."
