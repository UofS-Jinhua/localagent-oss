@echo off
rem Double-click this to set up LocalAgent (virtualenv, dependencies, shortcut).
rem It passes any arguments through, e.g.:  setup.cmd -BaseUrl http://192.168.1.20:1234 -Dev
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup failed. Read the message above; you can run it again after fixing it.
)
echo.
pause
