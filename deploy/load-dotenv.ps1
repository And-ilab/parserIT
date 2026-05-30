# Подставляет переменные из .env в текущую сессию PowerShell (для ручного python it_parser.py и т.д.).
param([string] $RepoPath = "C:\tender_it")
$envFile = Join-Path $RepoPath ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Error "Нет файла $envFile — сначала deploy\setup-server-env.ps1"
    exit 1
}
Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $name = $line.Substring(0, $i).Trim()
    $val = $line.Substring($i + 1).Trim()
    Set-Item -Path "Env:$name" -Value $val
}
Write-Host "Переменные из .env загружены."
