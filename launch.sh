#!/usr/bin/env bash
set -e

echo "Building and starting Receipt Processor…"
docker-compose up -d --build

echo "Waiting for server to be ready…"
until curl -sf http://localhost:8000 > /dev/null 2>&1; do
    sleep 2
done

echo "Server is up — opening browser…"
if command -v xdg-open &> /dev/null; then
    xdg-open http://localhost:8000
elif command -v open &> /dev/null; then
    open http://localhost:8000
else
    echo "Open http://localhost:8000 in your browser."
fi
