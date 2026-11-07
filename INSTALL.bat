@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo =========================================================
echo  EXPERIMENTAL SETUP - desktop application is NOT ready yet
echo  This prepares dependencies only. It will download several GB.
echo =========================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0desktop\install.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Installation failed. Review the messages above.
pause
exit /b %RC%
