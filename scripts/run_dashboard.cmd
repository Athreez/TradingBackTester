@echo off
rem Opens the paper trading dashboard at http://localhost:8501
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m streamlit run paper_trader\dashboard.py
