@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 pause & exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 pause & exit /b 1
start "Video Downloader" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py
pause
