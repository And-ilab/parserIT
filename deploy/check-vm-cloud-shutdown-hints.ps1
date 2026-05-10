# Detect cloud / hypervisor hints and common auto-shutdown signals from inside Windows.
# Run in PowerShell (admin not required). Does not change settings.
#
# If this VM is in Azure/AWS/GCP, also open the provider portal and check:
# - Azure: Auto-shutdown, DevTest Labs, Automation runbooks
# - AWS: Instance Scheduler, Lambda, EventBridge rules on EC2
# - GCP: Compute schedules, preemptible / spot

$ErrorActionPreference = 'Continue'

Write-Host '=== systeminfo (hypervisor line) ===' -ForegroundColor Cyan
systeminfo.exe 2>$null | Select-String -Pattern 'Hyper-V|Virtual|VMware|Parallels|KVM|QEMU' -CaseSensitive:$false

Write-Host "`n=== ComputerSystem (VM hint) ===" -ForegroundColor Cyan
Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue |
    Select-Object Manufacturer, Model, HypervisorPresent |
    Format-List

Write-Host "`n=== Services (cloud / virtualization) ===" -ForegroundColor Cyan
$svcPat = 'azure|waagent|rdagent|amazon|aws|google|gce|vbox|vmware|vmtools|vmic|qemu|xen|parallels'
Get-Service -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name + ' ' + $_.DisplayName) -match $svcPat } |
    Sort-Object Name |
    Format-Table Name, Status, DisplayName -AutoSize

Write-Host '=== Scheduled tasks (shutdown / azure / auto) ===' -ForegroundColor Cyan
Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object {
        $t = ($_.TaskName + ' ' + $_.TaskPath).ToLowerInvariant()
        $t -match 'shutdown|azure|auto.?shut|idle|dealloc|stop.?vm|hibernat'
    } |
    Select-Object TaskName, TaskPath, State |
    Format-Table -AutoSize

Write-Host '=== Metadata endpoint 169.254.169.254 (cloud) ===' -ForegroundColor Cyan
try {
    $h = @{ Metadata = 'true' }
    $az = Invoke-RestMethod -UseBasicParsing -Headers $h -Uri 'http://169.254.169.254/metadata/instance?api-version=2021-02-01' -TimeoutSec 2 -ErrorAction Stop
    Write-Host 'Azure IMDS responded (JSON keys):' -ForegroundColor Green
    ($az.compute | Get-Member -MemberType NoteProperty | Select-Object -ExpandProperty Name) -join ', '
} catch {
    Write-Host 'Azure IMDS: no response or not Azure.' -ForegroundColor Yellow
}
try {
    $id = Invoke-RestMethod -UseBasicParsing -Uri 'http://169.254.169.254/latest/meta-data/instance-id' -TimeoutSec 2 -ErrorAction Stop
    Write-Host "AWS instance-id: $id" -ForegroundColor Green
} catch {
    Write-Host 'AWS IMDS: no response or not EC2.' -ForegroundColor Yellow
}

Write-Host "`n=== Done. Interpret together with your hosting panel (auto-shutdown, spot, budgets). ===" -ForegroundColor Gray
