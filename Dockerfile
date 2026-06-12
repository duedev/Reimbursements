FROM python:3.12-slim
WORKDIR /app

# Runtime libs required by paddlepaddle/paddleocr (CPU)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download PaddleOCR models so the first fallback isn't delayed by a
# download at runtime.  Apply the same PaddlePredictorOption compat shim used
# at runtime (process_receipts._patch_paddle_predictor_option) so the init
# succeeds even when paddlepaddle and paddleocr minor versions diverge.
RUN python - <<'PYEOF' || true
import inspect

def _patch(mod, attr):
    orig = getattr(mod, attr, None)
    if not orig:
        return
    try:
        non_self = [n for n,p in inspect.signature(orig.__init__).parameters.items()
                    if n != 'self' and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    except Exception:
        non_self = ['?']
    if non_self:
        return
    class _C(orig):
        def __init__(self, *a, **kw): super().__init__()
    setattr(mod, attr, _C)

try:
    import paddle.inference as _pi; _patch(_pi, 'PaddlePredictorOption')
except Exception: pass
try:
    import paddleocr.utils.pp_option as _pp; _patch(_pp, 'PaddlePredictorOption')
except Exception: pass

from paddleocr import PaddleOCR
try:
    PaddleOCR(use_textline_orientation=True, lang='en')
except TypeError:
    try:
        PaddleOCR(use_textline_orientation=False, lang='en')
    except TypeError:
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
