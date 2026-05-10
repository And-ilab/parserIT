@echo off
cd /d C:\tender_it
"C:\Users\Администратор\AppData\Local\Programs\Python\Python314\python.exe" it_parser.py > C:\tender_it\scheduler_log.txt 2>&1
exit /b 0