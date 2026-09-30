<#
Registers a Windows scheduled task that runs `mygarmin sync` daily at 01:00 and 13:00
for the current user. No admin rights needed. If the machine is off at that time the run
is simply skipped (no catch-up) - run `mygarmin sync` manually instead.

Usage:   .\scripts\register-task.ps1
Remove:  Unregister-ScheduledTask -TaskName my-garmin-sync -Confirm:$false
#>
param(
    [string]$TaskName = "my-garmin-sync",
    [string[]]$Times = @("01:00", "13:00")
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
# pythonw: no console window pops up on scheduled runs
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Nem található: $pythonw - előbb hozd létre a .venv-et (lásd README)." }
if (-not (Test-Path (Join-Path $repo "data\.garminconnect"))) {
    Write-Warning "Nincs mentett token. Futtasd előbb: .venv\Scripts\mygarmin login"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m mygarmin sync" -WorkingDirectory $repo
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "my-garmin: Garmin Connect -> lokális export ($($Times -join ', '))" -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
