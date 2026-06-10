#!/usr/bin/env python3
"""server.py — FastAPI web frontend for the receipt processor."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from process_receipts import initialize_models, process_receipts_batch

TMP_ROOT = Path("tmp")
TMP_ROOT.mkdir(exist_ok=True)

# job_id → {"queue": Queue, "done": bool, "output_path": Path|None, "cancel": Event}
_jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load olmOCR-2 (or fall back to Gemma) in a background thread so the
    # server is immediately available while the model loads.
    threading.Thread(target=initialize_models, daemon=True).start()
    yield


app = FastAPI(title="Receipt Processor", lifespan=lifespan)


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse("templates/index.html", media_type="text/html")


@app.post("/process")
async def process(
    files: list[UploadFile] = File(...),
    employee: str = Form("Employee"),
    job_name: str = Form(""),
    job_number: str = Form(""),
):
    job_id = str(uuid.uuid4())
    receipts_dir = TMP_ROOT / job_id / "receipts"
    receipts_dir.mkdir(parents=True)
    out_dir = TMP_ROOT / job_id / "output"
    out_dir.mkdir()

    for f in files:
        dest = receipts_dir / (f.filename or "receipt")
        with open(dest, "wb") as fh:
            fh.write(await f.read())

    cancel = threading.Event()
    q: Queue = Queue()
    _jobs[job_id] = {"queue": q, "done": False, "output_path": None, "cancel": cancel}

    def run():
        def log_cb(msg: str):
            q.put({"type": "log", "message": msg})

        def progress_cb(cur: int, tot: int, fname: str):
            q.put({"type": "progress", "current": cur, "total": tot, "filename": fname})

        try:
            result = process_receipts_batch(
                template_path=Path("Reimbursement_sheet_1.xlsx"),
                receipts_folder=receipts_dir,
                output_dir=out_dir,
                employee_name=employee or "Employee",
                job_name_default=job_name,
                job_number_default=job_number,
                log_callback=log_cb,
                progress_callback=progress_cb,
                cancel_event=cancel,
            )
            out_path = result.get("output_path")
            _jobs[job_id]["output_path"] = out_path
            q.put({"type": "done", "filename": out_path.name if out_path else ""})
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
        finally:
            _jobs[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    if job_id in _jobs:
        _jobs[job_id]["cancel"].set()
        return {"ok": True}
    return {"ok": False}


@app.get("/events/{job_id}")
async def events(job_id: str):
    if job_id not in _jobs:
        return HTMLResponse("Job not found", status_code=404)

    async def generate():
        job = _jobs[job_id]
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=0.1)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
            except Empty:
                if job["done"]:
                    break
                await asyncio.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/download/{job_id}")
async def download(job_id: str):
    if job_id not in _jobs:
        return HTMLResponse("Job not found", status_code=404)

    output_path = _jobs[job_id].get("output_path")
    if not output_path or not Path(output_path).exists():
        return HTMLResponse("Output file not ready", status_code=404)

    filename = Path(output_path).name

    async def file_stream():
        with open(output_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk
        shutil.rmtree(TMP_ROOT / job_id, ignore_errors=True)
        _jobs.pop(job_id, None)

    return StreamingResponse(
        file_stream(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
