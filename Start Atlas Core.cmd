@echo off
setlocal
cd /d "%~dp0"
py -3.12 scripts\run_operator.py
endlocal
