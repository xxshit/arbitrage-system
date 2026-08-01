[CmdletBinding()]
param(
    [string]$ServerHost = "",
    [int]$Port = 0,
    [string]$User = "",
    [string]$KeyPath = "",
    [string]$LocalBackupRoot = "",
    [int]$RetentionDays = 180,
    [switch]$SkipRemoteBackup
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$privateConfigPath = Join-Path $repositoryRoot "cloud-backup.local.json"
if (Test-Path -LiteralPath $privateConfigPath) {
    $privateConfig = Get-Content -LiteralPath $privateConfigPath -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($ServerHost)) { $ServerHost = [string]$privateConfig.serverHost }
    if ($Port -le 0) { $Port = [int]$privateConfig.port }
    if ([string]::IsNullOrWhiteSpace($User)) { $User = [string]$privateConfig.user }
    if ([string]::IsNullOrWhiteSpace($KeyPath)) { $KeyPath = [string]$privateConfig.keyPath }
}
if ([string]::IsNullOrWhiteSpace($ServerHost)) {
    throw "Cloud backup host is missing. Copy cloud-backup.example.json to cloud-backup.local.json and fill it in."
}
if ($Port -le 0) { $Port = 22 }
if ([string]::IsNullOrWhiteSpace($User)) { $User = "root" }
if ([string]::IsNullOrWhiteSpace($KeyPath)) { $KeyPath = "$HOME\.ssh\id_ed25519" }
if ([string]::IsNullOrWhiteSpace($LocalBackupRoot)) {
    $LocalBackupRoot = Join-Path $repositoryRoot "backups\cloud-mysql"
}
$remoteRoot = "/var/backups/arbitrage-hub/mysql"
$sshTarget = "${User}@${ServerHost}"
$sshOptions = @(
    "-i", $KeyPath,
    "-p", $Port,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2"
)

if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}

New-Item -ItemType Directory -Force -Path $LocalBackupRoot | Out-Null

if (-not $SkipRemoteBackup) {
    & ssh @sshOptions $sshTarget "/opt/arbitrage-hub/scripts/backup_mysql.sh"
    if ($LASTEXITCODE -ne 0) {
        throw "Cloud database backup failed."
    }
}

$remoteFile = (& ssh @sshOptions $sshTarget "readlink -f '$remoteRoot/latest.sql.gz'").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteFile)) {
    throw "Could not locate the latest cloud database backup."
}

$fileName = Split-Path -Leaf $remoteFile
$localFile = Join-Path $LocalBackupRoot $fileName
$localChecksum = "$localFile.sha256"
$scpPortOptions = @(
    "-i", $KeyPath,
    "-P", $Port,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20"
)

& scp @scpPortOptions "${sshTarget}:$remoteFile" $localFile
if ($LASTEXITCODE -ne 0) {
    throw "Downloading the cloud database backup failed."
}
& scp @scpPortOptions "${sshTarget}:$remoteFile.sha256" $localChecksum
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $localFile -Force -ErrorAction SilentlyContinue
    throw "Downloading the checksum failed."
}

$expectedHash = ((Get-Content -LiteralPath $localChecksum -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actualHash = (Get-FileHash -LiteralPath $localFile -Algorithm SHA256).Hash.ToUpperInvariant()
if ($actualHash -ne $expectedHash) {
    Remove-Item -LiteralPath $localFile, $localChecksum -Force -ErrorAction SilentlyContinue
    throw "Backup checksum verification failed. The incomplete copy was removed."
}

Get-ChildItem -LiteralPath $LocalBackupRoot -File -Filter "arbitrage_hub-*.sql.gz*" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } |
    Remove-Item -Force

$sizeMb = [Math]::Round((Get-Item -LiteralPath $localFile).Length / 1MB, 2)
Write-Output "Local verified backup: $localFile ($sizeMb MB)"
