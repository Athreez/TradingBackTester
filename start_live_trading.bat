@echo off
rem Double-click to start the LIVE (real money) trading bot in its own window.
rem It needs LIVE_TRADING_ENABLED=yes in .env, and refuses to start while the paper bot runs (one INDmoney login).
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
timeout /t 5 >nul
