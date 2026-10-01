@echo off
cd /d "%~dp0.."
python zakupki_it_parser.py
exit /b %ERRORLEVEL%
