# Registers a Windows scheduled task that runs the sorter in LIVE mode
# every N minutes while you are logged in. Run it yourself once you are
# happy with the dry-run reports:
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 [-IntervalMinutes 10]
param([int]$IntervalMinutes = 10)
$ErrorActionPreference = "Stop"

$root   = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found at $python - see README (setup)." }
if (-not (Test-Path (Join-Path $root ".env"))) { throw ".env missing - copy .env.example and fill it in." }

$action   = New-ScheduledTaskAction -Execute $python -Argument "-m email_sorter --live" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
              -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "email-sorter" -Action $action -Trigger $trigger -Settings $settings `
  -Description "Sorts the Strato inbox with Jev (OpenRouter). Logs: $root\logs" -Force | Out-Null

Write-Host "Scheduled task 'email-sorter' registered: every $IntervalMinutes min, LIVE mode."
Write-Host "Logs: $root\logs\email-sorter.log"
