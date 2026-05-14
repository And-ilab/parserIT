# Однократный запуск angar_parser.py: окно процесса Python скрыто; вывод сохранён в angar_parser_log.txt.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $Repo
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'angar_parser.py'))) { throw "angar_parser.py не найден: $Repo" }
$py = if ($env:PARSER_PYTHON) { $env:PARSER_PYTHON } else { 'python.exe' }
$exe = Get-Command $py | Select-Object -ExpandProperty Source
$p = Start-Process -FilePath $exe -WorkingDirectory $Repo `
    -ArgumentList @('angar_parser.py') `
    -WindowStyle Hidden -Wait -PassThru
exit [int]$p.ExitCode
