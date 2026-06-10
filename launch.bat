@echo off
echo Building and starting Receipt Processor...
docker-compose up -d --build

echo Waiting for server to be ready...
:waitloop
curl -sf http://localhost:8000 >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto waitloop
)

echo Server is up -- opening browser...
start http://localhost:8000
