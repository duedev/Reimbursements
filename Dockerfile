FROM python:3.12-slim
WORKDIR /app

# Runtime libs required by paddlepaddle/paddleocr (CPU)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download PaddleOCR models so the first fallback isn't delayed by a
# download at runtime.  requirements.txt pins paddlepaddle/paddleocr/paddlex
# to matching versions, so no compat shim is needed here.  Best-effort: a
# network-restricted build still succeeds, models then download on first use.
RUN python - <<'PYEOF' || true
from paddleocr import PaddleOCR
try:
    PaddleOCR(use_textline_orientation=True, lang='en')
except TypeError:  # PaddleOCR 2.x
    PaddleOCR(use_angle_cls=True, lang='en')
PYEOF

COPY . .

# Persistent data directories for volume mounts
RUN mkdir -p /data/intake /data/output /data/export /data/processing \
    /data/failed /data/watch_inbox /data/watch_state \
    && chmod -R 777 /data

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000')" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
