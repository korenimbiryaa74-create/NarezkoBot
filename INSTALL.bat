@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo =========================================================
echo  EXPERIMENTAL SETUP - application is not validated yet
echo  Uses installed Python 3.15 x64. Downloads several GB.
echo =========================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0desktop\install.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Installation failed. Review the messages above.
pause
exit /b %RC%
