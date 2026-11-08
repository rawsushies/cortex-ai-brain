@echo off
setlocal
title Cortex
cd /d "%~dp0"

rem Find a working Python 3.10+ (python.org installs "py"; the Store alias for "python" may be a stub)
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Cortex needs Python 3.10 or newer.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b 1
)

rem First run: create the vault and connect the AI apps
if not exist "%USERPROFILE%\.config\cortex\config.json" (
  echo.
  echo  Welcome to Cortex. Where should your brain live?
  echo  Press Enter for the default: %USERPROFILE%\Documents\Cortex
  echo  ^(or paste the path of an existing Obsidian vault^)
  echo.
  set /p "VAULT=Folder: "
  call :setup
)

%PY% run.py app
pause
exit /b

:setup
if defined VAULT (%PY% run.py init "%VAULT%") else (%PY% run.py init)
echo.
%PY% run.py connect
echo.
exit /b
