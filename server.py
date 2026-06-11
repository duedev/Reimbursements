#!/usr/bin/env python3
"""server.py — FastAPI web frontend for the receipt processor (queue-based architecture)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from openai import OpenAI

import process_receipts as _pr
from process_receipts import (
    initialize_models,
    _extract_receipt_with_status,
    _is_low_confidence,
    _compute_confidence,
    _get_fail_reason,
    classify_category,
    rename_receipt_image,
    _detect_duplicates,
    generate_spreadsheet,
    list_available_models,
    _try_load_model,
    pdf_to_images,
    GEMMA_SMALL_MODEL_ID,
    GEMMA_LARGE_MODEL_ID,
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    OUTPUT_FOLDER,
    HOST_OUTPUT_PATH,
    RECEIPTS_FOLDER,
)

# ── Folder / config paths ──────────────────────────────────────────────────────

INTAKE_FOLDER = Path(RECEIPTS_FOLDER)
OUT_FOLDER    = Path(OUTPUT_FOLDER)
IMAGES_FOLDER = OUT_FOLDER / "receipts"   # processed receipt images land here
CONFIG_FILE   = OUT_FOLDER / ".app_config.json"

# ── Global state ───────────────────────────────────────────────────────────────

_work_queue: deque = deque()
_work_lock   = threading.Lock()

_kanban: dict[str, dict] = {}
_kanban_lock = threading.Lock()

_results: list[dict] = []
_results_lock = threading.Lock()

_last_context: dict = {"employee": "Employee", "job_name": "", "job_number": ""}

_seen_intake: set[str] = set()
_seen_lock   = threading.Lock()

_worker_cancel = threading.Event()

_subscribers: list[Queue] = []
_sub_lock = threading.Lock()


# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    OUT_FOLDER.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def _host_intake() -> str:
    cfg = _load_config()
    return cfg.get("host_intake_path") or os.getenv("HOST_INTAKE_PATH", "")


def _host_output() -> str:
    cfg = _load_config()
    return cfg.get("host_output_path") or HOST_OUTPUT_PATH or ""


def _save_field(cfg: dict, list_key: str, value: str) -> None:
    if not value.strip():
        return
    lst = cfg.get(list_key, [])
    if value not in lst:
        lst.insert(0, value)
    cfg[list_key] = lst[:20]


# ── SSE broadcast helpers ──────────────────────────────────────────────────────

def _broadcast(event: dict) -> None:
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass


def _add_subscriber() -> Queue:
    q: Queue = Queue()
    with _sub_lock:
        _subscribers.append(q)
    return q


def _remove_subscriber(q: Queue) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


# ── Kanban helpers ─────────────────────────────────────────────────────────────

def _update_kanban(filename: str, status: str, data, model: str = "") -> None:
    with _kanban_lock:
        _kanban[filename] = {"status": status, "data": _safe_receipt_data(data), "model": model}


def _safe_receipt_data(data) -> dict:
    """Serialize receipt data for SSE — strip non-serialisable internal fields."""
    if not data:
        return {}
    out = {}
    for k in ("date", "vendor", "amount", "category", "job_name", "job_number",
              "expense_description", "summary", "ai_summary", "_flag", "_category",
              "_new_filename", "_file", "flags", "_confidence", "_error"):
        if k in data:
            out[k] = data[k]
    return out


# ── Background worker ──────────────────────────────────────────────────────────

def _run_worker() -> None:
    while not _worker_cancel.is_set():
        with _work_lock:
            batch = list(_work_queue) if _work_queue else []
            if batch:
                _work_queue.clear()

        if not batch:
            time.sleep(0.4)
            continue

        _broadcast({"type": "log", "message": f"[worker] Processing {len(batch)} receipt(s)…"})
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")

        futures_map: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=_pr.MAX_PARALLEL_REQUESTS) as ex:
            for item in batch:
                if _worker_cancel.is_set():
                    break
                fname = item["filename"]
                path  = Path(item["path"])

                def make_cb(fn: str):
                    def cb(status: str, data=None, model: str = "") -> None:
                        _update_kanban(fn, status, data, model)
                        _broadcast({
                            "type":     "kanban_update",
                            "filename": fn,
                            "status":   status,
                            "data":     _safe_receipt_data(data),
                            "model":    model,
                        })
                    return cb

                future = ex.submit(_extract_receipt_with_status, client, path, make_cb(fname))
                futures_map[future] = item

            for future in concurrent.futures.as_completed(futures_map):
                if _worker_cancel.is_set():
                    break
                item  = futures_map[future]
                fname = item["filename"]
                path  = Path(item["path"])
                try:
                    data = future.result()
                except Exception as exc:
                    data = None
                    _broadcast({"type": "log", "message": f"[worker] ERROR {fname}: {exc}"})

                if data is None or _is_low_confidence(data):
                    fail_reason = _get_fail_reason(data)
                    partial: dict = {}
                    if data is not None:
                        partial = dict(data)
                        partial["_flag"]   = "Manual review required — incomplete extraction"
                        partial["_error"]  = fail_reason
                        partial["_file"]   = fname
                        conf, _            = _compute_confidence(data)
                        partial["_confidence"] = conf
                    _update_kanban(fname, "failed", partial)
                    _broadcast({
                        "type":     "kanban_update",
                        "filename": fname,
                        "status":   "failed",
                        "data":     _safe_receipt_data(partial),
                        "model":    "",
                        "error":    fail_reason,
                    })
                    continue

                category = classify_category(data)
                data["_category"]  = category
                data["job_name"]   = item.get("job_name") or None
                data["job_number"] = item.get("job_number") or None
                flags = data.get("flags") or []
                if flags and not data.get("_flag"):
                    data["_flag"] = flags[0].get("flag", "")
                conf, _ = _compute_confidence(data)
                data["_confidence"] = conf

                IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
                final_path = rename_receipt_image(path, data, category, IMAGES_FOLDER)
                data["_new_filename"] = final_path.name
                data["_file"]         = fname
                data["_image_path"]   = str(final_path)

                with _results_lock:
                    _results.append(data)
                    _last_context.update({
                        "employee":   item.get("employee", "Employee"),
                        "job_name":   item.get("job_name", ""),
                        "job_number": item.get("job_number", ""),
                    })

                _update_kanban(fname, "done", data)
                _broadcast({
                    "type":     "kanban_update",
                    "filename": fname,
                    "status":   "done",
                    "data":     _safe_receipt_data(data),
                    "model":    "",
                })
                _broadcast({
                    "type":    "log",
                    "message": f"[{category.upper()}] {data.get('vendor','?')} — ${data.get('amount', 0):.2f}",
                })

        with _results_lock:
            _detect_duplicates(_results)
            n_done = len(_results)
        with _work_lock:
            n_pending = len(_work_queue)
        _broadcast({"type": "batch_done", "completed": n_done, "pending": n_pending})


# ── Background watcher ─────────────────────────────────────────────────────────

def _run_watcher() -> None:
    """Poll INTAKE_FOLDER every 5 seconds and auto-queue new image/PDF files."""
    while not _worker_cancel.is_set():
        try:
            if INTAKE_FOLDER.exists():
                for p in sorted(INTAKE_FOLDER.iterdir()):
                    if not p.is_file():
                        continue
                    suffix = p.suffix.lower()
                    if suffix not in SUPPORTED_EXTENSIONS:
                        continue

                    with _seen_lock:
                        if p.name in _seen_intake:
                            continue
                        _seen_intake.add(p.name)

                    if suffix in IMAGE_EXTENSIONS:
                        item = {
                            "filename":   p.name,
                            "path":       str(p),
                            "employee":   _last_context.get("employee", "Employee"),
                            "job_name":   _last_context.get("job_name", ""),
                            "job_number": _last_context.get("job_number", ""),
                        }
                        with _work_lock:
                            _work_queue.append(item)
                        _update_kanban(p.name, "queued", None)
                        _broadcast({
                            "type":     "kanban_update",
                            "filename": p.name,
                            "status":   "queued",
                            "data":     {},
                            "model":    "",
                        })
                        _broadcast({"type": "log", "message": f"[watcher] Queued {p.name}"})

                    elif suffix in PDF_EXTENSIONS:
                        try:
                            dest_dir = INTAKE_FOLDER / f"_pdf_{p.stem}"
                            pages    = pdf_to_images(p, dest_dir)
                            for page_path in pages:
                                with _seen_lock:
                                    _seen_intake.add(page_path.name)
                                item = {
                                    "filename":   page_path.name,
                                    "path":       str(page_path),
                                    "employee":   _last_context.get("employee", "Employee"),
                                    "job_name":   _last_context.get("job_name", ""),
                                    "job_number": _last_context.get("job_number", ""),
                                }
                                with _work_lock:
                                    _work_queue.append(item)
                                _update_kanban(page_path.name, "queued", None)
                                _broadcast({
                                    "type":     "kanban_update",
                                    "filename": page_path.name,
                                    "status":   "queued",
                                    "data":     {},
                                    "model":    "",
                                })
                            # Move the original PDF out of the intake folder
                            IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(p), str(IMAGES_FOLDER / p.name))
                            _broadcast({
                                "type":    "log",
                                "message": f"[watcher] PDF {p.name} → {len(pages)} page(s) queued",
                            })
                        except Exception as exc:
                            _broadcast({
                                "type":    "log",
                                "message": f"[watcher] PDF error {p.name}: {exc}",
                            })
        except Exception as exc:
            _broadcast({"type": "log", "message": f"[watcher] scan error: {exc}"})

        time.sleep(5)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=initialize_models, daemon=True).start()
    threading.Thread(target=_run_worker,       daemon=True).start()
    threading.Thread(target=_run_watcher,      daemon=True).start()
    yield
    _worker_cancel.set()


app = FastAPI(title="Receipt Processor", lifespan=lifespan)


# ── Static / template routes ───────────────────────────────────────────────────

@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse("templates/index.html", media_type="text/html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("templates/manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse("templates/icon.svg", media_type="image/svg+xml")


# ── Queue endpoints ────────────────────────────────────────────────────────────

@app.post("/queue/add")
async def queue_add(
    files: list[UploadFile] = File(...),
    employee:   str = Form("Employee"),
    job_name:   str = Form(""),
    job_number: str = Form(""),
):
    """Upload receipts and enqueue them for processing."""
    IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
    tmp_dir = IMAGES_FOLDER / f"_upload_{uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    queued: list[str] = []

    for f in files:
        fname = f.filename or "receipt"
        dest  = tmp_dir / fname
        with open(dest, "wb") as fh:
            fh.write(await f.read())

        suffix = dest.suffix.lower()

        if suffix in PDF_EXTENSIONS:
            try:
                pages = pdf_to_images(dest, tmp_dir / f"_pdf_{dest.stem}")
                dest.unlink(missing_ok=True)
                for page_path in pages:
                    with _seen_lock:
                        _seen_intake.add(page_path.name)
                    item = {
                        "filename":   page_path.name,
                        "path":       str(page_path),
                        "employee":   employee or "Employee",
                        "job_name":   job_name,
                        "job_number": job_number,
                    }
                    with _work_lock:
                        _work_queue.append(item)
                    _update_kanban(page_path.name, "queued", None)
                    _broadcast({
                        "type": "kanban_update", "filename": page_path.name,
                        "status": "queued", "data": {}, "model": "",
                    })
                    queued.append(page_path.name)
            except Exception as exc:
                _broadcast({"type": "log", "message": f"[upload] PDF error {fname}: {exc}"})

        elif suffix in IMAGE_EXTENSIONS:
            with _seen_lock:
                _seen_intake.add(dest.name)
            item = {
                "filename":   dest.name,
                "path":       str(dest),
                "employee":   employee or "Employee",
                "job_name":   job_name,
                "job_number": job_number,
            }
            with _work_lock:
                _work_queue.append(item)
            _update_kanban(dest.name, "queued", None)
            _broadcast({
                "type": "kanban_update", "filename": dest.name,
                "status": "queued", "data": {}, "model": "",
            })
            queued.append(dest.name)

    # Persist defaults
    cfg = _load_config()
    if employee:
        cfg["default_employee"] = employee
        _save_field(cfg, "saved_employees", employee)
    if job_name:
        cfg["default_job_name"] = job_name
        _save_field(cfg, "saved_job_names", job_name)
    if job_number:
        cfg["default_job_number"] = job_number
        _save_field(cfg, "saved_job_numbers", job_number)
    _save_config(cfg)

    with _work_lock:
        n_pending = len(_work_queue)

    return JSONResponse({"queued": queued, "pending": n_pending})


@app.post("/queue/add-intake")
async def queue_add_intake(
    employee:   str = Form("Employee"),
    job_name:   str = Form(""),
    job_number: str = Form(""),
):
    """Enqueue all unprocessed files currently in the intake folder."""
    queued: list[str] = []

    try:
        files_in_intake = sorted(
            p for p in INTAKE_FOLDER.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    except Exception:
        files_in_intake = []

    for p in files_in_intake:
        with _seen_lock:
            if p.name in _seen_intake:
                continue
            _seen_intake.add(p.name)

        suffix = p.suffix.lower()

        if suffix in IMAGE_EXTENSIONS:
            item = {
                "filename":   p.name,
                "path":       str(p),
                "employee":   employee or "Employee",
                "job_name":   job_name,
                "job_number": job_number,
            }
            with _work_lock:
                _work_queue.append(item)
            _update_kanban(p.name, "queued", None)
            _broadcast({
                "type": "kanban_update", "filename": p.name,
                "status": "queued", "data": {}, "model": "",
            })
            queued.append(p.name)

        elif suffix in PDF_EXTENSIONS:
            try:
                dest_dir = INTAKE_FOLDER / f"_pdf_{p.stem}"
                pages    = pdf_to_images(p, dest_dir)
                for page_path in pages:
                    with _seen_lock:
                        _seen_intake.add(page_path.name)
                    item = {
                        "filename":   page_path.name,
                        "path":       str(page_path),
                        "employee":   employee or "Employee",
                        "job_name":   job_name,
                        "job_number": job_number,
                    }
                    with _work_lock:
                        _work_queue.append(item)
                    _update_kanban(page_path.name, "queued", None)
                    _broadcast({
                        "type": "kanban_update", "filename": page_path.name,
                        "status": "queued", "data": {}, "model": "",
                    })
                    queued.append(page_path.name)
                IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(IMAGES_FOLDER / p.name))
            except Exception as exc:
                _broadcast({"type": "log", "message": f"[intake] PDF error {p.name}: {exc}"})

    # Persist defaults
    cfg = _load_config()
    if employee:
        cfg["default_employee"] = employee
        _save_field(cfg, "saved_employees", employee)
    if job_name:
        cfg["default_job_name"] = job_name
        _save_field(cfg, "saved_job_names", job_name)
    if job_number:
        cfg["default_job_number"] = job_number
        _save_field(cfg, "saved_job_numbers", job_number)
    _save_config(cfg)

    with _work_lock:
        n_pending = len(_work_queue)

    return JSONResponse({"queued": queued, "pending": n_pending})


@app.post("/queue/cancel")
async def queue_cancel():
    """Signal cancellation, drain the pending queue, then re-arm for future jobs."""
    _worker_cancel.set()
    with _work_lock:
        cleared = len(_work_queue)
        _work_queue.clear()
    _worker_cancel.clear()   # allow future processing
    return JSONResponse({"ok": True, "cleared": cleared})


@app.get("/queue/status")
async def queue_status():
    with _work_lock:
        n_pending = len(_work_queue)
    with _results_lock:
        n_completed = len(_results)
    with _kanban_lock:
        kanban_snapshot = {fn: {"status": v["status"]} for fn, v in _kanban.items()}
    return JSONResponse({"pending": n_pending, "completed": n_completed, "kanban": kanban_snapshot})


# ── Global SSE stream ──────────────────────────────────────────────────────────

@app.get("/events")
async def events_global():
    """Global SSE stream — all connected clients receive all events."""
    q = _add_subscriber()

    # Send full state snapshot on connect
    with _kanban_lock:
        kanban_snapshot = {fn: dict(v) for fn, v in _kanban.items()}
    with _work_lock:
        n_pending = len(_work_queue)
    with _results_lock:
        n_completed = len(_results)
    full_state = {
        "type":      "full_state",
        "kanban":    kanban_snapshot,
        "pending":   n_pending,
        "completed": n_completed,
    }

    async def generate():
        try:
            yield f"data: {json.dumps(full_state)}\n\n"
            while True:
                try:
                    msg = q.get_nowait()
                    yield f"data: {json.dumps(msg)}\n\n"
                except Empty:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
        finally:
            _remove_subscriber(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Intake listing ─────────────────────────────────────────────────────────────

@app.get("/receipt-image")
async def get_receipt_image(filename: str = ""):
    """Serve a receipt image by filename for UI previews (searches output/receipts dirs)."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse({"error": "invalid"}, status_code=400)
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif",  ".webp": "image/webp", ".bmp": "image/bmp",
    }
    search: list[Path] = [IMAGES_FOLDER, INTAKE_FOLDER]
    try:
        search += [d for d in IMAGES_FOLDER.iterdir() if d.is_dir()]
    except Exception:
        pass
    for folder in search:
        p = folder / filename
        if p.exists() and p.is_file():
            mt = ext_map.get(p.suffix.lower(), "image/jpeg")
            return FileResponse(str(p), media_type=mt)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/intake/files")
async def list_intake_files():
    """List image/PDF files currently in the intake folder."""
    try:
        files = sorted(
            p.name for p in INTAKE_FOLDER.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        return JSONResponse({"files": files, "count": len(files)})
    except Exception as exc:
        return JSONResponse({"files": [], "count": 0, "error": str(exc)})


# ── Spreadsheet generation ─────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    exclude_filenames: list[str] = []


@app.get("/results/check-duplicates")
async def check_duplicates():
    """Return groups of receipts that share the same vendor/date/amount."""
    with _results_lock:
        results_copy = list(_results)

    groups: dict[tuple, list] = {}
    for i, r in enumerate(results_copy):
        vendor = (r.get("vendor") or "").lower().strip()
        dt     = r.get("date") or ""
        try:
            amt = round(float(r.get("amount") or 0), 2)
        except (ValueError, TypeError):
            amt = 0.0
        if not vendor or not amt:
            continue
        key = (vendor, dt, amt)
        img_file = r.get("_new_filename") or r.get("_file") or ""
        groups.setdefault(key, []).append({
            "index":      i,
            "filename":   r.get("_file") or r.get("_new_filename", ""),
            "img_file":   img_file,   # for UI image preview
            "vendor":     r.get("vendor", ""),
            "date":       dt,
            "amount":     amt,
            "summary":    r.get("ai_summary") or r.get("summary") or "",
            "job_name":   r.get("job_name") or "",
            "job_number": r.get("job_number") or "",
        })

    dup_groups = [v for v in groups.values() if len(v) > 1]
    return JSONResponse({"has_duplicates": bool(dup_groups), "groups": dup_groups})


class ManualReceiptRequest(BaseModel):
    filename:   str
    vendor:     str = ""
    date:       str = ""
    amount:     str = ""
    category:   str = "misc"
    job_name:   str = ""
    job_number: str = ""
    summary:    str = ""


@app.post("/results/add-manual")
async def add_manual_result(body: ManualReceiptRequest):
    """Manually add or update a receipt result (for failed/partial extractions)."""
    try:
        amt = float(body.amount) if body.amount.strip() else 0.0
    except ValueError:
        amt = 0.0

    data: dict = {
        "vendor":       body.vendor.strip() or "Unknown",
        "date":         body.date.strip(),
        "amount":       amt,
        "category":     body.category or "misc",
        "_category":    body.category or "misc",
        "job_name":     body.job_name.strip() or _last_context.get("job_name") or None,
        "job_number":   body.job_number.strip() or _last_context.get("job_number") or None,
        "ai_summary":   body.summary.strip(),
        "_flag":        "Manual entry",
        "_file":        body.filename,
        "_confidence":  None,
    }

    with _results_lock:
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                r.update(data)
                break
        else:
            _results.append(data)

    _update_kanban(body.filename, "done", data)
    _broadcast({
        "type":     "kanban_update",
        "filename": body.filename,
        "status":   "done",
        "data":     _safe_receipt_data(data),
        "model":    "",
    })
    return JSONResponse({"ok": True})


@app.post("/generate-spreadsheet")
async def make_spreadsheet(body: GenerateRequest = GenerateRequest()):
    """Generate an Excel workbook from all completed results."""
    with _results_lock:
        results_copy = list(_results)

    if body.exclude_filenames:
        excl = set(body.exclude_filenames)
        results_copy = [r for r in results_copy
                        if r.get("_file") not in excl and r.get("_new_filename") not in excl]

    if not results_copy:
        return HTMLResponse("No processed results available", status_code=404)

    # Deep-copy so we don't mutate _results; re-detect duplicates on filtered set
    # so excluded items don't leave stale duplicate flags on the remaining receipts
    results_copy = copy.deepcopy(results_copy)
    _dup_kw = ("potential duplicate", "duplicate of")
    for r in results_copy:
        flag = (r.get("_flag") or "").lower()
        if any(kw in flag for kw in _dup_kw):
            r["_flag"] = ""
    _detect_duplicates(results_copy)

    employee = _last_context.get("employee", "Employee")

    def _build():
        return generate_spreadsheet(
            results=results_copy,
            output_dir=OUT_FOLDER,
            employee_name=employee,
        )

    try:
        output_path = await asyncio.get_event_loop().run_in_executor(None, _build)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if not output_path or not Path(output_path).exists():
        return HTMLResponse("Spreadsheet generation failed", status_code=500)

    filename = Path(output_path).name

    async def file_stream():
        with open(output_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        file_stream(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Legacy alias — same behaviour as global endpoint
@app.post("/generate-spreadsheet/{job_id}")
async def make_spreadsheet_legacy(job_id: str):
    return await make_spreadsheet()


# ── Results management ─────────────────────────────────────────────────────────

@app.post("/results/clear")
async def clear_results():
    """Clear completed results and remove done/failed entries from the kanban."""
    with _results_lock:
        _results.clear()
    with _kanban_lock:
        to_remove = [fn for fn, v in _kanban.items() if v["status"] in ("done", "failed")]
        for fn in to_remove:
            del _kanban[fn]
    _broadcast({"type": "results_cleared"})
    return JSONResponse({"ok": True})


# ── Retry endpoint ─────────────────────────────────────────────────────────────

class RetryRequest(BaseModel):
    filename: str


@app.post("/retry-receipt")
async def retry_receipt(body: RetryRequest):
    """Re-queue a failed receipt for reprocessing (sends it back to the front of the queue)."""
    filename = body.filename

    # 1. Try _results first (image was already renamed/moved to IMAGES_FOLDER)
    img_path_str: str | None = None
    with _results_lock:
        for r in _results:
            if r.get("_file") == filename or r.get("_new_filename") == filename:
                img_path_str = r.get("_image_path")
                break

    # 2. Try kanban data
    if not img_path_str:
        with _kanban_lock:
            entry = _kanban.get(filename, {})
        kdata = entry.get("data") or {}
        img_path_str = kdata.get("_image_path")

    # 3. Try intake folder directly
    if not img_path_str or not Path(img_path_str).exists():
        candidate = INTAKE_FOLDER / filename
        if candidate.exists():
            img_path_str = str(candidate)

    if not img_path_str or not Path(img_path_str).exists():
        return JSONResponse(
            {"ok": False, "error": f"Image file not found for retry: {filename}"},
            status_code=404,
        )

    # Re-queue at the front so it processes next
    item = {
        "filename":   filename,
        "path":       img_path_str,
        "employee":   _last_context.get("employee", "Employee"),
        "job_name":   _last_context.get("job_name", ""),
        "job_number": _last_context.get("job_number", ""),
    }
    with _work_lock:
        _work_queue.appendleft(item)

    _update_kanban(filename, "queued", None)
    _broadcast({
        "type": "kanban_update", "filename": filename,
        "status": "queued", "data": {}, "model": "",
    })
    return JSONResponse({"ok": True, "queued": filename})


@app.post("/queue/clear-all")
async def queue_clear_all():
    """Full board reset: drain queue, clear kanban, clear results, reset seen-intake set."""
    _worker_cancel.set()
    with _work_lock:
        cleared = len(_work_queue)
        _work_queue.clear()
    _worker_cancel.clear()

    with _kanban_lock:
        _kanban.clear()
    with _results_lock:
        _results.clear()
    with _seen_lock:
        _seen_intake.clear()   # allows intake files to be re-queued after board reset

    _broadcast({"type": "kanban_cleared"})
    return JSONResponse({"ok": True, "cleared": cleared})


@app.post("/kanban/remove")
async def kanban_remove(body: RetryRequest):
    """Remove a single item from the kanban (client-initiated dismiss)."""
    filename = body.filename
    with _kanban_lock:
        _kanban.pop(filename, None)
    return JSONResponse({"ok": True})


# ── Model management ───────────────────────────────────────────────────────────

@app.get("/models/available")
async def get_available_models():
    """List all models loaded in LM Studio — for the model selector UI."""
    def _fetch():
        return list_available_models()

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({
            "models":           models,
            "active_distill":   _pr._active_distill_model,
            "active_ocr":       _pr._active_ocr_model,
            "distill_thinking": _pr._distill_thinking,
            "ocr_thinking":     _pr._ocr_thinking,
            "ok":               True,
        })
    except Exception as exc:
        return JSONResponse({
            "models":           [],
            "active_distill":   _pr._active_distill_model,
            "active_ocr":       _pr._active_ocr_model,
            "distill_thinking": _pr._distill_thinking,
            "ocr_thinking":     _pr._ocr_thinking,
            "ok":               False,
            "error":            str(exc),
        })


class ModelSwapRequest(BaseModel):
    model: str


@app.post("/models/distill")
async def swap_distill_model(body: ModelSwapRequest):
    model_str = body.model
    no_think  = model_str.endswith("::no-think")
    model_str = model_str.removesuffix("::no-think") if no_think else model_str
    target = GEMMA_SMALL_MODEL_ID if model_str == "small" else (
        GEMMA_LARGE_MODEL_ID if model_str == "large" else model_str
    )
    ok = _try_load_model(target)
    if ok:
        _pr._active_distill_model = target
        _pr._distill_thinking = not no_think
        return JSONResponse({"ok": True, "active_distill": target, "thinking": not no_think})
    return JSONResponse({"ok": False, "error": f"Could not load model: {target}"}, status_code=503)


@app.post("/models/ocr")
async def swap_ocr_model(body: ModelSwapRequest):
    """Set (or clear) the dedicated OCR model. Pass model="" to disable dedicated OCR."""
    target = body.model.strip()
    no_think = target.endswith("::no-think")
    target   = target.removesuffix("::no-think") if no_think else target
    if not target:
        _pr._active_ocr_model = ""
        _pr._ocr_thinking = False
        return JSONResponse({"ok": True, "active_ocr": ""})
    ok = _try_load_model(target)
    if ok:
        _pr._active_ocr_model = target
        _pr._ocr_thinking = not no_think
        return JSONResponse({"ok": True, "active_ocr": target, "thinking": not no_think})
    return JSONResponse({"ok": False, "error": f"Could not load model: {target}"}, status_code=503)


@app.get("/models/lmstudio")
async def get_lmstudio_models():
    def _fetch():
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")
        response = client.models.list()
        return [m.id for m in response.data]

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({"loaded": models, "ok": True})
    except Exception as exc:
        return JSONResponse({"loaded": [], "ok": False, "error": str(exc)})


# ── Folder / file-manager helpers ──────────────────────────────────────────────

def _is_docker() -> bool:
    return Path("/.dockerenv").exists()


def _open_folder_native(folder: Path) -> None:
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    elif sys.platform == "win32":
        os.startfile(str(folder))
    else:
        subprocess.Popen(["xdg-open", str(folder)])


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
    host = _host_output()
    return JSONResponse({"path": str(OUTPUT_FOLDER), "host_path": host})


@app.post("/open-folder")
async def open_folder_in_manager():
    folder = Path(OUTPUT_FOLDER).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if _is_docker():
        host = _host_output() or str(folder)
        return JSONResponse({"ok": True, "path": str(folder), "host_path": host, "docker": True})
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
        host = _host_intake() or ""
        return JSONResponse({"path": str(WATCH_INBOX), "host_path": host, "ok": True})
    except Exception as exc:
        return JSONResponse({"path": "", "host_path": "", "ok": False, "error": str(exc)})


@app.post("/open-watch-folder")
async def open_watch_folder():
    try:
        from watch_mode import WATCH_INBOX
        folder = Path(WATCH_INBOX).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        if _is_docker():
            host = _host_intake() or str(folder)
            return JSONResponse({"ok": True, "path": str(folder), "host_path": host, "docker": True})
        _open_folder_native(folder)
        return JSONResponse({"ok": True, "path": str(folder)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Settings ───────────────────────────────────────────────────────────────────

class SettingsRequest(BaseModel):
    host_intake_path: str = ""
    host_output_path: str = ""


@app.get("/settings")
async def get_settings():
    cfg = _load_config()
    return JSONResponse({
        "host_intake_path": cfg.get("host_intake_path") or os.getenv("HOST_INTAKE_PATH", ""),
        "host_output_path": cfg.get("host_output_path") or HOST_OUTPUT_PATH or "",
    })


@app.post("/settings")
async def save_settings(body: SettingsRequest):
    try:
        cfg = _load_config()
        cfg["host_intake_path"] = body.host_intake_path.strip()
        cfg["host_output_path"] = body.host_output_path.strip()
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Saved fields ───────────────────────────────────────────────────────────────

class SaveFieldsRequest(BaseModel):
    employee:   str = ""
    job_name:   str = ""
    job_number: str = ""


@app.get("/saved-fields")
async def get_saved_fields():
    cfg = _load_config()
    return JSONResponse({
        "employees":   cfg.get("saved_employees",   []),
        "job_names":   cfg.get("saved_job_names",   []),
        "job_numbers": cfg.get("saved_job_numbers", []),
    })


@app.post("/saved-fields")
async def save_fields(body: SaveFieldsRequest):
    cfg = _load_config()
    _save_field(cfg, "saved_employees",   body.employee)
    _save_field(cfg, "saved_job_names",   body.job_name)
    _save_field(cfg, "saved_job_numbers", body.job_number)
    _save_config(cfg)
    return JSONResponse({"ok": True})


class RemoveFieldRequest(BaseModel):
    list_key: str
    value:    str


@app.post("/saved-fields/remove")
async def remove_saved_field(body: RemoveFieldRequest):
    """Remove a single value from a saved-fields list."""
    allowed = {"saved_employees", "saved_job_names", "saved_job_numbers"}
    if body.list_key not in allowed:
        return JSONResponse({"ok": False, "error": "invalid key"}, status_code=400)
    cfg = _load_config()
    lst = cfg.get(body.list_key, [])
    try:
        lst.remove(body.value)
    except ValueError:
        pass
    cfg[body.list_key] = lst
    _save_config(cfg)
    return JSONResponse({"ok": True})


# ── Watch-mode / email ─────────────────────────────────────────────────────────

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
        return JSONResponse({
            "receipts": 0, "last_emailed": None, "smtp_configured": False,
            "error": str(exc),
        })


@app.post("/watch/send-email")
async def watch_send_email():
    try:
        from watch_mode import load_state, send_report
        state  = load_state()
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio")
        result = send_report(state, client=client)
        status = 200 if result.get("ok") else 503
        return JSONResponse(result, status_code=status)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
