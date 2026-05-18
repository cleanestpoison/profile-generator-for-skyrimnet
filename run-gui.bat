@echo off
REM Launch the dialogue-analyser GUI. Double-click to run.
setlocal
cd /d "%~dp0"
python scripts\gui.py
if errorlevel 1 pause
endlocal
