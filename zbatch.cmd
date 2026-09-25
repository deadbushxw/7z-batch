@echo off
rem Launcher for zbatch. Double-click this file to start the tool.
rem
rem Two rules for this file:
rem   1) keep it pure ASCII - CJK or typographic punctuation in a .cmd gets
rem      mis-decoded under a mismatched OEM codepage and corrupts the parsing;
rem   2) keep CRLF line endings - LF-only batch files are parsed unreliably.
rem
rem The tool itself lives in the zbatch/ subfolder.
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto run
where py >nul 2>nul
if not errorlevel 1 set "PY=py"

:run
if not defined PY goto nopython
%PY% "%~dp0zbatch\zbatch.py" %*
set RC=%ERRORLEVEL%
if "%RC%"=="0" exit /b 0
echo.
echo [ERROR] zbatch exited with code %RC%
pause
exit /b %RC%

:nopython
echo [ERROR] Python not found in PATH. Install Python 3 first, then re-run.
pause
exit /b 1
