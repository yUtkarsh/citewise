@echo off
rem One-time setup: Python venv, packages, .env, FalkorDB image.
setlocal
cd /d "%~dp0"

echo == Code Graph Agent: install ==

rem --- Python 3.11+: first candidate that is new enough ---
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "python" "py -3") do (
    if not defined PY call :try_python %%~C
)
if not defined PY (
    echo Python 3.11+ was not found. Install it ^(winget install Python.Python.3.12^) and re-run.
    goto :fail
)
echo Using:
%PY% --version

rem --- Virtual environment ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)

echo Installing Python packages ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

rem --- .env ---
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example. Set ANTHROPIC_API_KEY in .env before launching.
) else (
    echo .env already exists; left unchanged.
)

rem --- FalkorDB image (optional here; launch.bat starts it) ---
where docker >nul 2>nul
if errorlevel 1 (
    echo Docker was not found. Install Docker Desktop before running launch.bat.
) else (
    docker info >nul 2>nul
    if errorlevel 1 (
        echo Docker Desktop is not running; the FalkorDB image will be pulled on first launch.
    ) else (
        echo Pulling the FalkorDB image ...
        docker compose pull
    )
)

echo.
echo Install finished. Run launch.bat to start FalkorDB and the app.
pause
exit /b 0

:fail
echo.
echo Install failed.
pause
exit /b 1

:try_python
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%*"
exit /b 0
