#!/usr/bin/env bash
set -e

echo "Building and starting Receipt Processor…"
# Support both newer 'docker compose' and legacy 'docker-compose'
if docker compose version &>/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

echo "Waiting for server to be ready…"
MAX_TRIES=45   # ~90 seconds
TRIES=0
until curl -sf http://localhost:8000 > /dev/null 2>&1; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -ge "$MAX_TRIES" ]; then
        echo "Server did not respond after ${MAX_TRIES} attempts."
        echo "Open http://localhost:8000 manually once it's up."
        exit 1
    fi
    sleep 2
done

echo "Server is up — opening browser…"
open http://localhost:8000
