@echo off
title Authenova setup
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
  exit /b 1
)
echo Installing the claim engine models (SBERT + DeBERTa). This downloads about 1 GB the first time...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements-local.txt
echo.
echo Done. Now double-click run.bat
pause
