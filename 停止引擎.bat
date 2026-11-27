@echo off
setlocal
rem Stop the watcher and the engine.
rem
rem Needs elevation: once the watcher runs elevated (see the .vbs launcher),
rem a normal-privilege taskkill cannot terminate it. This script re-launches
rem itself elevated if needed, then does the killing.

rem ---- elevation gate ------------------------------------------------------
rem "net session" only succeeds for administrators. That is the cheapest
rem reliable elevation test available from a batch file.
net session >nul 2>&1
if %errorlevel%==0 goto :elevated

rem Not elevated: ask UAC for an elevated copy of this script, then quit.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
if %errorlevel%==0 (
    echo Requested administrator rights in a new window...
    timeout /t 2 >nul
    exit /b 0
)
echo Could not get administrator rights. Trying anyway - may not be able to
echo stop an elevated engine.
echo.

:elevated

rem Write the stop marker BEFORE killing anything, so a watcher that survives
rem the taskkill does not immediately pull the engine back up.
rem The data folder name contains Chinese characters; match it by its "CDP"
rem suffix instead, so this file can stay pure ASCII.
set "DIR="
for /d %%d in ("%LOCALAPPDATA%\*CDP") do set "DIR=%%d"
if not defined DIR set "DIR=%LOCALAPPDATA%\CDP"
if not exist "%DIR%" mkdir "%DIR%"
echo stopped > "%DIR%\engine.stopped"
echo Marker written: %DIR%\engine.stopped
echo.

rem Watcher first (port 9224), then the engine (port 9223).
set "wfound=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":9224" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
    set "wfound=1"
)
set "efound=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":9223" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
    set "efound=1"
)

if "%wfound%"=="1" (echo Watcher stopped.) else (echo Watcher was not running.)
if "%efound%"=="1" (echo Engine stopped.) else (echo Engine was not running.)
pause
