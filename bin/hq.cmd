@echo off
rem Windows launcher for henry-hq.  PATHEXT lets users type `hq`.
setlocal
set "PYTHONUTF8=1"
if defined HQ_PYTHON (
  "%HQ_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto custom
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto pylauncher
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto python
>&2 echo hq: Python 3.11+ is required
if /i "%~1"=="gate" (
  echo {}
  exit /b 0
)
if /i "%~1"=="auto-default" (
  echo {}
  exit /b 0
)
exit /b 1
:custom
"%HQ_PYTHON%" "%~dp0hq.py" %*
exit /b %errorlevel%
:pylauncher
py -3 "%~dp0hq.py" %*
exit /b %errorlevel%
:python
python "%~dp0hq.py" %*
exit /b %errorlevel%
