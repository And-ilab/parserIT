@echo off
REM Без видимого окна. Лог: angar_parser_log.txt и logs/.
cd /d "%~dp0.." || exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0win-angar-parser-quiet.ps1"
exit /b %ERRORLEVEL%
