FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Persistent data directories for volume mounts
RUN mkdir -p /data/receipts /data/output /data/watch_inbox /data/watch_staged /data/watch_state \
    && chmod -R 777 /data

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000')" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
