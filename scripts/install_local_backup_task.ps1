[CmdletBinding()]
param(
    [string]$TaskName = "ArbitrageHub-CloudMySQLBackup",
    [string]$RunAt = "09:30"
)

$ErrorActionPreference = "Stop"
$pullScript = Join-Path $PSScriptRoot "pull_cloud_mysql_backup.ps1"
$hiddenRunner = Join-Path $PSScriptRoot "run_cloud_backup_hidden.vbs"
if (-not (Test-Path -LiteralPath $pullScript)) {
    throw "Backup pull script not found: $pullScript"
}
if (-not (Test-Path -LiteralPath $hiddenRunner)) {
    throw "Hidden backup runner not found: $hiddenRunner"
}

$action = New-ScheduledTaskAction `
    -Execute "$env:WINDIR\System32\wscript.exe" `
    -Argument "//B //Nologo `"$hiddenRunner`""
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Pull and verify the latest private cloud MySQL backup for Arbitrage Hub." `
    -Force | Out-Null

Write-Output "Scheduled task installed: $TaskName (daily $RunAt, hidden, runs after the next startup if missed)."
