@echo off
cd /d "%~dp0"
title Camera Console

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] First run: creating venv, please wait 1-2 minutes ...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check opencv-python flask requests
)

echo [2/2] Starting ...
".venv\Scripts\python.exe" camera_console.py

pause
