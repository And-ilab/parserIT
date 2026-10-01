# Деплой ветки ЕИС и один прогон на Windows-сервере (C:\tender_it).
# Первый раз (пока PR не в main) — скопируйте блок из комментария ниже в PowerShell.
#
#   Set-Location C:\tender_it
#   $sd = (Get-Location).Path
#   git -c "safe.directory=$sd" fetch origin
#   git -c "safe.directory=$sd" checkout cursor/zakupki-it-parser-c967
#   git -c "safe.directory=$sd" pull origin cursor/zakupki-it-parser-c967
#   python -m pip install -q -r requirements.txt
#   powershell -ExecutionPolicy Bypass -File deploy\win-zakupki-deploy-and-run.ps1
#
param(
    [string] $RepoPath = "C:\tender_it",
    [string] $Branch = "cursor/zakupki-it-parser-c967",
    [string] $ChatId = "-5385385913",
    [switch] $StayOnMain
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $RepoPath)) {
    throw "Нет каталога: $RepoPath"
}
Set-Location -LiteralPath $RepoPath
$sd = (Get-Location).Path
$opt = "safe.directory=$sd"

if (-not $StayOnMain) {
    git -c $opt fetch origin
    git -c $opt checkout $Branch
    git -c $opt pull origin $Branch
} else {
    git -c $opt fetch origin
    git -c $opt checkout main
    git -c $opt pull origin main
}
git -c $opt log -1 --oneline

if (Test-Path -LiteralPath 'requirements.txt') {
    python -m pip install -q -r requirements.txt
}

$envFile = Join-Path $RepoPath '.env'
if (Test-Path -LiteralPath $envFile) {
    $has = Select-String -LiteralPath $envFile -Pattern '^\s*ZAKUPKI_TELEGRAM_CHAT_ID=' -Quiet
    if (-not $has) {
        Add-Content -LiteralPath $envFile -Value "ZAKUPKI_TELEGRAM_CHAT_ID=$ChatId" -Encoding utf8
        Write-Host "Добавлено в .env: ZAKUPKI_TELEGRAM_CHAT_ID=$ChatId"
    }
} else {
    Write-Host "Нет .env — используем chat.id из кода ($ChatId). TELEGRAM_BOT_TOKEN возьмётся с запасного значения в репо."
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoPath 'zakupki_it_parser.py'))) {
    throw "zakupki_it_parser.py нет в $RepoPath — ветка не подтянулась."
}

Write-Host "Запуск zakupki_it_parser.py ..."
python zakupki_it_parser.py
exit [int]$LASTEXITCODE
