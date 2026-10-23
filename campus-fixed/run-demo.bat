@echo off
rem One-click local demo (Windows): venv -> install deps -> start in demo mode on 127.0.0.1 only.
rem Usage: run-demo.bat [port, default 8000] [--synthetic]
rem Default data is a private copy of the 2026-10-09 historical public sample; --synthetic uses labelled fake data.
rem The collector is OFF by default: no external sites are fetched (first run needs PyPI to install deps).
setlocal
cd /d "%~dp0"
set "PORT=8000"
set "EXTRA="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--synthetic" (set "EXTRA=--synthetic") else (set "PORT=%~1")
shift
goto parse
:parsed
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo ^>^> Creating virtual environment .venv
  where python >nul 2>nul || (echo Please install Python 3.12+ and add it to PATH. & exit /b 1)
  python -m venv .venv || (echo Failed to create venv. & exit /b 1)
)
if not exist ".venv\.demo-deps-installed" (
  echo ^>^> Installing dependencies
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (echo Dependency install failed. & exit /b 1)
  echo ok> ".venv\.demo-deps-installed"
)
".venv\Scripts\python.exe" demo_server.py --port %PORT% %EXTRA%
endlocal
