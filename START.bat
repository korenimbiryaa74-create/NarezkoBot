@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0desktop\.venv\Scripts\pythonw.exe" (
  echo Run INSTALL.bat first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0desktop\launch.ps1"
if errorlevel 1 pause
