# Loads .env into current PowerShell session (manual python runs).
param([string] $RepoPath = "C:\tender_it")
$envFile = Join-Path $RepoPath ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Error "Missing $envFile - run deploy\setup-server-env.ps1 first"
    exit 1
}
Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $name = $line.Substring(0, $i).Trim()
    $val = $line.Substring($i + 1).Trim()
    Set-Item -Path "Env:$name" -Value $val
}
Write-Host "Loaded variables from .env"
