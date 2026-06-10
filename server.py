#!/usr/bin/env python3
"""server.py — FastAPI web frontend for the receipt processor."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import process_receipts as _pr
from process_receipts import (
    initialize_models,
    process_receipts_batch,
    generate_spreadsheet,
    list_available_models,
    _try_load_model,
    OLMOCR_MODEL_ID,
    GEMMA_SMALL_MODEL_ID,
    GEMMA_LARGE_MODEL_ID,
    OUTPUT_FOLDER,
    HOST_OUTPUT_PATH,
)

TMP_ROOT = Path("tmp")
TMP_ROOT.mkdir(exist_ok=True)

# job_id → {queue, done, output_path, results, cancel}
_jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=initialize_models, daemon=True).start()
    yield


app = FastAPI(title="Receipt Processor", lifespan=lifespan)


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse("templates/index.html", media_type="text/html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("templates/manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse("templates/icon.svg", media_type="image/svg+xml")


@app.post("/process")
async def process(
    files: list[UploadFile] = File(...),
    employee: str = Form("Employee"),
    job_name: str = Form(""),
    job_number: str = Form(""),
):
    """
    Upload receipts and start the pipeline (OCR + Unified Distillation).
    Returns job_id immediately; stream progress via GET /events/{job_id}.
    Spreadsheet is NOT generated automatically — use POST /generate-spreadsheet/{job_id}.
    """
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
    _jobs[job_id] = {
        "queue":       q,
        "done":        False,
        "output_path": None,
        "results":     None,       # stored after processing for deferred spreadsheet
        "employee":    employee or "Employee",
        "cancel":      cancel,
        "out_dir":     out_dir,
    }

    def run():
        def log_cb(msg: str):
            q.put({"type": "log", "message": msg})

        def progress_cb(cur: int, tot: int, fname: str):
            q.put({"type": "progress", "current": cur, "total": tot, "filename": fname})

        def receipt_status_cb(idx: int, tot: int, fname: str, status: str, data):
            q.put({
                "type":     "receipt_update",
                "index":    idx,
                "total":    tot,
                "filename": fname,
                "status":   status,
                "data":     _safe_receipt_data(data),
            })

        try:
            result = process_receipts_batch(
                template_path=Path("Reimbursement_sheet_1.xlsx"),
                receipts_folder=receipts_dir,
                output_dir=out_dir,
                employee_name=employee or "Employee",
                job_name_default=job_name,
                job_number_default=job_number,
                auto_generate=False,   # deferred — user clicks "Generate Spreadsheet"
                log_callback=log_cb,
                progress_callback=progress_cb,
                cancel_event=cancel,
                receipt_status_callback=receipt_status_cb,
            )
            _jobs[job_id]["results"] = result.get("results", [])
            q.put({
                "type":           "done",
                "processed":      result.get("processed", 0),
                "skipped":        result.get("skipped", []),
                "total":          result.get("total", 0),
                "expense_period": result.get("expense_period", ""),
            })
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
        finally:
            _jobs[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


def _safe_receipt_data(data) -> dict:
    """Serialize receipt data for SSE — strip non-serialisable internal fields."""
    if not data:
        return {}
    out = {}
    for k in ("date", "vendor", "amount", "category", "job_name", "job_number",
              "expense_description", "ai_summary", "_flag", "_category",
              "_new_filename", "_file", "flags"):
        if k in data:
            out[k] = data[k]
    return out


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


@app.post("/generate-spreadsheet/{job_id}")
async def make_spreadsheet(job_id: str):
    """
    Generate the Excel workbook from stored processing results.
    Called when the user clicks the 'Generate Spreadsheet' button.
    Returns the file as a download.
    """
    if job_id not in _jobs:
        return HTMLResponse("Job not found", status_code=404)

    job = _jobs[job_id]
    results = job.get("results")
    if not results:
        return HTMLResponse("No processed results available", status_code=404)

    out_dir = job.get("out_dir", TMP_ROOT / job_id / "output")
    employee = job.get("employee", "Employee")

    def _build():
        return generate_spreadsheet(
            results=results,
            output_dir=Path(str(out_dir)),
            employee_name=employee,
            host_output_path=HOST_OUTPUT_PATH,
        )

    try:
        output_path = await asyncio.get_event_loop().run_in_executor(None, _build)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if not output_path or not Path(output_path).exists():
        return HTMLResponse("Spreadsheet generation failed", status_code=500)

    job["output_path"] = output_path
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


@app.post("/retry-receipt/{job_id}/{index}")
async def retry_receipt(job_id: str, index: int):
    """
    Re-process a single failed receipt using Gemma direct-vision (bypasses olmOCR).
    Updates the job results in-place and emits an SSE receipt_update via the job queue.
    """
    if job_id not in _jobs:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)

    job = _jobs[job_id]
    results = job.get("results", [])

    # Find the matching result by index (index is 1-based from the Kanban)
    receipt_idx = index - 1
    if receipt_idx < 0 or receipt_idx >= len(results):
        return JSONResponse({"ok": False, "error": "Invalid receipt index"}, status_code=400)

    target = results[receipt_idx]
    img_path_str = target.get("_image_path")
    if not img_path_str or not Path(img_path_str).exists():
        return JSONResponse({"ok": False, "error": "Image file not available for retry"}, status_code=404)

    def _retry():
        from openai import OpenAI
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")
        return _pr._extract_with_model(client, Path(img_path_str), _pr._active_gemma_model)

    try:
        new_data = await asyncio.get_event_loop().run_in_executor(None, _retry)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if new_data and not _pr._is_low_confidence(new_data):
        # Merge new data into existing result
        for k in ("date", "vendor", "amount", "category", "job_name", "job_number",
                  "expense_description", "ai_summary", "flags"):
            if k in new_data:
                target[k] = new_data[k]
        target["_flag"] = (new_data.get("flags") or [{}])[0].get("flag", "") if new_data.get("flags") else ""
        q = job.get("queue")
        if q:
            q.put({"type": "receipt_update", "index": index, "status": "done",
                   "data": _safe_receipt_data(target)})
        return JSONResponse({"ok": True, "data": _safe_receipt_data(target)})

    return JSONResponse({"ok": False, "error": "Retry extraction failed or still low confidence"}, status_code=422)


@app.get("/models")
async def get_models():
    return JSONResponse({
        "olmocr":      _pr._active_model,
        "gemma":       _pr._active_gemma_model,
        "gemma_small": GEMMA_SMALL_MODEL_ID,
        "gemma_large": GEMMA_LARGE_MODEL_ID,
    })


@app.get("/models/available")
async def get_available_models():
    """List all models currently loaded in LM Studio — for the model selector UI."""
    def _fetch():
        return list_available_models()

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({"models": models, "active_gemma": _pr._active_gemma_model, "ok": True})
    except Exception as exc:
        return JSONResponse({"models": [], "active_gemma": _pr._active_gemma_model,
                             "ok": False, "error": str(exc)})


class GemmaSwapRequest(BaseModel):
    model: str  # model id from discovery list, or "small" | "large"


@app.post("/models/gemma")
async def swap_gemma_model(body: GemmaSwapRequest):
    if body.model == "small":
        target = GEMMA_SMALL_MODEL_ID
    elif body.model == "large":
        target = GEMMA_LARGE_MODEL_ID
    else:
        target = body.model

    ok = _try_load_model(target)
    if ok:
        _pr._active_gemma_model = target
        return JSONResponse({"ok": True, "active_gemma": target})
    return JSONResponse({"ok": False, "error": f"Could not load model: {target}"}, status_code=503)


@app.get("/open-output-folder")
async def open_output_folder():
    return JSONResponse({"path": str(OUTPUT_FOLDER)})


def _is_docker() -> bool:
    return Path("/.dockerenv").exists()


def _open_folder_native(folder: Path) -> None:
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    elif sys.platform == "win32":
        import os as _os
        _os.startfile(str(folder))
    else:
        subprocess.Popen(["xdg-open", str(folder)])


@app.post("/open-folder")
async def open_folder_in_manager():
    folder = Path(OUTPUT_FOLDER).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if _is_docker():
        return JSONResponse({"ok": True, "path": str(folder), "docker": True})
    try:
        _open_folder_native(folder)
        return JSONResponse({"ok": True, "path": str(folder)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "path": str(folder)}, status_code=500)


@app.get("/watch/folder")
async def watch_folder_path():
    try:
        from watch_mode import WATCH_INBOX
        return JSONResponse({"path": str(WATCH_INBOX), "ok": True})
    except Exception as exc:
        return JSONResponse({"path": "", "ok": False, "error": str(exc)})


@app.post("/open-watch-folder")
async def open_watch_folder():
    try:
        from watch_mode import WATCH_INBOX
        folder = Path(WATCH_INBOX).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        if _is_docker():
            return JSONResponse({"ok": True, "path": str(folder), "docker": True})
        _open_folder_native(folder)
        return JSONResponse({"ok": True, "path": str(folder)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/models/lmstudio")
async def get_lmstudio_models():
    def _fetch():
        from openai import OpenAI
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")
        response = client.models.list()
        return [m.id for m in response.data]

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({"loaded": models, "ok": True})
    except Exception as exc:
        return JSONResponse({"loaded": [], "ok": False, "error": str(exc)})


@app.post("/watch/send-email")
async def watch_send_email():
    try:
        from watch_mode import load_state, send_report
        from openai import OpenAI
        state  = load_state()
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")
        result = send_report(state, client=client)
        status = 200 if result.get("ok") else 503
        return JSONResponse(result, status_code=status)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/watch/status")
async def watch_status():
    try:
        from watch_mode import load_state, SMTP_HOST, EMAIL_TO
        state = load_state()
        return JSONResponse({
            "receipts":        len(state.get("receipts", [])),
            "last_emailed":    state.get("last_emailed"),
            "smtp_configured": bool(SMTP_HOST and EMAIL_TO),
        })
    except Exception as exc:
        return JSONResponse({"receipts": 0, "last_emailed": None, "smtp_configured": False,
                             "error": str(exc)})


@app.get("/download/{job_id}")
async def download(job_id: str):
    """Legacy download endpoint — serves an already-generated spreadsheet."""
    if job_id not in _jobs:
        return HTMLResponse("Job not found", status_code=404)

    output_path = _jobs[job_id].get("output_path")
    if not output_path or not Path(output_path).exists():
        return HTMLResponse("Output file not ready — use POST /generate-spreadsheet/{job_id}", status_code=404)

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
