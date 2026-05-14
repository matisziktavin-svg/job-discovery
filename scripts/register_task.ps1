# Register MizzixJobDiscovery scheduled task. Re-runnable (unregisters
# existing task first if present).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
# Runs daily at 3:00 AM as the current user. Requires the repo's venv at
# .venv\Scripts\python.exe (Python 3.12 — system 3.13 can't install jobspy).

$ErrorActionPreference = "Stop"

$TaskName  = "MizzixJobDiscovery"
$RepoRoot  = (Resolve-Path "$PSScriptRoot\..").Path
$VenvPy    = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$VaultPath = "C:\Users\matis\Desktop\Second Brain"
$LogPath   = Join-Path $VaultPath ".mizzix_state\job_discovery.log"

if (-not (Test-Path $VenvPy)) {
    Write-Error "Venv python not found at $VenvPy. Create the venv first: py -3.12 -m venv .venv && .venv\Scripts\pip install -e ."
    exit 1
}

# Build the action: cmd.exe so we can set the env var inline AND redirect
# stdout/stderr. The double-quoting is intentional — cmd.exe needs double-
# quotes around the python path because it has spaces.
$ActionArgs = "/c set `"VAULT_PATH=$VaultPath`" && `"$VenvPy`" -m job_discovery.cli scan >> `"$LogPath`" 2>&1"
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $ActionArgs `
    -WorkingDirectory $RepoRoot

# Daily at 3:00 AM (intentionally before MizzixReflection at 3:30 — reflection
# shouldn't see incomplete state).
$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00am

# Run as the current user, only when logged in (no need to store credentials).
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Unregister + re-register (safe to repeat)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Daily job-discovery scan at 3am. Repo: $RepoRoot. See DESIGN.md."

Write-Output "Registered $TaskName."
Write-Output "  Action: $ActionArgs"
Write-Output "  Trigger: daily 3:00 AM"
Write-Output "  Logs: $LogPath"
Write-Output ""
Write-Output "Test manually with: schtasks /Run /TN $TaskName"
