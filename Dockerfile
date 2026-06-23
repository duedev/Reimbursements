FROM python:3.12-slim
WORKDIR /app

# Runtime libs for the local OCR stack (RapidOCR → onnxruntime + opencv, CPU):
# libgomp1 (OpenMP, onnxruntime), libgl1 + libglib2.0-0 (OpenCV image I/O).
# wkhtmltopdf bundles wkhtmltoimage (patched-Qt, runs headless) so emailed HTML
# e-receipts render to a faithful JPEG "copy of the receipt" (see
# process_receipts.render_receipt_copy); without it the app still produces a
# pure-Python text-image copy, so this is a fidelity upgrade, not a hard dep.
# wkhtmltopdf was removed from Debian Bookworm repos; fetch the .deb from the
# upstream packaging releases (supports amd64 and arm64).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 \
        wget ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && wget -q \
        "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb" \
        -O /tmp/wkhtmltox.deb \
    && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
    && rm /tmp/wkhtmltox.deb \
    && apt-get purge -y --auto-remove wget \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged account the app actually runs as (see docker-entrypoint.py).
RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Smoke-init RapidOCR at build time. Its PP-OCR ONNX models are bundled in the
# wheel, so this needs no network and just warms/validates the engine. Run as
# appuser so any per-user cache lands in its HOME. Best-effort (`|| true`) so the
# build can't fail here, but it prints a loud warning if the engine can't be
# constructed (a missing/broken dependency) instead of failing silently at runtime.
USER appuser
RUN python - <<'PYEOF' || true
try:
    from rapidocr_onnxruntime import RapidOCR
    RapidOCR()
    print('[build] RapidOCR engine initialised OK')
except Exception as exc:  # noqa: BLE001 - surface, don't fail the build
    print(f'[build] WARNING: RapidOCR init failed ({type(exc).__name__}: {exc}). '
          'The image still builds, but the local OCR fallback will be unavailable '
          'until this is resolved.')
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
