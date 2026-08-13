[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)] [string]$InstallRoot,
    [Parameter(Mandatory = $true)] [string]$StagedRelease,
    [Parameter(Mandatory = $true)] [string]$ReleaseVersion,
    [string]$TaskName = 'StatsPlus Residential Collector'
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallRoot)
$staged = [IO.Path]::GetFullPath($StagedRelease)
if (-not (Test-Path -LiteralPath $staged -PathType Container)) { throw "Staged release not found: $staged" }
if ($ReleaseVersion -notmatch '^[A-Za-z0-9._-]+$') { throw 'ReleaseVersion is malformed.' }
if ($staged -eq $root -or $staged.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'StagedRelease must be a separate immutable release directory.'
}
$releases = Join-Path $root 'releases'
New-Item -ItemType Directory -Force -Path $releases | Out-Null
$target = Join-Path $releases $ReleaseVersion
if (Test-Path -LiteralPath $target) { throw "Release version already exists: $ReleaseVersion" }
if ($PSCmdlet.ShouldProcess($target, 'copy immutable staged release')) {
    $temporary = Join-Path $releases ('.' + $ReleaseVersion + '.staging.' + [guid]::NewGuid().ToString('N'))
    try {
        Copy-Item -LiteralPath $staged -Destination $temporary -Recurse -Force
        Move-Item -LiteralPath $temporary -Destination $target
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
    $previous = Join-Path $root 'current.txt'
    if (Test-Path -LiteralPath $previous) { Copy-Item -LiteralPath $previous -Destination (Join-Path $root 'previous.txt') -Force }
    Set-Content -LiteralPath $previous -Value $ReleaseVersion -Encoding ASCII
}
Write-Output "Staged $ReleaseVersion. Run one foreground rehearsal, then enable the task explicitly."
