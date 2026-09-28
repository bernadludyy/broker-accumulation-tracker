@echo off

chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PROJECT=C:\broker_accumulation_tracker\broker_accumulation_tracker"
set "PYTHON=C:\broker_accumulation_tracker\.venv\Scripts\python.exe"
set "SCRIPT=%PROJECT%\daily_runner.py"

cd /d "%PROJECT%"

if not exist "%PROJECT%\logs" mkdir "%PROJECT%\logs"

echo ================================================== >> "%PROJECT%\logs\task_scheduler.log"
echo START %date% %time% >> "%PROJECT%\logs\task_scheduler.log"

"%PYTHON%" "%SCRIPT%" >> "%PROJECT%\logs\task_scheduler.log" 2>&1

set "EXITCODE=%ERRORLEVEL%"

echo EXIT CODE: %EXITCODE% >> "%PROJECT%\logs\task_scheduler.log"
echo END %date% %time% >> "%PROJECT%\logs\task_scheduler.log"

exit /b %EXITCODE%