@echo off
rem Double-click to start the LIVE (real money) trading bot and the dashboard, each in its own window.
rem It needs LIVE_TRADING_ENABLED=yes in .env, and refuses to start while the paper bot runs (one INDmoney login).
rem If the dashboard is already running it is not started twice; the browser just opens on its Live view.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found in %CD%\.venv
    echo Run:  python -m venv .venv  and  .venv\Scripts\pip install -e ".[dev,dashboard]"
    pause
    exit /b 1
)
if not exist ".env" (
    echo .env not found. Copy .env.example to .env and fill it in first.
    pause
    exit /b 1
)

start "LIVE Trading Bot (real money)" cmd /k ".venv\Scripts\python.exe -m live_trader.runner"
echo Started the LIVE bot. Watch Telegram for "LIVE bot started".

netstat -ano | findstr /R /C:":8501 .*LISTENING" >nul
if errorlevel 1 (
    start "Trading Dashboard" cmd /k ".venv\Scripts\python.exe -m streamlit run paper_trader\dashboard.py --server.headless true"
    echo Started the dashboard.
    timeout /t 8 >nul
) else (
    echo The dashboard is already running.
)
start "" "http://localhost:8501/?view=live"
echo The dashboard opens at http://localhost:8501/?view=live
timeout /t 5 >nul
