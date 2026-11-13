@echo off
setlocal
cd /d "%~dp0"
title CyberAttend Launcher
:menu
echo.
echo =====================================
echo          CYBERATTEND LAUNCHER
echo =====================================
echo 1. Install / update Python requirements
echo 2. Run CyberAttend
echo 3. Open SMSGate setup instructions
echo 4. Exit
echo.
set /p choice="Choose an option: "
if "%choice%"=="1" goto install
if "%choice%"=="2" goto run
if "%choice%"=="3" goto sms
if "%choice%"=="4" exit /b
goto menu
:install
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
pause
goto menu
:run
py app.py
pause
goto menu
:sms
echo SMSGate is a separate service and is not included in this ZIP.
echo Read README_START_HERE.txt for connection steps and required credentials.
pause
goto menu
