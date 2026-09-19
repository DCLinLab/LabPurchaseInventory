[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$labRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$labSupervisor = Join-Path $labRepo 'supervise_bot.py'
$labPython = Join-Path $labRepo '.venv\Scripts\pythonw.exe'
$labTaskName = 'LabPurchaseBot-Standalone'
$labAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not (Test-Path -LiteralPath $labPython) -or -not (Test-Path -LiteralPath $labSupervisor)) {
    throw 'Create the project virtual environment before installing startup.'
}
$labExisting = Get-ScheduledTask -TaskName $labTaskName -ErrorAction SilentlyContinue
if ($labExisting -and ($labExisting.Actions.Execute -ne $labPython -or $labExisting.Actions.Arguments -notlike "*$labSupervisor*")) {
    throw 'A different task already uses this name; no changes were made.'
}
$labAction = New-ScheduledTaskAction -Execute $labPython -Argument ('-X utf8 -u "{0}"' -f $labSupervisor) -WorkingDirectory $labRepo
$labTrigger = New-ScheduledTaskTrigger -AtLogOn -User $labAccount
$labTrigger.Delay = 'PT30S'
$labPrincipal = New-ScheduledTaskPrincipal -UserId $labAccount -LogonType Interactive -RunLevel Limited
$labSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -Hidden
Register-ScheduledTask -TaskName $labTaskName -Action $labAction -Trigger $labTrigger -Principal $labPrincipal -Settings $labSettings -Description 'Runs LabPurchaseBot after Windows sign-in and restarts its listener after unexpected exits. Uses project-local Slack credentials and Codex ChatGPT login with Luna and shared subscription allowance.' -Force | Out-Null
Write-Output "Registered $labTaskName for $labAccount (30 seconds after sign-in)."
