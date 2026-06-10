@echo off
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
