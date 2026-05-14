# Однократный запуск it_parser.py: окно процесса Python скрыто; вывод сохранён в it_parser_log.txt у корня репо.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $Repo
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'it_parser.py'))) { throw "it_parser.py не найден: $Repo" }
$py = if ($env:PARSER_PYTHON) { $env:PARSER_PYTHON } else { 'python.exe' }
$exe = Get-Command $py | Select-Object -ExpandProperty Source
$p = Start-Process -FilePath $exe -WorkingDirectory $Repo `
    -ArgumentList @('it_parser.py') `
    -WindowStyle Hidden -Wait -PassThru
exit [int]$p.ExitCode
