[CmdletBinding()]
param(
    [switch]$SkipDatabase
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repositoryRoot

$changes = @(git status --porcelain=v1)
if ($LASTEXITCODE -ne 0) { throw "This directory is not a valid Git checkout." }
if ($changes.Count -gt 0) {
    throw "The working tree has uncommitted changes. Finish or preserve them before switching computers."
}

git fetch origin master
if ($LASTEXITCODE -ne 0) { throw "Could not fetch origin/master." }

$ahead = [int](git rev-list --count origin/master..HEAD)
$behind = [int](git rev-list --count HEAD..origin/master)
if ($ahead -gt 0) {
    throw "This computer has $ahead unpublished commit(s). Push or hand them off before pulling another version."
}
if ($behind -gt 0) {
    git pull --ff-only origin master
    if ($LASTEXITCODE -ne 0) { throw "Fast-forward pull failed." }
}

if (-not $SkipDatabase) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync_cloud_to_local.ps1") -RestoreDatabase
    if ($LASTEXITCODE -ne 0) { throw "Cloud database synchronization failed." }
}

Write-Output "Workstation is synchronized. Read AGENTS.md, docs/README.md and docs/当前工作交接.md before editing."
