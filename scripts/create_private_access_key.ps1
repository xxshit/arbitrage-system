[CmdletBinding()]
param(
    [string]$DeviceName = $env:COMPUTERNAME,
    [string]$OutputDirectory = "$HOME\.ssh"
)

$ErrorActionPreference = "Stop"
$sshKeygen = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keygen.exe"
if (-not (Test-Path -LiteralPath $sshKeygen)) {
    throw "Windows OpenSSH Client is not installed."
}

$safeDeviceName = ($DeviceName -replace '[^A-Za-z0-9_-]', '_').ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($safeDeviceName)) { $safeDeviceName = "device" }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$keyPath = Join-Path $OutputDirectory "arbitrage_hub_${safeDeviceName}"

if (-not (Test-Path -LiteralPath $keyPath)) {
    & $sshKeygen -q -t ed25519 -f $keyPath -N "" -C "arbitrage-hub:${safeDeviceName}"
    if ($LASTEXITCODE -ne 0) { throw "Creating the private access key failed." }
}

$publicKeyPath = "$keyPath.pub"
$publicKey = (Get-Content -LiteralPath $publicKeyPath -Raw).Trim()
if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
    Set-Clipboard -Value $publicKey
}

Write-Output "Private key: $keyPath"
Write-Output "Public key:  $publicKeyPath"
Write-Output "The public key has been copied to the clipboard. Send only the .pub content for server authorization; never send the private key."
