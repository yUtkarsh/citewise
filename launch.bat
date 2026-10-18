@echo off
rem Start FalkorDB (Docker) and the web app on http://127.0.0.1:8000/
rem Optional: set APP_PORT to use another port, NO_BROWSER=1 to skip opening a browser.
setlocal
cd /d "%~dp0"

if not defined APP_PORT set "APP_PORT=8000"
set "APP_URL=http://127.0.0.1:%APP_PORT%/"

if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Run install.bat first.
    goto :fail
)
if not exist ".env" (
    echo .env not found. Run install.bat first, then set ANTHROPIC_API_KEY in .env.
    goto :fail
)

rem --- Docker Desktop: start it if it is installed but not running ---
where docker >nul 2>nul
if errorlevel 1 (
    echo Docker was not found. Install Docker Desktop.
    goto :fail
)
docker info >nul 2>nul
if not errorlevel 1 goto :docker_ready
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    echo Starting Docker Desktop ...
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
)
echo Waiting for Docker ...
set /a TRIES=0
:wait_docker
timeout /t 3 /nobreak >nul
docker info >nul 2>nul
if not errorlevel 1 goto :docker_ready
set /a TRIES+=1
if %TRIES% lss 40 goto :wait_docker
echo Docker did not start. Start Docker Desktop and re-run launch.bat.
goto :fail

:docker_ready
echo Starting FalkorDB (browser UI on http://localhost:3000) ...
docker compose up -d
if errorlevel 1 goto :fail

rem --- Open the browser once the server has had a moment to start ---
if not defined NO_BROWSER start "" /min cmd /c "timeout /t 5 /nobreak >nul & start "" %APP_URL%"

echo Starting the app on %APP_URL%  (Ctrl+C to stop)
".venv\Scripts\python.exe" -m uvicorn app.api.main:app --host 127.0.0.1 --port %APP_PORT%
exit /b %errorlevel%

:fail
echo.
pause
exit /b 1
