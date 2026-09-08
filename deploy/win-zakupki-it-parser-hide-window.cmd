@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0win-zakupki-it-parser-quiet.ps1"
exit /b %ERRORLEVEL%
