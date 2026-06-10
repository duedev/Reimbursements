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
    GEMMA_SMALL_MODEL_ID,
    GEMMA_LARGE_MODEL_ID,
    OUTPUT_FOLDER,
    HOST_OUTPUT_PATH,
)

TMP_ROOT = Path("tmp")
TMP_ROOT.mkdir(exist_ok=True)

# job_id → {queue, done, output_path, results, cancel, receipt_file_map}
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

    # Store original filename → tmp path mapping for retry support
    receipt_file_map: dict[str, str] = {}
    for f in files:
        fname = f.filename or "receipt"
        dest = receipts_dir / fname
        with open(dest, "wb") as fh:
            fh.write(await f.read())
        receipt_file_map[fname] = str(dest)

    cancel = threading.Event()
    q: Queue = Queue()
    _jobs[job_id] = {
        "queue":            q,
        "done":             False,
        "output_path":      None,
        "results":          None,
        "employee":         employee or "Employee",
        "job_name_default": job_name,
        "job_number_default": job_number,
        "cancel":           cancel,
        "out_dir":          out_dir,
        "receipts_dir":     receipts_dir,
        "receipt_file_map": receipt_file_map,
    }

    def run():
        def log_cb(msg: str):
            q.put({"type": "log", "message": msg})

        def progress_cb(cur: int, tot: int, fname: str):
            q.put({"type": "progress", "current": cur, "total": tot, "filename": fname})

        def receipt_status_cb(idx: int, tot: int, fname: str, status: str, data, model: str = ""):
            q.put({
                "type":     "receipt_update",
                "index":    idx,
                "total":    tot,
                "filename": fname,
                "status":   status,
                "model":    model,
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
                auto_generate=False,
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
              "expense_description", "summary", "ai_summary", "_flag", "_category",
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
    """Generate the Excel workbook from stored processing results."""
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


class RetryRequest(BaseModel):
    filename: str


@app.post("/retry-receipt/{job_id}")
async def retry_receipt(job_id: str, body: RetryRequest):
    """
    Re-process a single failed receipt by original filename.
    Updates the job results in-place and emits an SSE receipt_update via the job queue.
    """
    if job_id not in _jobs:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)

    job = _jobs[job_id]
    filename = body.filename

    # Look up the image path from the upload map
    file_map: dict = job.get("receipt_file_map", {})
    img_path_str = file_map.get(filename)

    # Also check if there's already a processed result with a renamed path
    if not img_path_str:
        results = job.get("results") or []
        for r in results:
            if r.get("_file") == filename or r.get("_new_filename") == filename:
                img_path_str = r.get("_image_path")
                break

    if not img_path_str or not Path(img_path_str).exists():
        # Try finding it in the receipts directory
        receipts_dir: Path = job.get("receipts_dir", TMP_ROOT / job_id / "receipts")
        candidate = receipts_dir / filename
        if candidate.exists():
            img_path_str = str(candidate)
        else:
            return JSONResponse(
                {"ok": False, "error": f"Image file not found for retry: {filename}"},
                status_code=404,
            )

    img_path = Path(img_path_str)

    def _retry():
        from openai import OpenAI
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")

        def status_cb(status, data=None, model=""):
            q = job.get("queue")
            if q:
                q.put({
                    "type":     "receipt_update",
                    "index":    0,
                    "total":    0,
                    "filename": filename,
                    "status":   status,
                    "model":    model,
                    "data":     _safe_receipt_data(data),
                })

        return _pr._extract_receipt_with_status(client, Path(img_path_str), status_cb)

    try:
        new_data = await asyncio.get_event_loop().run_in_executor(None, _retry)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if new_data and not _pr._is_low_confidence(new_data):
        from process_receipts import classify_category
        category = classify_category(new_data)
        new_data["_category"] = category
        if category == "fuel":
            new_data["expense_description"] = None

        jn = job.get("job_name_default", "")
        jnum = job.get("job_number_default", "")
        new_data["job_name"]   = jn or None
        new_data["job_number"] = jnum or None

        flags_list = new_data.get("flags") or []
        if flags_list and not new_data.get("_flag"):
            new_data["_flag"] = flags_list[0].get("flag", "")

        results = job.get("results") or []
        matched = False
        for r in results:
            if r.get("_file") == filename or r.get("_new_filename") == filename:
                r.update(new_data)
                matched = True
                break
        if not matched:
            new_data["_file"] = filename
            new_data["_image_path"] = img_path_str
            results.append(new_data)
            job["results"] = results

        q = job.get("queue")
        if q:
            q.put({
                "type": "receipt_update", "index": 0, "total": 0,
                "filename": filename, "status": "done", "model": "",
                "data": _safe_receipt_data(new_data),
            })
        return JSONResponse({"ok": True, "data": _safe_receipt_data(new_data)})

    return JSONResponse(
        {"ok": False, "error": "Retry extraction failed or still low confidence"},
        status_code=422,
    )


@app.get("/models/available")
async def get_available_models():
    """List all models currently loaded in LM Studio — for the model selector UI."""
    def _fetch():
        return list_available_models()

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({
            "models":         models,
            "active_distill": _pr._active_distill_model,
            "active_ocr":     _pr._active_ocr_model,
            "ok":             True,
        })
    except Exception as exc:
        return JSONResponse({
            "models":         [],
            "active_distill": _pr._active_distill_model,
            "active_ocr":     _pr._active_ocr_model,
            "ok":             False,
            "error":          str(exc),
        })


class ModelSwapRequest(BaseModel):
    model: str


@app.post("/models/distill")
async def swap_distill_model(body: ModelSwapRequest):
    target = GEMMA_SMALL_MODEL_ID if body.model == "small" else (
        GEMMA_LARGE_MODEL_ID if body.model == "large" else body.model
    )
    ok = _try_load_model(target)
    if ok:
        _pr._active_distill_model = target
        return JSONResponse({"ok": True, "active_distill": target})
    return JSONResponse({"ok": False, "error": f"Could not load model: {target}"}, status_code=503)


@app.post("/models/ocr")
async def swap_ocr_model(body: ModelSwapRequest):
    """Set (or clear) the dedicated OCR model. Pass model="" to disable dedicated OCR."""
    target = body.model.strip()
    if not target:
        _pr._active_ocr_model = ""
        return JSONResponse({"ok": True, "active_ocr": ""})
    ok = _try_load_model(target)
    if ok:
        _pr._active_ocr_model = target
        return JSONResponse({"ok": True, "active_ocr": target})
    return JSONResponse({"ok": False, "error": f"Could not load model: {target}"}, status_code=503)


@app.get("/folders")
async def get_folders():
    """Return configured folder paths for the UI."""
    paths = {"output": str(OUTPUT_FOLDER)}
    try:
        from watch_mode import WATCH_INBOX, WATCH_STAGED, WATCH_STATE
        paths["watch_inbox"]  = str(WATCH_INBOX)
        paths["watch_staged"] = str(WATCH_STAGED)
        paths["watch_state"]  = str(WATCH_STATE)
    except Exception:
        pass
    return JSONResponse(paths)



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


class OpenFolderRequest(BaseModel):
    path: str


@app.post("/open-folder-path")
async def open_folder_by_path(body: OpenFolderRequest):
    """Open an arbitrary folder path in the native file manager."""
    folder = Path(body.path).resolve()
    if _is_docker():
        return JSONResponse({"ok": True, "path": str(folder), "docker": True})
    try:
        folder.mkdir(parents=True, exist_ok=True)
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
