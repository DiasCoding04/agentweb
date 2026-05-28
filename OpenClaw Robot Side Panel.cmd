@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0OpenClaw Robot Side Panel.ps1"
if errorlevel 1 (
  echo.
  echo OpenClaw Robot Side Panel bi loi. Hay chup man hinh cua so nay gui lai.
  pause
)
