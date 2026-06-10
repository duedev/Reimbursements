#!/usr/bin/env bash

echo "Building and starting Receipt Processor…"
if docker compose version &>/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

if [ $? -ne 0 ]; then
    echo "Docker failed to start. Check the error above."
    exit 1
fi

echo "Waiting for server to be ready…"
MAX_TRIES=45   # ~90 seconds
TRIES=0
until curl -sf http://localhost:8000 > /dev/null 2>&1; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -ge "$MAX_TRIES" ]; then
        echo "Server did not respond after ${MAX_TRIES} attempts."
        echo "Open http://localhost:8000 in your browser once it starts."
        exit 0
    fi
    sleep 2
done

echo "Server is up — opening http://localhost:8000 …"
if command -v open &>/dev/null; then
    open http://localhost:8000              # macOS
elif command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:8000 &        # Linux desktop
elif command -v wslview &>/dev/null; then
    wslview http://localhost:8000           # WSL
else
    echo "  -> Open http://localhost:8000 in your browser."
fi
