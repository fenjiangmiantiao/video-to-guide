@echo off
chcp 65001 >nul
rem ============================================================
rem  Video to Guide - launch the desktop GUI (source version)
rem  Double-click this file. No console window stays open.
rem ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m video_to_guide
    exit /b 0
)

echo [ERROR] .venv not found in this folder.
echo Run the setup steps in docs\usage.md first.
pause
