@echo off
REM Обычное окно: закроется автоматически после завершения (без pause). Рабочий каталог = корень репозитория.
cd /d "%~dp0.." || exit /b 1
python it_parser.py
exit /b %ERRORLEVEL%
