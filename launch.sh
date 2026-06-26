#!/usr/bin/env bash

cd "$(dirname "$0")"

# ── First-run folder wizard ────────────────────────────────────────────────────
# Writes a .env file so Docker uses real folders on YOUR computer instead of
# hidden ones inside the container. Re-run any time with: ./launch.sh --reconfigure

if [ ! -f .env ] || [ "$1" = "--reconfigure" ]; then
    echo ""
    echo "── First-time setup ──────────────────────────────────────────"
    echo "Pick the folders the app should use on this computer."
    echo "Press Enter to accept the suggested folder shown in brackets."
    echo ""

    read -e -r -p "1) Receipts drop folder — put receipt photos here [$PWD/intake]: " INTAKE_PATH
    INTAKE_PATH="${INTAKE_PATH:-$PWD/intake}"

    read -e -r -p "2) Reports folder — spreadsheets are saved here [$PWD/output]: " OUTPUT_PATH
    OUTPUT_PATH="${OUTPUT_PATH:-$PWD/output}"

    echo "3) Auto-export folder — scheduled reports are copied here."
    read -e -r -p "   Tip: choose a Dropbox/Drive/OneDrive folder for automatic cloud upload [$PWD/export]: " EXPORT_PATH
    EXPORT_PATH="${EXPORT_PATH:-$PWD/export}"

    echo ""
    echo "4) AI model — where should the model that reads receipts run?"
    echo "   • Bundled: ship a local model INSIDE Docker (offline, ~2-3 GB image)."
    echo "   • Lite:    use an LM Studio on this computer, or OpenRouter (set up later)."
    read -e -r -p "   Bundle a local AI model? [y/N]: " BUNDLE_LLM
    case "$BUNDLE_LLM" in
        [Yy]*) VARIANT="bundled" ;;
        *)     VARIANT="lite" ;;
    esac

    mkdir -p "$INTAKE_PATH" "$OUTPUT_PATH" "$EXPORT_PATH"
    cat > .env <<EOF
INTAKE_PATH=$INTAKE_PATH
OUTPUT_PATH=$OUTPUT_PATH
EXPORT_PATH=$EXPORT_PATH
EOF
    if [ "$VARIANT" = "bundled" ]; then
        cat >> .env <<EOF
# Bundled-LLM variant — see .env.bundled.example
COMPOSE_FILE=docker-compose.yml:docker-compose.bundled.yml
COMPOSE_PROFILES=bundled-llm
LMSTUDIO_BASE_URL=http://model-server:1234/v1
EOF
    else
        cat >> .env <<EOF
# Lite variant (no bundled model) — see .env.lite.example
COMPOSE_FILE=docker-compose.yml:docker-compose.lite.yml
LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
EOF
    fi
    echo ""
    echo "Selected the ${VARIANT} variant (AI model)."
    echo "Saved to .env — re-run './launch.sh --reconfigure' to change these."
    echo "──────────────────────────────────────────────────────────────"
    echo ""
fi

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
