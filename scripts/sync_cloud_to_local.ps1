[CmdletBinding()]
param(
    [switch]$SkipDownload,
    [switch]$RestoreDatabase,
    [string]$MariaBin = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $repositoryRoot ".env"
$backupRoot = Join-Path $repositoryRoot "backups\local-mysql"
$cloudBackupRoot = Join-Path $repositoryRoot "backups\cloud-mysql"
$cloudSecretsRoot = Join-Path $repositoryRoot "backups\cloud-secrets"
$localSecretsRoot = Join-Path $repositoryRoot "backups\local-secrets"

function Find-DatabaseTool([string[]]$names) {
    foreach ($name in $names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) { return $command.Source }
    }
    $candidateDirectories = @(
        $MariaBin,
        "F:\mysql\mariadb-11.8.6-winx64\bin",
        "C:\Program Files\MySQL\MySQL Server 8.0\bin",
        "C:\Program Files\MariaDB 11.8\bin"
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    foreach ($directory in $candidateDirectories) {
        foreach ($name in $names) {
            $candidate = Join-Path $directory $name
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

$dumpExe = Find-DatabaseTool @("mariadb-dump.exe", "mysqldump.exe")
$clientExe = Find-DatabaseTool @("mariadb.exe", "mysql.exe")

function Get-DatabaseConnection {
    if (-not (Test-Path -LiteralPath $envPath)) {
        throw "Missing local .env file. Configure DATABASE_URL before syncing."
    }
    $line = Get-Content -LiteralPath $envPath -Encoding UTF8 |
        Where-Object { $_ -match '^DATABASE_URL=' } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($line)) {
        throw "DATABASE_URL is missing from the local .env file."
    }
    $rawUrl = $line.Substring("DATABASE_URL=".Length).Trim().Trim('"').Trim("'")
    $uri = [Uri]$rawUrl
    $userParts = $uri.UserInfo.Split(':', 2)
    if ($userParts.Count -ne 2) {
        throw "DATABASE_URL must contain a username and password."
    }
    [pscustomobject]@{
        Host = $uri.Host
        Port = if ($uri.Port -gt 0) { $uri.Port } else { 3306 }
        User = [Uri]::UnescapeDataString($userParts[0])
        Password = [Uri]::UnescapeDataString($userParts[1])
        Database = $uri.AbsolutePath.TrimStart('/')
    }
}

function New-ProcessInfo([string]$fileName, [string]$arguments, [string]$password) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $fileName
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables["MYSQL_PWD"] = $password
    return $info
}

function Backup-LocalDatabase($connection) {
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $target = Join-Path $backupRoot "arbitrage_hub-before-cloud-sync-$stamp.sql.gz"
    $arguments = @(
        "--host=$($connection.Host)",
        "--port=$($connection.Port)",
        "--user=$($connection.User)",
        "--single-transaction",
        "--quick",
        "--routines",
        "--events",
        "--triggers",
        "--hex-blob",
        "--default-character-set=utf8mb4",
        "--databases",
        $connection.Database
    ) -join ' '
    $info = New-ProcessInfo $dumpExe $arguments $connection.Password
    $info.RedirectStandardOutput = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    if (-not $process.Start()) { throw "Could not start the local database backup." }
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $file = [System.IO.File]::Create($target)
    try {
        $gzip = New-Object System.IO.Compression.GZipStream($file, [System.IO.Compression.CompressionMode]::Compress)
        try { $process.StandardOutput.BaseStream.CopyTo($gzip) } finally { $gzip.Dispose() }
    } finally {
        $file.Dispose()
    }
    $process.WaitForExit()
    $stderr = $stderrTask.Result
    if ($process.ExitCode -ne 0) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        throw "Local database backup failed: $stderr"
    }
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    Set-Content -LiteralPath "$target.sha256" -Value "$hash  $(Split-Path -Leaf $target)" -Encoding ASCII
    Write-Host "Local rollback backup: $target"
    return $target
}

function Expand-SanitizedDump([string]$source, [string]$target) {
    $sourceStream = [System.IO.File]::OpenRead($source)
    try {
        $gzip = New-Object System.IO.Compression.GZipStream($sourceStream, [System.IO.Compression.CompressionMode]::Decompress)
        try {
            $reader = New-Object System.IO.StreamReader($gzip, [System.Text.UTF8Encoding]::new($false))
            $writer = New-Object System.IO.StreamWriter($target, $false, [System.Text.UTF8Encoding]::new($false))
            try {
                while (($line = $reader.ReadLine()) -ne $null) {
                    if ($line -match '^CREATE DATABASE ' -or $line -match '^USE `') { continue }
                    $writer.WriteLine($line)
                }
            } finally {
                $writer.Dispose()
                $reader.Dispose()
            }
        } finally {
            $gzip.Dispose()
        }
    } finally {
        $sourceStream.Dispose()
    }
}

function Restore-CloudDatabase($connection, [string]$cloudBackup) {
    $temporarySql = Join-Path ([System.IO.Path]::GetTempPath()) ("arbitrage-cloud-restore-{0}.sql" -f [Guid]::NewGuid().ToString('N'))
    try {
        Expand-SanitizedDump $cloudBackup $temporarySql
        $arguments = @(
            "--host=$($connection.Host)",
            "--port=$($connection.Port)",
            "--user=$($connection.User)",
            "--default-character-set=utf8mb4",
            $connection.Database
        ) -join ' '
        $info = New-ProcessInfo $clientExe $arguments $connection.Password
        $info.RedirectStandardInput = $true
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $info
        if (-not $process.Start()) { throw "Could not start the local database restore." }
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $sqlStream = [System.IO.File]::OpenRead($temporarySql)
        try { $sqlStream.CopyTo($process.StandardInput.BaseStream) } finally {
            $sqlStream.Dispose()
            $process.StandardInput.Close()
        }
        $process.WaitForExit()
        $stderr = $stderrTask.Result
        if ($process.ExitCode -ne 0) { throw "Local database restore failed: $stderr" }
    } finally {
        Remove-Item -LiteralPath $temporarySql -Force -ErrorAction SilentlyContinue
    }
}

if ([string]::IsNullOrWhiteSpace($dumpExe) -or [string]::IsNullOrWhiteSpace($clientExe)) {
    throw "MySQL/MariaDB command-line tools were not found. Pass -MariaBin with the local bin directory."
}

if (-not $SkipDownload) {
    $pullScript = Join-Path $PSScriptRoot "pull_cloud_mysql_backup.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $pullScript
    if ($LASTEXITCODE -ne 0) { throw "Downloading the cloud backup failed." }
}

$cloudBackup = Get-ChildItem -LiteralPath $cloudBackupRoot -File -Filter "arbitrage_hub-*.sql.gz" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $cloudBackup) { throw "No verified cloud database backup was found." }

$connection = Get-DatabaseConnection
$localBackup = Backup-LocalDatabase $connection

if (-not $RestoreDatabase) {
    Write-Output "Cloud backup is ready. Re-run with -RestoreDatabase to replace the local database."
    exit 0
}

$cloudKey = Join-Path $cloudSecretsRoot "chat-encryption.key"
if (-not (Test-Path -LiteralPath $cloudKey)) {
    throw "The matching cloud chat encryption key is missing. Restore was stopped."
}

New-Item -ItemType Directory -Force -Path $localSecretsRoot | Out-Null
$currentKey = Join-Path $repositoryRoot ".chat_encryption.key"
if (Test-Path -LiteralPath $currentKey) {
    $keyBackup = Join-Path $localSecretsRoot ("chat-encryption-before-cloud-sync-{0}.key" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    Copy-Item -LiteralPath $currentKey -Destination $keyBackup
}

Restore-CloudDatabase $connection $cloudBackup.FullName
Copy-Item -LiteralPath $cloudKey -Destination $currentKey -Force
Write-Output "Local MySQL now matches cloud backup: $($cloudBackup.Name)"
Write-Output "Rollback database backup remains at: $localBackup"
