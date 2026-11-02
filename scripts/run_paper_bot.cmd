@echo off
rem Starts today's paper trading session. Used by the Windows scheduled task
rem "TradingBackTester Paper Bot" (weekdays 09:00); safe to run by hand too.
cd /d "%~dp0.."
if not exist "paper_trader\logs" mkdir "paper_trader\logs"
".venv\Scripts\python.exe" -m paper_trader.runner --once >> "paper_trader\logs\console.log" 2>&1
