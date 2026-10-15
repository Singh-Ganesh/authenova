@echo off
title Authenova
cd /d "%~dp0"
where py >nul 2>nul && (py -3 server.py & goto :end)
where python >nul 2>nul && (python server.py & goto :end)
echo Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
:end
pause
