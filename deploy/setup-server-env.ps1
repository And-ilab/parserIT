# Creates C:\tender_it\.env for manual parser runs (.gitignore, do not commit).
# Example:
#   cd C:\tender_it
#   powershell -ExecutionPolicy Bypass -File deploy\setup-server-env.ps1 -BotToken "..." -EquipmentChatId "-5141347518"
param(
    [Parameter(Mandatory = $true)]
    [string] $BotToken,
    [string] $EquipmentChatId = "-5141347518",
    [string] $RepoPath = "C:\tender_it"
)

$envPath = Join-Path $RepoPath ".env"
$lines = @(
    "# local secrets - do not commit"
    "TELEGRAM_BOT_TOKEN=$BotToken"
    "EQUIPMENT_TELEGRAM_CHAT_ID=$EquipmentChatId"
    "EQUIPMENT_TELEGRAM_BOT_TOKEN=$BotToken"
)
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($envPath, $lines, $utf8)
Write-Host "Written: $envPath"
