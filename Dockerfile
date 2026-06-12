FROM python:3.12-slim
WORKDIR /app

# Runtime libs required by paddlepaddle/paddleocr (CPU)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download PaddleOCR models so the first fallback isn't delayed by a
# download at runtime.  The helper script applies the same PaddlePredictorOption
# compat shim used at runtime so the init succeeds even when paddlepaddle and
# paddleocr minor versions diverge.
COPY _paddle_preload.py .
RUN python _paddle_preload.py || true

COPY . .

# Persistent data directories for volume mounts
RUN mkdir -p /data/intake /data/output /data/export /data/processing \
    /data/failed /data/watch_inbox /data/watch_state \
    && chmod -R 777 /data

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000')" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
