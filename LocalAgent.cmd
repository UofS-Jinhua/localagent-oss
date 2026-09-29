@echo off
rem Double-click this to open the LocalAgent window (sets itself up the first time).
setlocal
set "REPO=%~dp0"
if not exist "%REPO%.venv\Scripts\pythonw.exe" (
  echo First run: setting up the virtualenv and dependencies...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%scripts\setup.ps1"
  if errorlevel 1 ( pause & exit /b 1 )
)
start "" "%REPO%.venv\Scripts\pythonw.exe" -m localagent.cli.main ui %*
