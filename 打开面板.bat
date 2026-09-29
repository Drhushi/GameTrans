@echo off
rem gametrans panel launcher: double-click me, or drop a game folder on me.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. & pause & exit /b 1)
python "%~dp0scripts\open_panel.py" %*
if errorlevel 1 pause
