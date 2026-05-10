# Grant NT AUTHORITY\NETWORK SERVICE Modify on the git clone folder (for GitHub Actions self-hosted runner service).
#
# Run in elevated PowerShell (Run as administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   .\deploy\grant-networkservice-repo-access.ps1
# Optional path:
#   .\deploy\grant-networkservice-repo-access.ps1 -RepoPath 'D:\repos\parserIT'

[CmdletBinding()]
param(
    [string] $RepoPath = 'C:\tender_it'
)

$ErrorActionPreference = 'Stop'

$Principal = 'NT AUTHORITY\NETWORK SERVICE'
$Icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'

if (-not (Test-Path -LiteralPath $RepoPath)) {
    Write-Error "Path not found: $RepoPath"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error 'Run PowerShell as Administrator.'
}

Write-Host "Granting $Principal Modify (OI)(CI) on $RepoPath ..." -ForegroundColor Cyan

$grantArg = "${Principal}:(OI)(CI)M"
$p = Start-Process -FilePath $Icacls -ArgumentList @($RepoPath, '/grant', $grantArg) -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) {
    Write-Error "icacls failed with exit code $($p.ExitCode)"
}

Write-Host 'Done. Expect a line containing NETWORK SERVICE below:' -ForegroundColor Green
& $Icacls $RepoPath | Select-String -Pattern 'NETWORK SERVICE' -SimpleMatch

Write-Host ''
Write-Host 'Restart the runner service, e.g.:' -ForegroundColor Yellow
Write-Host '  Get-Service actions.runner.* | Restart-Service' -ForegroundColor Gray
