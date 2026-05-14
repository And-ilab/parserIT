@echo off
REM Обычное окно: закроется само после выполнения.
cd /d "%~dp0.." || exit /b 1
python angar_parser.py
exit /b %ERRORLEVEL%
