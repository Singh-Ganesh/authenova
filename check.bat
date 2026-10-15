@echo off
title Authenova API check
cd /d "%~dp0"
where py >nul 2>nul && (py -3 check_apis.py & goto :end)
where python >nul 2>nul && (python check_apis.py & goto :end)
echo Python 3 was not found.
:end
pause
