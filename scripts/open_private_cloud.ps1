[CmdletBinding()]
param(
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $repositoryRoot "cloud-access.local.json"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Private access configuration is missing. Copy cloud-access.example.json to cloud-access.local.json first."
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$sshPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
$keyPath = [Environment]::ExpandEnvironmentVariables([string]$config.keyPath)
$localPort = [int]$config.localPort
$url = "http://127.0.0.1:${localPort}"

if (-not (Test-Path -LiteralPath $sshPath)) {
    throw "Windows OpenSSH Client is not installed."
}
if (-not (Test-Path -LiteralPath $keyPath)) {
    throw "Private access key is missing: $keyPath"
}

function Test-PrivateWebsite {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/login" -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

if (-not (Test-PrivateWebsite)) {
    $forward = "127.0.0.1:${localPort}:$($config.remoteHost):$([int]$config.remotePort)"
    $target = "$($config.sshUser)@$($config.serverHost)"
    $sshArguments = @(
        "-N",
        "-i", $keyPath,
        "-p", [string][int]$config.sshPort,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        "-o", "LogLevel=ERROR",
        "-L", $forward,
        $target
    )
    $sshProcess = Start-Process -FilePath $sshPath -ArgumentList $sshArguments -WindowStyle Hidden -PassThru

    $ready = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 500
        if ($sshProcess.HasExited) { break }
        if (Test-PrivateWebsite) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        if (-not $sshProcess.HasExited) { Stop-Process -Id $sshProcess.Id -Force }
        throw "The private cloud tunnel could not be established."
    }
}

Start-Process $url
