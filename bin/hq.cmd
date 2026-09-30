@echo off
rem Windows launcher for henry-hq.  PATHEXT lets users type `hq`.
setlocal
set "HQ_HOME=%~dp0.."
set "HQ_PY="
if defined HQ_PYTHON (
  "%HQ_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "HQ_PY=%HQ_PYTHON%"
    goto run
  )
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "HQ_PY=py -3"
  goto run
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "HQ_PY=python"
  goto run
)
>&2 echo hq: Python 3.11+ is required
if /i "%~1"=="gate" echo {}
exit /b 0
:run
set "PYTHONPATH=%HQ_HOME%;%PYTHONPATH%"
if defined HQ_PYTHON if /i "%HQ_PY%"=="%HQ_PYTHON%" (
  "%HQ_PYTHON%" -m hqlib %*
  exit /b %errorlevel%
)
%HQ_PY% -m hqlib %*
exit /b %errorlevel%
