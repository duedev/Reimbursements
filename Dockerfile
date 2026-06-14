FROM python:3.12-slim
WORKDIR /app

# Runtime libs required by paddlepaddle/paddleocr (CPU)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged account the app actually runs as (see docker-entrypoint.py).
RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download PaddleOCR models so the first fallback isn't delayed by a
# download at runtime.  requirements.txt pins paddlepaddle/paddleocr/paddlex
# to matching versions, so no compat shim is needed here.  Best-effort: a
# network-restricted build still succeeds, models then download on first use.
# Run as appuser so the models land in its HOME and are found at runtime.
#
# This stays best-effort (the `|| true`) so a network-restricted build can't
# fail here, but it now prints a loud warning when the engine can't even be
# constructed (e.g. a missing setuptools / dependency-drift problem) rather than
# swallowing it silently — that masked class of failure is why the runtime
# fallback kept breaking with no signal at build time.
USER appuser
RUN python - <<'PYEOF' || true
try:
    from paddleocr import PaddleOCR
    try:
        PaddleOCR(use_textline_orientation=True, lang='en')
    except TypeError:  # PaddleOCR 2.x
        PaddleOCR(use_angle_cls=True, lang='en')
    print('[build] PaddleOCR models pre-downloaded')
except Exception as exc:  # noqa: BLE001 - surface, don't fail the build
    print(f'[build] WARNING: PaddleOCR pre-init failed ({type(exc).__name__}: {exc}). '
          'The image still builds; if this is an import/dependency error (not a '
          'network/model-download error) the runtime OCR fallback will be unavailable.')
PYEOF
USER root

COPY . .

# Persistent data directories for volume mounts. Secrets live in /data/config
# (mounted to a NON cloud-synced volume — see docker-compose.yml) so the SMTP
# password / Dropbox token never sync to Dropbox/Drive with the output folder.
RUN mkdir -p /data/intake /data/output /data/export /data/processing \
        /data/failed /data/watch_inbox /data/watch_state /data/config \
    && chown -R appuser:appuser /data /app \
    && chmod -R 770 /data
ENV SECRETS_PATH=/data/config/secrets.json

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000')" || exit 1

# The entrypoint chowns the data dirs (incl. fresh bind mounts) then drops from
# root to appuser before exec-ing the command — so the app never runs as root.
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
