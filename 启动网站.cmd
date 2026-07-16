@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Please run the installation steps in README.md first.
  pause
  exit /b 1
)
start "ArbiScope" http://127.0.0.1:5000/
echo ArbiScope is running at http://127.0.0.1:5000/
echo Keep this window open while using the site. Press Ctrl+C to stop it.
.venv\Scripts\python.exe app.py
pause
