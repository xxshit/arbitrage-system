[CmdletBinding()]
param(
    [string]$TaskName = "ArbitrageHub-CloudMySQLBackup",
    [string]$RunAt = "09:30"
)

$ErrorActionPreference = "Stop"
$pullScript = Join-Path $PSScriptRoot "pull_cloud_mysql_backup.ps1"
if (-not (Test-Path -LiteralPath $pullScript)) {
    throw "Backup pull script not found: $pullScript"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$pullScript`""
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

Write-Output "Scheduled task installed: $TaskName (daily $RunAt, runs after the next startup if missed)."
