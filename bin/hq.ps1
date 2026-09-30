#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$HqHome = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = if ($env:HQ_PYTHON) { $env:HQ_PYTHON } else { 'py' }
$pythonArgs = if ($env:HQ_PYTHON) { @() } else { @('-3') }
& $python @pythonArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'hq: Python 3.11+ is required' }
$env:PYTHONPATH = "$HqHome" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { '' })
& $python @pythonArgs -m hqlib @args
exit $LASTEXITCODE
