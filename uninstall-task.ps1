# Removes the scheduled task created by install-task.ps1.
Unregister-ScheduledTask -TaskName "email-sorter" -Confirm:$false
Write-Host "Scheduled task 'email-sorter' removed."
