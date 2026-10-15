@echo off
REM ==========================================================================
REM  StockWarden Futures/Options scraper - portable launcher
REM  100% ASCII ONLY. Do NOT put any Chinese / full-width chars anywhere in
REM  this file, including REM comments or echo lines. Reason: cmd.exe parses
REM  UTF-8 (no BOM) .bat files with Chinese characters INCORRECTLY even with
REM  chcp 65001, chopping lines mid-byte and producing garbage like:
REM    'startfile is not recognized' / 'Cannot initialize device PRN' / etc.
REM  All user-facing Chinese prompts are printed by the Python program.
REM ==========================================================================
chcp 65001 >nul

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        set "PY=py -3"
    ) else (
        echo.
        echo [ERROR] Python not detected. Install Python 3.10+
        echo         https://www.python.org/downloads/
        echo         (Check "Add Python to PATH" during install)
        echo.
        pause
        exit /b 1
    )
)

REM ---- syntax check ---------------------------------------------------------
%PY% -m py_compile stockwarden_futures_options_scraper.py
if not %ERRORLEVEL%==0 (
    echo.
    echo [ERROR] Syntax check FAILED - stockwarden_futures_options_scraper.py
    pause
    exit /b 2
)
%PY% -m py_compile _open_browser.py
if not %ERRORLEVEL%==0 (
    echo.
    echo [ERROR] Syntax check FAILED - _open_browser.py
    pause
    exit /b 2
)

REM ---- run main program ----------------------------------------------------
echo.
echo [RUN] %PY% stockwarden_futures_options_scraper.py
echo       working directory: %~dp0
echo -------------------------------------------------------------------

%PY% stockwarden_futures_options_scraper.py
set EXITCODE=%ERRORLEVEL%

echo -------------------------------------------------------------------
if not %EXITCODE%==0 goto FAIL_EXIT

echo [DONE] Exit code: %EXITCODE%
echo(
echo IMPORTANT:
echo   - Browser popup is launched by Python (4 fallbacks).
echo   - Line printed just above starts with "[開啟捷徑]" - double-click
echo     that file if no browser window appears.
echo(
for %%f in ("output\LATEST_*.html") do (
    echo   Fixed launcher path:
    echo     %%~ff
    goto AFTER_LAUNCHER
)
:AFTER_LAUNCHER
echo -------------------------------------------------------------------
exit /b 0

:FAIL_EXIT
echo [FAIL] Exit code: %EXITCODE%
echo -------------------------------------------------------------------
REM  失敗才暫停讓你看錯誤碼；成功直接關閉
pause
exit /b %EXITCODE%
