@echo off
rem Double-click to start the paper trading bot and its dashboard, each in its own window.
rem Close a window (or press Ctrl+C in it) to stop that part. The bot refuses to start twice.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found in %CD%\.venv
    echo Run:  python -m venv .venv  and  .venv\Scripts\pip install -e ".[dev,dashboard]"
    pause
    exit /b 1
)
if not exist "paper_trader\.env" (
    echo paper_trader\.env not found. Copy paper_trader\.env.example to paper_trader\.env first.
    pause
    exit /b 1
)

start "Paper Trading Bot" cmd /k ".venv\Scripts\python.exe -m paper_trader.runner"
start "Paper Trading Dashboard" cmd /k ".venv\Scripts\python.exe -m streamlit run paper_trader\dashboard.py"

echo Started the bot and the dashboard in separate windows.
echo The dashboard opens at http://localhost:8501 (give it a few seconds).
timeout /t 5 >nul
