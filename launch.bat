@echo off
cd /d "%~dp0"

rem ── First-run folder wizard ─────────────────────────────────────────────────
rem Writes a .env file so Docker uses real folders on YOUR computer instead of
rem hidden ones inside the container. Re-run with: launch.bat --reconfigure

if "%~1"=="--reconfigure" goto :wizard
if not exist .env goto :wizard
goto :launch

:wizard
echo.
echo -- First-time setup ------------------------------------------
echo Pick the folders the app should use on this computer.
echo Press Enter to accept the suggested folder shown in brackets.
echo.

set "INTAKE_PATH="
set /p INTAKE_PATH="1) Receipts drop folder [%CD%\intake]: "
if "%INTAKE_PATH%"=="" set "INTAKE_PATH=%CD%\intake"

set "OUTPUT_PATH="
set /p OUTPUT_PATH="2) Reports folder [%CD%\output]: "
if "%OUTPUT_PATH%"=="" set "OUTPUT_PATH=%CD%\output"

echo 3) Auto-export folder - scheduled reports are copied here.
set "EXPORT_PATH="
set /p EXPORT_PATH="   Tip: pick a Dropbox/Drive/OneDrive folder [%CD%\export]: "
if "%EXPORT_PATH%"=="" set "EXPORT_PATH=%CD%\export"

echo 4) AI model - where should the model that reads receipts run?
echo    Bundled: ship a local model INSIDE Docker (offline, ~2-3 GB image).
echo    Lite:    use an LM Studio on this computer, or OpenRouter (set up later).
set "BUNDLE_LLM="
set /p BUNDLE_LLM="   Bundle a local AI model? [y/N]: "

if not exist "%INTAKE_PATH%" mkdir "%INTAKE_PATH%"
if not exist "%OUTPUT_PATH%" mkdir "%OUTPUT_PATH%"
if not exist "%EXPORT_PATH%" mkdir "%EXPORT_PATH%"

(
    echo INTAKE_PATH=%INTAKE_PATH%
    echo OUTPUT_PATH=%OUTPUT_PATH%
    echo EXPORT_PATH=%EXPORT_PATH%
) > .env
if /i "%BUNDLE_LLM:~0,1%"=="y" (
    (
        echo COMPOSE_FILE=docker-compose.yml:docker-compose.bundled.yml
        echo COMPOSE_PROFILES=bundled-llm
        echo LMSTUDIO_BASE_URL=http://model-server:1234/v1
    ) >> .env
    echo Selected the bundled variant ^(AI model^).
) else (
    (
        echo COMPOSE_FILE=docker-compose.yml:docker-compose.lite.yml
        echo LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
    ) >> .env
    echo Selected the lite variant ^(AI model^).
)
echo.
echo Saved to .env - re-run "launch.bat --reconfigure" to change these.
echo ---------------------------------------------------------------
echo.

:launch
echo Building and starting Receipt Processor...
docker compose up -d --build 2>nul
if errorlevel 1 docker-compose up -d --build
if errorlevel 1 (
    echo Docker failed to start.
    exit /b 1
)

echo Waiting for server to be ready...
set TRIES=0
:waitloop
set /a TRIES+=1
if %TRIES% geq 45 (
    echo Server did not respond. Open http://localhost:8000 in your browser.
    goto :done
)
curl -sf http://localhost:8000 >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto waitloop
)

echo Server is up -- opening browser...
start http://localhost:8000
:done
