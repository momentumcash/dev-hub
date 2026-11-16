@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 server.py
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Dev Hub needs Python 3.8 or newer. Install Python, then double-click again.
  ) else (
    python server.py
  )
)
pause
