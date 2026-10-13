@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependencies installation failed.
    pause
    exit /b 1
)
python -m playwright install chromium
if errorlevel 1 (
    echo Chromium installation failed.
    pause
    exit /b 1
)
python study_helper.py
pause
