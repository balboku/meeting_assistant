@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Please install the desktop requirements first. See README.md.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m gui.desktop_recorder
