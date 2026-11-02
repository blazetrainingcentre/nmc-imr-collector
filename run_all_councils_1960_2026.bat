@echo off
setlocal

if /i "%~1"=="--help" goto help

pushd "%~dp0"
if errorlevel 1 (
    echo Could not open the collector folder.
    pause
    exit /b 1
)

set "IMR_BATCH_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "IMR_BATCH_PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python was not found. Install Python or create the project .venv first.
        popd
        pause
        exit /b 1
    )
)

echo Collecting every available council for years 1960 through 2026.
echo These are API information years, not registration-date years.
echo Each council runs its entire 1960-2026 year range before the next council.
echo Completed batches are skipped; capped batches get recovery searches.
echo Progress and data use the collector's configured local data folder.
echo Press Ctrl+C to stop. Run this file again to resume.
echo.

"%IMR_BATCH_PYTHON%" -u scrape.py batch --year-from 1960 --year-to 2026 --recover-capped %*
set "IMR_BATCH_RESULT=%ERRORLEVEL%"

echo.
echo Collector stopped with exit code %IMR_BATCH_RESULT%.
echo Check the progress summary for incomplete or capped batches:
"%IMR_BATCH_PYTHON%" scrape.py status
echo.
echo Rerun this file to resume unfinished work.
popd
pause
exit /b %IMR_BATCH_RESULT%

:help
echo Usage: run_all_councils_1960_2026.bat [scrape options]
echo.
echo Collects all available councils for offered years within 1960-2026,
echo using API information years rather than registration-date years,
echo one explicitly selected council at a time across its full year range,
echo with capped-batch recovery and saved-progress resume enabled.
echo Uses .venv\Scripts\python.exe when available, otherwise python on PATH.
echo.
echo Examples:
echo   run_all_councils_1960_2026.bat
echo   run_all_councils_1960_2026.bat --dry-run
echo   run_all_councils_1960_2026.bat --delay 3
exit /b 0
