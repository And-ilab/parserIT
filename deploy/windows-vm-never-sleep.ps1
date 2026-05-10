# Disable sleep/hibernate on AC for a Windows VM (so GitHub runner and cron jobs keep running).
# Run elevated PowerShell once on the VM.
#
# Does not stop: host shutdown, Azure "auto-shutdown", VMware suspend from host, etc.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error 'Run PowerShell as Administrator.'
}

Write-Host 'Turning off hibernate file (hiberfil.sys)...' -ForegroundColor Cyan
powercfg /hibernate off 2>$null

Write-Host 'Setting AC timeouts to 0 (never) for current power scheme...' -ForegroundColor Cyan
powercfg /change monitor-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

# Hybrid sleep off (SUB_SLEEP / HYBRIDSLEEP) — on VM usually not needed and can confuse diagnostics
$SUB_SLEEP = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
$HYBRIDSLEEP = '94ac6d29-73ce-41a6-809f-6363ba21b47e'
powercfg /setacvalueindex SCHEME_CURRENT $SUB_SLEEP $HYBRIDSLEEP 0
powercfg /setdcvalueindex SCHEME_CURRENT $SUB_SLEEP $HYBRIDSLEEP 0
powercfg /setactive SCHEME_CURRENT

# DC (laptop) profile if present
powercfg /change monitor-timeout-dc 0
powercfg /change disk-timeout-dc 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-dc 0

powercfg /setactive SCHEME_CURRENT

Write-Host 'Current active scheme:' -ForegroundColor Green
powercfg /getactivescheme

Write-Host 'List timeouts (AC should be 0 for sleep/hibernate):' -ForegroundColor Green
powercfg /query SCHEME_CURRENT SUB_SLEEP

Write-Host 'Done. Reboot optional. If VM still stops, check hypervisor / cloud auto-shutdown.' -ForegroundColor Yellow
