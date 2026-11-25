@echo off
chcp 65001 >nul
rem Write the stop marker BEFORE killing, so a surviving watcher stays down.
set "DIR=%LOCALAPPDATA%\鸣潮宝箱助手CDP"
if not exist "%DIR%" mkdir "%DIR%"
echo stopped > "%DIR%\engine.stopped"
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
