# Выдаёт права учётной записи службы GitHub Actions runner на каталог с клоном репозитория.
# Служба по умолчанию работает от NT AUTHORITY\NETWORK SERVICE — ей нужны Modify на C:\tender_it для git pull.
#
# Запуск (PowerShell от администратора):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   .\deploy\grant-networkservice-repo-access.ps1
# Или с другим путём:
#   .\deploy\grant-networkservice-repo-access.ps1 -RepoPath 'D:\repos\parserIT'

[CmdletBinding()]
param(
    [string] $RepoPath = 'C:\tender_it'
)

$ErrorActionPreference = 'Stop'

# Служба GitHub runner (у вас в логе): actions.runner.And-ilab-parserIT.*
$Principal = 'NT AUTHORITY\NETWORK SERVICE'

if (-not (Test-Path -LiteralPath $RepoPath)) {
    Write-Error "Каталог не найден: $RepoPath"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error 'Запустите PowerShell от имени администратора (ПКМ → Запуск от имени администратора).'
}

Write-Host "Выдача прав $Principal на $RepoPath (наследование на подпапки и файлы)..." -ForegroundColor Cyan

# (OI) — наследование на файлы, (CI) — на папки, M — изменение (нужно для git fetch/pull)
& icacls.exe $RepoPath /grant "${Principal}:(OI)(CI)M" | Out-Host

if ($LASTEXITCODE -ne 0) {
    Write-Error "icacls завершился с кодом $LASTEXITCODE"
}

Write-Host 'Готово. Проверка (должна быть строка с NETWORK SERVICE):' -ForegroundColor Green
& icacls.exe $RepoPath | Select-String -Pattern 'NETWORK SERVICE' -SimpleMatch

Write-Host "`nДалее: перезапустите службу раннера (services.msc → actions.runner...) или:" -ForegroundColor Yellow
Write-Host '  Get-Service actions.runner.* | Restart-Service' -ForegroundColor Gray
