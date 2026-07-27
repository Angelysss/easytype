@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [EasyType] Missing uv. Install it from https://docs.astral.sh/uv/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo [EasyType] Installing dependencies for the first run...
  uv sync
  if errorlevel 1 (
    pause
    exit /b 1
  )
)

start "" ".venv\Scripts\pythonw.exe" ".\main.py"
endlocal
