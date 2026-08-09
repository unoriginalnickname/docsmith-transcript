@echo off
rem Double-click this to start transkrp and open it in your browser.
rem
rem `cd /d "%~dp0"` first, because double-clicking can start you anywhere and
rem every path below is relative to this file rather than to wherever Explorer
rem happened to be. `%~dp0` is this script's own folder, with a trailing slash.
title transkrp

cd /d "%~dp0"

rem `py` is the Windows launcher and picks a sane interpreter even when several
rem are installed; plain `python` on PATH may be a Store stub that does nothing.
rem Fall back to it only when `py` is genuinely absent.
py -3 --version >nul 2>nul
if %errorlevel%==0 (
  py -3 transkrp.py --serve -o "%~dp0notes"
) else (
  python transkrp.py --serve -o "%~dp0notes"
)

echo.
if %errorlevel% neq 0 (
  echo transkrp could not start. The error is above.
  echo If it says Python was not found, install Python 3.10 or newer.
) else (
  echo transkrp has stopped. Double-click this file again to restart it.
)
pause
