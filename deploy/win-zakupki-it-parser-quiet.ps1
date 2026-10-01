# Однократный запуск zakupki_it_parser.py: окно скрыто; лог zakupki_it_parser_log.txt.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $Repo
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'zakupki_it_parser.py'))) {
    throw "zakupki_it_parser.py не найден: $Repo"
}
$py = if ($env:PARSER_PYTHON) { $env:PARSER_PYTHON } else { 'python.exe' }
$exe = Get-Command $py | Select-Object -ExpandProperty Source
$p = Start-Process -FilePath $exe -WorkingDirectory $Repo `
    -ArgumentList @('zakupki_it_parser.py') `
    -WindowStyle Hidden -Wait -PassThru
exit [int]$p.ExitCode
