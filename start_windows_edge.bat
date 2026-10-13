@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "CHAOXING_BROWSER=msedge"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Python dependencies installation failed.
    pause
    exit /b 1
)
python study_helper.py
pause
