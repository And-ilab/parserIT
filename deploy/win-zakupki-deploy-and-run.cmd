@echo off
cd /d "%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0win-zakupki-deploy-and-run.ps1"
exit /b %ERRORLEVEL%
