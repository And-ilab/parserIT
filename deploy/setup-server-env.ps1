# Создаёт C:\tender_it\.env для ручного запуска парсеров (файл в .gitignore, не коммитить).
# Запуск от администратора на сервере:
#   cd C:\tender_it
#   powershell -ExecutionPolicy Bypass -File deploy\setup-server-env.ps1 `
#     -BotToken "..." -EquipmentChatId "-5141347518"
#
param(
    [Parameter(Mandatory = $true)]
    [string] $BotToken,
    [string] $EquipmentChatId = "-5141347518",
    [string] $RepoPath = "C:\tender_it"
)

$envPath = Join-Path $RepoPath ".env"
$lines = @(
    "# Локальные секреты — не коммитить. Создано setup-server-env.ps1",
    "TELEGRAM_BOT_TOKEN=$BotToken",
    "EQUIPMENT_TELEGRAM_CHAT_ID=$EquipmentChatId",
    "EQUIPMENT_TELEGRAM_BOT_TOKEN=$BotToken"
)
Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8
Write-Host "Записано: $envPath"
Write-Host "Для python подгрузите переменные перед запуском (см. deploy/load-dotenv.ps1)."
