@echo off
REM Без черного окна (скрытый процесс python). Полный текст — в it_parser_log.txt и logs/.
cd /d "%~dp0.." || exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0win-it-parser-quiet.ps1"
exit /b %ERRORLEVEL%
