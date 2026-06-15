#!/usr/bin/env python3
"""server.py — FastAPI web frontend for the receipt processor (queue-based architecture)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import csv
import io
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from openai import OpenAI

import app_secrets
import process_receipts as _pr
import scheduler
from process_receipts import (
    LLM_TIMEOUT,
    LLM_MAX_RETRIES,
    initialize_models,
    _extract_receipt_with_status,
    _is_low_confidence,
    _has_ocr_flag,
    _compute_confidence,
    _get_fail_reason,
    audit_amount,
    classify_category,
    sort_key_for_receipt,
    rename_receipt_image,
    compress_result_images,
    _detect_duplicates,
    generate_spreadsheet,
    list_available_models,
    _try_load_model,
    pdf_to_images,
    extract_archive,
    APP_VERSION,
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    ARCHIVE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    OUTPUT_FOLDER,
    RECEIPTS_FOLDER,
    CONFIG_FILE,
    DEFAULT_JOB_NAME,
    DEFAULT_JOB_NUMBER,
)

HOST_OUTPUT_PATH = os.getenv("HOST_OUTPUT_PATH", "")

# ── Folder / config paths ──────────────────────────────────────────────────────

INTAKE_FOLDER      = Path(RECEIPTS_FOLDER)
OUT_FOLDER         = Path(OUTPUT_FOLDER)
IMAGES_FOLDER      = OUT_FOLDER / "receipts"    # completed receipt images land here
PROCESSING_FOLDER  = OUT_FOLDER / "processing"  # in-flight and failed images live here
REJECTED_FOLDER    = OUT_FOLDER / "unsupported" # files dropped in intake we can't read
# CONFIG_FILE is the single authoritative app-config path, defined once in
# process_receipts and imported here so the server, watcher, and scheduler all
# read/write the same file (see process_receipts.CONFIG_FILE).
STATE_FILE    = OUT_FOLDER / ".app_state.json"   # crash-safe results/board snapshot

# ── Stall checker config ───────────────────────────────────────────────────────

STALL_TIMEOUT_SECS  = int(os.getenv("STALL_TIMEOUT_SECS",  "180"))  # 3 min
STALL_CHECK_INTERVAL = int(os.getenv("STALL_CHECK_INTERVAL", "60"))   # 1 min

# Largest single uploaded file we'll stage to disk. A receipt photo/PDF is a few
# MB at most; the generous default keeps the whole file out of memory only when
# something pathological (or a mis-targeted upload) arrives. 0 disables the cap.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))  # 100 MiB

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

# Why each quarantined file in REJECTED_FOLDER was moved there, keyed by its
# on-disk name. The folder is the source of truth for *which* files exist; this
# just remembers the human-readable reason to show alongside each one.
_rejected_reasons: dict[str, str] = {}
_rejected_lock    = threading.Lock()

_worker_cancel = threading.Event()

# Reference to the background worker thread + a guard so a crashed worker can be
# revived (by the stall checker, the lifespan startup, or a manual queue nudge).
_worker_thread: threading.Thread | None = None
_worker_start_lock = threading.Lock()

_subscribers: list[Queue] = []
_sub_lock = threading.Lock()

# Item metadata cache — preserves queue item data for stall recovery
_item_cache: dict[str, dict] = {}
_item_cache_lock = threading.Lock()

# Status change timestamps — used by stall checker
_status_timestamps: dict[str, float] = {}
_status_ts_lock = threading.Lock()


# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            # A corrupt/hand-edited config that parses to a non-object (null,
            # a list, a bare number) must not propagate — every caller does
            # cfg.get(...), which would raise on anything but a dict.
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    # Create the config file's own parent (it usually lives in OUT_FOLDER, but
    # may be relocated): writing to a dir that doesn't exist would raise.
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
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


def _processing_settings() -> dict:
    """Current image-processing settings as the UI sees them."""
    return {
        "autorotate":   _pr.AUTOROTATE_ENABLED,
        "autocrop":     _pr.AUTOCROP_ENABLED,
        "grayscale":    _pr.GRAYSCALE_ENABLED,
        "compress":     _pr.COMPRESS_ENABLED,
        "local_ocr":    _pr.LOCAL_OCR_ENABLED,
        "jpeg_quality": _pr.JPEG_QUALITY,
    }


def _apply_processing_config(cfg: dict | None = None) -> dict:
    """Push persisted image-processing settings into the process_receipts module."""
    cfg = cfg if cfg is not None else _load_config()
    if "thinking_enabled" in cfg:
        _pr._thinking_enabled = bool(cfg["thinking_enabled"])
    proc = cfg.get("processing") or {}
    if "autorotate" in proc:
        _pr.AUTOROTATE_ENABLED = bool(proc["autorotate"])
    if "autocrop" in proc:
        _pr.AUTOCROP_ENABLED = bool(proc["autocrop"])
    if "grayscale" in proc:
        _pr.GRAYSCALE_ENABLED = bool(proc["grayscale"])
    if "compress" in proc:
        _pr.COMPRESS_ENABLED = bool(proc["compress"])
    # "local_ocr" is the current key; "paddleocr" is read for backward compat with
    # configs saved before the RapidOCR swap so a prior "disabled" choice sticks.
    if "local_ocr" in proc:
        _pr.LOCAL_OCR_ENABLED = bool(proc["local_ocr"])
    elif "paddleocr" in proc:
        _pr.LOCAL_OCR_ENABLED = bool(proc["paddleocr"])
    if proc.get("jpeg_quality") is not None:
        try:
            _pr.JPEG_QUALITY = max(40, min(95, int(proc["jpeg_quality"])))
        except (TypeError, ValueError):
            pass
    return _processing_settings()


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

def _update_kanban(filename: str, status: str, data, model: str = "",
                   steps: list | None = None) -> None:
    safe = _safe_receipt_data(data)
    # For mid-processing statuses (ocr, distilling) data is None; attach the
    # current step-log snapshot so reconnecting clients can see live progress.
    if steps is not None:
        safe["_steps"] = steps
    with _kanban_lock:
        _kanban[filename] = {"status": status, "data": safe, "model": model}
    with _status_ts_lock:
        _status_timestamps[filename] = time.time()


def _is_active_in_kanban(filename: str) -> bool:
    """True if the file is already queued or being processed — skip re-queuing."""
    with _kanban_lock:
        entry = _kanban.get(filename, {})
    return entry.get("status") in ("queued", "ocr", "distilling")


def _safe_receipt_data(data) -> dict:
    """Serialize receipt data for SSE — strip non-serialisable internal fields."""
    if not data:
        return {}
    out = {}
    for k in ("date", "vendor", "amount", "category", "job_name", "job_number",
              "expense_description", "summary", "ai_summary", "_flag", "_category",
              "_new_filename", "_file", "_compressed_file", "flags", "_confidence", "_error",
              "_amount_verified", "_proc_seconds", "_ocr_seconds",
              "_distill_seconds", "_ocr_engine", "_steps", "_field_boxes",
              "_review_required", "_approved"):
        if k in data:
            out[k] = data[k]
    return out


def _cache_item(item: dict) -> None:
    """Cache queue item data for stall recovery."""
    with _item_cache_lock:
        _item_cache[item["filename"]] = item


# ── State persistence ──────────────────────────────────────────────────────────
# Completed/failed receipts and the last-used form context are snapshotted to
# disk so a server restart doesn't wipe an already-processed batch. Queued and
# in-flight items are intentionally not persisted — their worker is gone.

def _persist_state() -> None:
    try:
        with _results_lock:
            results_copy = copy.deepcopy(_results)
            context_copy = dict(_last_context)
        with _kanban_lock:
            kanban_copy = {
                fn: dict(v) for fn, v in _kanban.items()
                if v.get("status") in ("done", "failed")
            }
        payload = {
            "results":      results_copy,
            "kanban":       kanban_copy,
            "last_context": context_copy,
        }
        OUT_FOLDER.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.replace(STATE_FILE)
    except Exception as exc:
        print(f"[state] persist failed: {exc}")


def _restore_state() -> None:
    try:
        if not STATE_FILE.exists():
            return
        payload = json.loads(STATE_FILE.read_text())
    except Exception as exc:
        print(f"[state] restore failed: {exc}")
        return
    if not isinstance(payload, dict):
        return

    results = payload.get("results")
    if isinstance(results, list):
        with _results_lock:
            _results.extend(r for r in results if isinstance(r, dict))

    kanban = payload.get("kanban")
    if isinstance(kanban, dict):
        with _kanban_lock:
            for fn, entry in kanban.items():
                if isinstance(entry, dict) and entry.get("status") in ("done", "failed"):
                    _kanban[fn] = entry

    ctx = payload.get("last_context")
    if isinstance(ctx, dict):
        with _results_lock:
            _last_context.update({
                k: ctx[k] for k in ("employee", "job_name", "job_number")
                if isinstance(ctx.get(k), str)
            })

    with _results_lock:
        n = len(_results)
    if n:
        print(f"[state] Restored {n} completed receipt(s) from previous session")


# ── Background worker ──────────────────────────────────────────────────────────

def _ensure_worker_alive() -> bool:
    """(Re)start the background worker thread if it isn't running.

    A single unhandled error used to kill the worker for good, leaving items stuck
    in the queue until a full container restart. The worker loop now self-heals, and
    this watchdog brings it back if the thread ever dies anyway. Returns True when a
    new worker thread was started.
    """
    global _worker_thread
    if _worker_cancel.is_set():
        return False
    with _worker_start_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=_run_worker, daemon=True)
        _worker_thread.start()
        return True


def _run_worker() -> None:
    """Drain the work queue forever, surviving per-batch errors."""
    while not _worker_cancel.is_set():
        try:
            if not _drain_once():
                time.sleep(0.4)
        except Exception as exc:
            _broadcast({"type": "log", "message": f"[worker] recovered from error: {exc}"})
            time.sleep(1)


def _receipts_output_dir() -> Path:
    """Completed receipts are grouped into a short, dated subfolder under the
    completed-receipts folder — e.g. ``receipts/Processed_2026-06-13`` — so each
    day's processed receipts stay tidily together instead of piling into one flat
    directory. The subfolder is created on demand and reused for the day."""
    d = IMAGES_FOLDER / f"Processed_{date.today().isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drain_once() -> bool:
    """Process one batch from the queue. Returns False when the queue was empty."""
    with _work_lock:
        batch = list(_work_queue) if _work_queue else []
        if batch:
            _work_queue.clear()

    if not batch:
        return False

    _broadcast({"type": "log", "message": f"[worker] Processing {len(batch)} receipt(s)…"})
    client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio",
                    timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES)

    futures_map: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_pr.MAX_PARALLEL_REQUESTS or None) as ex:
        for item in batch:
            if _worker_cancel.is_set():
                break
            fname = item["filename"]
            path  = Path(item["path"])

            # Create a fresh per-item step log for every receipt.
            step_log: list = []
            item["_steps"] = step_log

            # Move to the processing folder so in-flight and failed images have a
            # known, visible home. Files already in PROCESSING_FOLDER (e.g. a retry)
            # are left in place; only files arriving from upload staging or intake
            # are moved.
            PROCESSING_FOLDER.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.parent.resolve() != PROCESSING_FOLDER.resolve():
                dest = PROCESSING_FOLDER / fname
                if dest.exists() and dest.resolve() != path.resolve():
                    stem = Path(fname).stem
                    ext  = path.suffix or ".jpg"
                    ts   = f"{int(time.time() * 1000) % 1_000_000:06d}"
                    dest = PROCESSING_FOLDER / f"{stem}_{ts}{ext}"
                try:
                    shutil.move(str(path), str(dest))
                    path = dest
                    item["path"] = str(path)
                except Exception as _mv_err:
                    _broadcast({"type": "log",
                                "message": f"[worker] could not move {fname} to processing: {_mv_err}"})
            _cache_item(item)

            # Compression is DEFERRED to spreadsheet generation (see
            # process_receipts.compress_result_images), so extraction reads the
            # full-resolution stored file exactly as the worker saved it — no
            # suffix rewrite, no stale-path hand-off.
            IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)

            def make_cb(fn: str, steps: list):
                def cb(status: str, data=None, model: str = "") -> None:
                    steps_now = list(steps)
                    _update_kanban(fn, status, data, model, steps_now)
                    _broadcast({
                        "type":     "kanban_update",
                        "filename": fn,
                        "status":   status,
                        "data":     _safe_receipt_data(data),
                        "model":    model,
                        "steps":    steps_now,
                    })
                return cb

            future = ex.submit(
                _extract_receipt_with_status, client, path, make_cb(fname, step_log), step_log,
            )
            futures_map[future] = item

        for future in concurrent.futures.as_completed(futures_map):
            if _worker_cancel.is_set():
                break
            item  = futures_map[future]
            fname = item["filename"]
            path  = Path(item["path"])
            steps = item.get("_steps", [])
            try:
                data = future.result()
            except Exception as exc:
                data = None
                _broadcast({"type": "log", "message": f"[worker] ERROR {fname}: {exc}"})

            if data is None or _is_low_confidence(data) or _has_ocr_flag(data):
                fail_reason = _get_fail_reason(data)
                partial: dict = {}
                if data is not None:
                    partial = dict(data)
                    partial["_flag"]   = "Manual review required — incomplete extraction"
                    partial["_error"]  = fail_reason
                    partial["_file"]   = fname
                    conf, _            = _compute_confidence(data)
                    partial["_confidence"] = conf
                else:
                    partial["_file"] = fname
                # Track the compressed filename so the UI can find the image even
                # when the extension changed (e.g. photo.webp → photo.jpg).
                compressed_name = path.name
                if compressed_name != fname:
                    partial["_compressed_file"] = compressed_name
                # Always attach the step log so failed cards show what was tried
                if "_steps" not in partial:
                    partial["_steps"] = steps
                _update_kanban(fname, "failed", partial)
                _broadcast({
                    "type":     "kanban_update",
                    "filename": fname,
                    "status":   "failed",
                    "data":     _safe_receipt_data(partial),
                    "model":    "",
                    "error":    fail_reason,
                    "steps":    partial.get("_steps", []),
                })
                continue

            category = classify_category(data)
            data["_category"]  = category
            data["job_name"]   = item.get("job_name") or DEFAULT_JOB_NAME
            data["job_number"] = item.get("job_number") or DEFAULT_JOB_NUMBER
            audit_flag = audit_amount(data, data.get("_raw_ocr") or "")
            flags = _pr._normalize_flags(data.get("flags") or [])
            data["flags"] = flags  # ensure normalised form is stored
            if flags and not data.get("_flag"):
                data["_flag"] = flags[0].get("flag", "")
            if audit_flag and not data.get("_flag"):
                data["_flag"] = audit_flag
            conf, _ = _compute_confidence(data)
            data["_confidence"] = conf
            # Auto-flag for review when extraction has issues or low confidence
            if not data.get("_review_required"):
                data["_review_required"] = bool(data.get("_flag")) or (conf is not None and conf < 60)
            data.setdefault("_approved", False)

            # Append classify and audit steps to the log
            _pr._append_step(steps, "classify", "Classify", f"category: {category}")
            if data.get("_amount_verified"):
                _pr._append_step(steps, "audit", "Audit",
                                 f"${data.get('amount', 0):.2f} verified against OCR text")
            elif audit_flag:
                _pr._append_step(steps, "audit", "Audit",
                                 audit_flag, ok=False)

            # Finalize step list in data (supersedes the snapshot set by _finish)
            data["_steps"] = list(steps)

            # item["path"] points at the full-resolution file extraction read; it
            # is renamed into a dated subfolder of IMAGES_FOLDER here and compressed
            # later, when the spreadsheet is generated.
            dest_dir = _receipts_output_dir()
            path = Path(item["path"])
            final_path = rename_receipt_image(path, data, category, dest_dir)
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
                "steps":    data.get("_steps", []),
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
    _persist_state()
    _broadcast({"type": "batch_done", "completed": n_done, "pending": n_pending})
    return True


# ── Unsupported / invalid intake files ─────────────────────────────────────────

def _reject_intake_file(p: Path, reason: str) -> dict | None:
    """Move an unreadable intake file out of the way into REJECTED_FOLDER and
    announce it so the user can review or delete it.

    Returns the quarantined item's metadata (or ``None`` if the move failed). The
    file is given a collision-safe name; its reason is remembered and a ``rejected``
    event is broadcast so the UI can surface a notification with a delete button.
    """
    try:
        REJECTED_FOLDER.mkdir(parents=True, exist_ok=True)
        dest = REJECTED_FOLDER / p.name
        if dest.exists():
            ts   = f"{int(time.time() * 1000) % 1_000_000:06d}"
            dest = REJECTED_FOLDER / f"{p.stem}_{ts}{p.suffix}"
        shutil.move(str(p), str(dest))
    except Exception as exc:
        _broadcast({"type": "log", "message": f"[intake] could not quarantine {p.name}: {exc}"})
        return None

    with _rejected_lock:
        _rejected_reasons[dest.name] = reason

    try:
        st = dest.stat()
        size, modified = st.st_size, datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        size, modified = 0, ""

    item = {
        "name":          dest.name,
        "original_name": p.name,
        "reason":        reason,
        "ext":           p.suffix.lower() or "(none)",
        "size":          size,
        "modified":      modified,
        "path":          str(dest.resolve()),
    }
    _broadcast({"type": "rejected", "item": item})
    _broadcast({"type": "log",
                "message": f"[intake] {p.name} moved to '{REJECTED_FOLDER.name}' — {reason}"})
    return item


def _rejected_items() -> list[dict]:
    """Current contents of REJECTED_FOLDER as display records (newest first)."""
    items: list[dict] = []
    if not REJECTED_FOLDER.exists():
        return items
    for p in REJECTED_FOLDER.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
            size, modified, mtime = (
                st.st_size,
                datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                st.st_mtime,
            )
        except OSError:
            size, modified, mtime = 0, "", 0
        with _rejected_lock:
            reason = _rejected_reasons.get(p.name, "Unsupported file format")
        items.append({
            "name":     p.name,
            "reason":   reason,
            "ext":      p.suffix.lower() or "(none)",
            "size":     size,
            "modified": modified,
            "path":     str(p.resolve()),
            "_mtime":   mtime,
        })
    items.sort(key=lambda i: i.pop("_mtime"), reverse=True)
    return items


# ── Background watcher ─────────────────────────────────────────────────────────

def _run_watcher() -> None:
    """Poll INTAKE_FOLDER every 5 seconds and auto-queue new image/PDF files."""
    while not _worker_cancel.is_set():
        try:
            if INTAKE_FOLDER.exists():
                for p in sorted(INTAKE_FOLDER.iterdir()):
                    if not p.is_file() or p.name.startswith("."):
                        continue
                    suffix = p.suffix.lower()

                    with _seen_lock:
                        if p.name in _seen_intake:
                            continue
                        _seen_intake.add(p.name)

                    # Zip archives: expand into member images/PDFs, queue each, and
                    # move the archive out of intake (extract → import → clean up).
                    if suffix in ARCHIVE_EXTENSIONS:
                        try:
                            members = extract_archive(p, INTAKE_FOLDER / f"_zip_{p.stem}")
                            for m in members:
                                with _seen_lock:
                                    _seen_intake.add(m.name)
                                item = {
                                    "filename":   m.name,
                                    "path":       str(m),
                                    "employee":   _last_context.get("employee", "Employee"),
                                    "job_name":   _last_context.get("job_name", ""),
                                    "job_number": _last_context.get("job_number", ""),
                                }
                                _cache_item(item)
                                with _work_lock:
                                    _work_queue.append(item)
                                _update_kanban(m.name, "queued", None)
                                _broadcast({
                                    "type":     "kanban_update",
                                    "filename": m.name,
                                    "status":   "queued",
                                    "data":     {},
                                    "model":    "",
                                })
                            IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(p), str(IMAGES_FOLDER / p.name))
                            _broadcast({
                                "type":    "log",
                                "message": f"[watcher] zip {p.name} → {len(members)} file(s) queued",
                            })
                        except Exception as exc:
                            _broadcast({
                                "type":    "log",
                                "message": f"[watcher] zip error {p.name}: {exc}",
                            })
                        continue

                    # Anything that isn't an image or PDF can't be processed —
                    # quarantine it and notify so the user can check or delete it.
                    if suffix not in SUPPORTED_EXTENSIONS:
                        reason = (f"Unsupported file type '{suffix or '(none)'}' — "
                                  "only images and PDFs can be processed.")
                        _reject_intake_file(p, reason)
                        continue

                    if suffix in IMAGE_EXTENSIONS:
                        item = {
                            "filename":   p.name,
                            "path":       str(p),
                            "employee":   _last_context.get("employee", "Employee"),
                            "job_name":   _last_context.get("job_name", ""),
                            "job_number": _last_context.get("job_number", ""),
                        }
                        _cache_item(item)
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
                                _cache_item(item)
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


# ── Stall checker ─────────────────────────────────────────────────────────────

def _run_stall_checker() -> None:
    """Periodically detect items stuck in ocr/distilling and re-queue them."""
    while not _worker_cancel.is_set():
        _worker_cancel.wait(timeout=STALL_CHECK_INTERVAL)
        if _worker_cancel.is_set():
            break

        # Revive the worker if it died, so a crashed thread never strands the queue.
        if _ensure_worker_alive():
            _broadcast({"type": "log", "message": "[watchdog] worker thread restarted"})

        now = time.time()
        stalled: list[str] = []

        with _kanban_lock:
            for fname, entry in list(_kanban.items()):
                if entry["status"] not in ("ocr", "distilling"):
                    continue
                with _status_ts_lock:
                    ts = _status_timestamps.get(fname, now)
                if now - ts > STALL_TIMEOUT_SECS:
                    stalled.append(fname)

        for fname in stalled:
            with _item_cache_lock:
                cached = _item_cache.get(fname)

            if not cached:
                # Try processing folder (exact + fuzzy extension match)
                stem = Path(fname).stem
                for name in [fname] + [stem + ext for ext in (".jpg", ".jpeg", ".png", ".webp")]:
                    candidate = PROCESSING_FOLDER / name
                    if candidate.exists():
                        cached = {
                            "filename":   fname,
                            "path":       str(candidate),
                            "employee":   _last_context.get("employee", "Employee"),
                            "job_name":   _last_context.get("job_name", ""),
                            "job_number": _last_context.get("job_number", ""),
                        }
                        break

            if not cached:
                # Last resort: look in intake folder
                candidate = INTAKE_FOLDER / fname
                if candidate.exists():
                    cached = {
                        "filename":   fname,
                        "path":       str(candidate),
                        "employee":   _last_context.get("employee", "Employee"),
                        "job_name":   _last_context.get("job_name", ""),
                        "job_number": _last_context.get("job_number", ""),
                    }

            if not cached:
                _update_kanban(fname, "failed", {"_error": "Stalled — image path unavailable for retry"})
                _broadcast({
                    "type": "kanban_update", "filename": fname,
                    "status": "failed",
                    "data": {"_error": "Stalled — image path unavailable for retry"},
                    "model": "",
                })
                _broadcast({"type": "log", "message": f"[stall] {fname} stuck with no recoverable path — marked failed"})
                continue

            item = dict(cached)
            with _work_lock:
                _work_queue.appendleft(item)
            _update_kanban(fname, "queued", None)
            _broadcast({
                "type": "kanban_update", "filename": fname,
                "status": "queued", "data": {}, "model": "",
            })
            _broadcast({"type": "stall_recovered", "filename": fname})
            _broadcast({"type": "log", "message": f"[stall] {fname} was stuck — re-queued automatically"})


# ── Lifespan ───────────────────────────────────────────────────────────────────

# ── Scheduled export ───────────────────────────────────────────────────────────

# Created inside lifespan, not at import: an asyncio.Event binds to the running
# loop on first use, so a module-level one breaks when the app is (re)started on
# a different loop (e.g. successive TestClient instances → "bound to a different
# event loop").
_schedule_wakeup: asyncio.Event | None = None


def _get_schedule_config() -> scheduler.ScheduleConfig:
    try:
        sched = dict(_load_config().get("schedule") or {})
        # The Dropbox token is a secret kept out of the synced config file —
        # overlay it (falling back to a legacy config value or the env var).
        token = app_secrets.get_secret(
            "dropbox_token", "schedule", "dropbox_token", "SCHEDULE_DROPBOX_TOKEN")
        if token:
            sched["dropbox_token"] = token
        return scheduler.parse_schedule(sched)
    except scheduler.ScheduleError:
        return scheduler.ScheduleConfig(enabled=False)


def _schedule_results_snapshot() -> tuple[list[dict], str]:
    with _results_lock:
        results = copy.deepcopy(_results)
        employee = _last_context.get("employee", "Employee")
    _detect_duplicates(results)
    return results, employee


def _on_schedule_result(report: dict) -> None:
    cfg = _load_config()
    cfg.setdefault("schedule", {})["last_run"] = report
    _save_config(cfg)
    if report.get("ok"):
        msg = (f"Scheduled export complete: {report.get('filename')} "
               f"({', '.join(report.get('delivered', []))})")
    else:
        msg = f"Scheduled export failed: {report.get('error')}"
    _broadcast({"type": "log", "message": msg})
    print(f"[schedule] {msg}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _schedule_wakeup
    _schedule_wakeup = asyncio.Event()   # bind to this app's running loop
    _restore_state()
    _apply_processing_config()   # restore UI-saved auto-crop / compress / local-OCR settings
    # Session-start housekeeping: sweep away empty, non-active orphaned folders
    # (collapsed dated/job subfolders, leftover _pdf_*/_upload_* staging) left
    # behind by a previous run before the new session starts handing out work.
    try:
        removed = _run_empty_dir_cleanup()
        if removed:
            print(f"[startup] cleaned {len(removed)} empty orphaned folder(s)")
    except Exception as exc:
        print(f"[startup] empty-folder cleanup skipped: {exc}")
    threading.Thread(target=initialize_models,  daemon=True).start()
    _ensure_worker_alive()       # start the self-healing worker thread
    threading.Thread(target=_run_watcher,       daemon=True).start()
    threading.Thread(target=_run_stall_checker, daemon=True).start()
    sched_task = asyncio.create_task(scheduler.run_scheduler(
        _get_schedule_config, _schedule_results_snapshot,
        _on_schedule_result, _schedule_wakeup,
    ))
    yield
    sched_task.cancel()
    _worker_cancel.set()


app = FastAPI(title="Receipt Processor", lifespan=lifespan)


# ── Optional shared-secret auth ─────────────────────────────────────────────────
# The app is a local-network tool with no login. When APP_AUTH_TOKEN is set,
# every request must present it (via the X-Auth-Token header, an auth_token
# cookie, or a ?token= query param — opening the page once with ?token= drops the
# cookie so the SPA's fetch/SSE calls authenticate automatically). When the env
# var is unset the gate is a no-op, preserving the open localhost behaviour.
# The token is read per-request so it can be configured without a code change.
_AUTH_EXEMPT_PATHS = {"/", "/manifest.json", "/icon.svg"}


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    token = os.getenv("APP_AUTH_TOKEN", "")
    if not token:
        return await call_next(request)

    import secrets as _secrets
    path = request.url.path

    # Always let the shell page + its icons load so the token can be supplied
    # via ?token= on the URL; the page's API/SSE calls are still protected.
    if path in _AUTH_EXEMPT_PATHS:
        resp = await call_next(request)
        supplied = request.query_params.get("token", "")
        if supplied and _secrets.compare_digest(supplied, token):
            # Persist the token so the SPA's cookie-only requests (receipt images,
            # the SSE stream) keep authenticating across reloads, browser restarts,
            # and PWA relaunches — the old session cookie was dropped on restart,
            # which over a tunnel left the shell loading but every image/API call
            # 401-ing. SameSite=Lax (not Strict) so following a link/bookmark to the
            # app still sends it; Secure only when actually served over HTTPS so a
            # plain-HTTP LAN install can still set the cookie.
            https = (request.url.scheme == "https"
                     or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
            resp.set_cookie("auth_token", token, max_age=31_536_000, path="/",
                            httponly=True, samesite="lax", secure=https)
        return resp

    supplied = (
        request.headers.get("X-Auth-Token", "")
        or request.cookies.get("auth_token", "")
        or request.query_params.get("token", "")
    )
    if not (supplied and _secrets.compare_digest(supplied, token)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


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
    skipped: list[str] = []

    # Stage every uploaded file to disk, expanding any .zip into its member
    # images/PDFs and discarding the archive itself (extract → import → clean up).
    staged: list[Path] = []
    for f in files:
        name = Path(f.filename or "receipt").name   # basename only → no path traversal
        size = getattr(f, "size", None)
        if MAX_UPLOAD_BYTES and size is not None and size > MAX_UPLOAD_BYTES:
            skipped.append(name)
            _broadcast({"type": "log",
                        "message": f"[upload] {name}: skipped — exceeds "
                                   f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit"})
            continue
        payload = await f.read()
        if not payload:
            skipped.append(name)            # empty/zero-byte file — nothing to process
            continue
        dest = tmp_dir / name
        with open(dest, "wb") as fh:
            fh.write(payload)
        if dest.suffix.lower() in ARCHIVE_EXTENSIONS:
            members = extract_archive(dest, tmp_dir)
            dest.unlink(missing_ok=True)
            if members:
                staged.extend(members)
            else:
                _broadcast({"type": "log",
                            "message": f"[upload] {dest.name}: no images or PDFs found in archive"})
        else:
            staged.append(dest)

    # Dispatch each staged image/PDF into the work queue.
    for dest in staged:
        fname  = dest.name
        suffix = dest.suffix.lower()

        if suffix in PDF_EXTENSIONS:
            try:
                pages = pdf_to_images(dest, tmp_dir / f"_pdf_{dest.stem}")
                dest.unlink(missing_ok=True)
                for page_path in pages:
                    if _is_active_in_kanban(page_path.name):
                        skipped.append(page_path.name)
                        continue
                    with _seen_lock:
                        _seen_intake.add(page_path.name)
                    item = {
                        "filename":   page_path.name,
                        "path":       str(page_path),
                        "employee":   employee or "Employee",
                        "job_name":   job_name,
                        "job_number": job_number,
                    }
                    _cache_item(item)
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
            if _is_active_in_kanban(dest.name):
                skipped.append(dest.name)
                continue
            with _seen_lock:
                _seen_intake.add(dest.name)
            item = {
                "filename":   dest.name,
                "path":       str(dest),
                "employee":   employee or "Employee",
                "job_name":   job_name,
                "job_number": job_number,
            }
            _cache_item(item)
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

    return JSONResponse({"queued": queued, "skipped": skipped, "pending": n_pending})


@app.post("/queue/add-intake")
async def queue_add_intake(
    employee:   str = Form("Employee"),
    job_name:   str = Form(""),
    job_number: str = Form(""),
):
    """Enqueue all unprocessed files currently in the intake folder."""
    queued: list[str] = []
    skipped: list[str] = []
    rejected: list[dict] = []

    try:
        all_files = sorted(
            p for p in INTAKE_FOLDER.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    except Exception:
        all_files = []

    # Anything that isn't an image, PDF, or archive can't be processed — quarantine
    # it and notify so the user can check or delete it (same handling as the
    # watcher). Zips are expanded here into their member images/PDFs, the members
    # join the processing list, and the archive is moved out of intake.
    files_in_intake = []
    for p in all_files:
        suffix = p.suffix.lower()
        if suffix in ARCHIVE_EXTENSIONS:
            members = extract_archive(p, INTAKE_FOLDER / f"_zip_{p.stem}")
            files_in_intake.extend(members)
            try:
                IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(IMAGES_FOLDER / p.name))
            except Exception:
                p.unlink(missing_ok=True)
            _broadcast({"type": "log",
                        "message": f"[intake] {p.name} → {len(members)} file(s) extracted"})
        elif suffix in SUPPORTED_EXTENSIONS:
            files_in_intake.append(p)
        else:
            reason = (f"Unsupported file type '{suffix or '(none)'}' — "
                      "only images, PDFs, and .zip archives can be processed.")
            item = _reject_intake_file(p, reason)
            if item:
                rejected.append(item)

    for p in files_in_intake:
        with _seen_lock:
            if p.name in _seen_intake:
                continue
            _seen_intake.add(p.name)

        if _is_active_in_kanban(p.name):
            skipped.append(p.name)
            continue

        suffix = p.suffix.lower()

        if suffix in IMAGE_EXTENSIONS:
            item = {
                "filename":   p.name,
                "path":       str(p),
                "employee":   employee or "Employee",
                "job_name":   job_name,
                "job_number": job_number,
            }
            _cache_item(item)
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
                    if _is_active_in_kanban(page_path.name):
                        skipped.append(page_path.name)
                        continue
                    with _seen_lock:
                        _seen_intake.add(page_path.name)
                    item = {
                        "filename":   page_path.name,
                        "path":       str(page_path),
                        "employee":   employee or "Employee",
                        "job_name":   job_name,
                        "job_number": job_number,
                    }
                    _cache_item(item)
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

    return JSONResponse({"queued": queued, "skipped": skipped,
                         "rejected": rejected, "pending": n_pending})


@app.post("/queue/cancel")
async def queue_cancel():
    """Signal cancellation, drain the pending queue, then re-arm for future jobs."""
    _worker_cancel.set()
    with _work_lock:
        cleared = len(_work_queue)
        _work_queue.clear()
    _worker_cancel.clear()   # allow future processing
    _ensure_worker_alive()   # revive the worker if the cancel toggle stopped it
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
    """Serve a receipt image by filename for UI previews.

    Searches completed-receipts, processing, and intake folders.  Falls back to a
    fuzzy extension match so a card whose original .png was compressed to .jpg can
    still show its preview image.
    """
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse({"error": "invalid"}, status_code=400)
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif",  ".webp": "image/webp", ".bmp": "image/bmp",
    }
    search: list[Path] = [IMAGES_FOLDER, PROCESSING_FOLDER, INTAKE_FOLDER]
    try:
        search += [d for d in IMAGES_FOLDER.iterdir() if d.is_dir()]
    except Exception:
        pass
    try:
        search += [d for d in PROCESSING_FOLDER.iterdir() if d.is_dir()]
    except Exception:
        pass

    _roots = [IMAGES_FOLDER.resolve(), PROCESSING_FOLDER.resolve(), INTAKE_FOLDER.resolve()]

    def _serveable(p: Path) -> bool:
        # Must be a real file (never a symlink) that resolves to somewhere
        # inside our working folders.  Blocks a planted symlink — e.g.
        # ``photo.jpg`` → ``/etc/passwd`` — from turning this preview endpoint
        # into an arbitrary-file read.
        try:
            if p.is_symlink() or not p.is_file():
                return False
            rp = p.resolve()
            return any(rp == root or root in rp.parents for root in _roots)
        except OSError:
            return False

    # Exact name match
    for folder in search:
        p = folder / filename
        if _serveable(p):
            mt = ext_map.get(p.suffix.lower(), "image/jpeg")
            return FileResponse(str(p), media_type=mt)
    # Fuzzy extension match — handles .png → .jpg renames after compression
    stem = Path(filename).stem
    for folder in search:
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
            p = folder / (stem + ext)
            if _serveable(p):
                mt = ext_map.get(ext, "image/jpeg")
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


@app.get("/intake/rejected")
async def list_rejected_files():
    """List intake files that were quarantined because they aren't a supported
    image/PDF — with the reason, size, and full on-disk location for each."""
    items = _rejected_items()
    return JSONResponse({
        "ok":     True,
        "count":  len(items),
        "folder": str(REJECTED_FOLDER.resolve()),
        "items":  items,
    })


class RejectedDeleteRequest(BaseModel):
    name: str = ""


@app.post("/intake/rejected/delete")
async def delete_rejected_file(body: RejectedDeleteRequest):
    """Delete one quarantined file by name. Guards against path traversal and
    only ever unlinks a file that resolves inside REJECTED_FOLDER."""
    name = (body.name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"ok": False, "error": "invalid name"}, status_code=400)

    target = (REJECTED_FOLDER / name).resolve()
    try:
        target.relative_to(REJECTED_FOLDER.resolve())
    except ValueError:
        return JSONResponse({"ok": False, "error": "outside quarantine folder"}, status_code=400)

    if not target.is_file():
        with _rejected_lock:
            _rejected_reasons.pop(name, None)
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    try:
        target.unlink()
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    with _rejected_lock:
        _rejected_reasons.pop(name, None)
    _broadcast({"type": "log", "message": f"[intake] deleted quarantined file {name}"})
    return JSONResponse({"ok": True, "deleted": name})


@app.post("/intake/rejected/delete-all")
async def delete_all_rejected_files():
    """Delete every quarantined file at once."""
    deleted: list[str] = []
    errors: list[dict] = []
    for item in _rejected_items():
        p = (REJECTED_FOLDER / item["name"]).resolve()
        try:
            p.relative_to(REJECTED_FOLDER.resolve())
        except ValueError:
            continue
        try:
            if p.is_file():
                p.unlink()
                deleted.append(item["name"])
        except OSError as exc:
            errors.append({"name": item["name"], "error": str(exc)})
    with _rejected_lock:
        for name in deleted:
            _rejected_reasons.pop(name, None)
    return JSONResponse({"ok": True, "count": len(deleted), "deleted": deleted, "errors": errors})


# ── Spreadsheet generation ─────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    exclude_filenames: list[str] = []
    employee: str = ""
    job_name: str = ""
    job_number: str = ""


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
    filename:        str
    vendor:          str = ""
    date:            str = ""
    amount:          str = ""
    category:        str = "misc"
    job_name:        str = ""
    job_number:      str = ""
    summary:         str = ""
    review_required: bool = False
    approved:        bool = False


@app.post("/results/add-manual")
async def add_manual_result(body: ManualReceiptRequest):
    """Manually add or update a receipt result (for failed/partial extractions)."""
    try:
        amt = float(body.amount) if body.amount.strip() else 0.0
    except ValueError:
        amt = 0.0
    # Guard against "inf"/"nan", which parse as floats but serialise to invalid
    # JSON and corrupt every total/average downstream.
    if not math.isfinite(amt):
        amt = 0.0

    data: dict = {
        "vendor":           body.vendor.strip() or "Unknown",
        "date":             body.date.strip(),
        "amount":           amt,
        "category":         body.category or "misc",
        "_category":        body.category or "misc",
        "job_name":         body.job_name.strip() or _last_context.get("job_name") or DEFAULT_JOB_NAME,
        "job_number":       body.job_number.strip() or _last_context.get("job_number") or DEFAULT_JOB_NUMBER,
        "ai_summary":       body.summary.strip(),
        "_flag":            "Manual entry",
        "_file":            body.filename,
        "_confidence":      None,
        "_review_required": body.review_required,
        "_approved":        body.approved,
    }

    with _results_lock:
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                # Preserve fields not managed by this form
                preserved = {k: r[k] for k in ("_new_filename", "_compressed_file",
                             "_image_path", "_proc_seconds", "_ocr_seconds",
                             "_distill_seconds", "_ocr_engine", "_steps", "_raw_ocr")
                             if k in r}
                # Reviewing/approving an already-extracted receipt must not
                # rewrite it as a manual entry: keep its flag and extraction
                # confidence, and only drop the OCR amount cross-check when
                # the amount itself was edited.
                preserved["_flag"] = r.get("_flag", "")
                preserved["_confidence"] = r.get("_confidence")
                try:
                    amount_changed = abs(float(r.get("amount") or 0) - amt) > 0.005
                except (TypeError, ValueError):
                    amount_changed = True
                if amount_changed:
                    r.pop("_amount_verified", None)
                r.update(data)
                r.update(preserved)
                data = dict(r)
                break
        else:
            _results.append(data)

    _update_kanban(body.filename, "done", data)
    _persist_state()
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

    # Approval gate — when enabled in settings, every receipt in the batch must
    # have been reviewed and approved before a spreadsheet can be generated.
    if _load_config().get("require_approval"):
        unapproved = sum(1 for r in results_copy if not r.get("_approved"))
        if unapproved:
            return JSONResponse(
                {"ok": False,
                 "error": f"{unapproved} of {len(results_copy)} receipt(s) have not been "
                          "reviewed and approved. Approve them on the board (or turn off "
                          "'Require review & approval') and try again."},
                status_code=409,
            )

    # Deferred compression — shrink the images for the receipts that will land in
    # this report now, at export time. results_copy still holds the live _results
    # dicts here, so this updates the stored paths in place (the output folder and
    # re-generation both stay consistent); already-compressed records skip. Run it
    # off the event loop since it does PIL disk I/O.
    def _compress_live():
        with _results_lock:
            compress_result_images(
                results_copy,
                log=lambda m: _broadcast({"type": "log", "message": m}),
            )
    await asyncio.get_event_loop().run_in_executor(None, _compress_live)

    # Deep-copy so we don't mutate _results; re-detect duplicates on filtered set
    # so excluded items don't leave stale duplicate flags on the remaining receipts
    results_copy = copy.deepcopy(results_copy)
    _dup_kw = ("potential duplicate", "duplicate of")
    for r in results_copy:
        flag = (r.get("_flag") or "").lower()
        if any(kw in flag for kw in _dup_kw):
            r["_flag"] = ""
    _detect_duplicates(results_copy)

    employee = (body.employee or "").strip()
    if employee:
        with _results_lock:
            _last_context["employee"] = employee
    else:
        employee = (
            _last_context.get("employee")
            or _load_config().get("default_employee")
            or "Employee"
        )

    # Job name / number are captured live at generation time too. Any receipt
    # still missing one is filled with the value currently entered on the board,
    # so a field added (or corrected) after an earlier generation shows up on
    # regen. Values already set per-receipt are kept — only blanks are filled.
    job_name = (body.job_name or "").strip()
    job_number = (body.job_number or "").strip()
    if job_name or job_number:
        with _results_lock:
            if job_name:
                _last_context["job_name"] = job_name
            if job_number:
                _last_context["job_number"] = job_number
        for r in results_copy:
            if job_name and not str(r.get("job_name") or "").strip():
                r["job_name"] = job_name
            if job_number and not str(r.get("job_number") or "").strip():
                r["job_number"] = job_number

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


# DEPRECATED legacy alias — the frontend no longer calls this; kept one release
# for external scripts. Remove after 2026-12.
@app.post("/generate-spreadsheet/{job_id}")
async def make_spreadsheet_legacy(job_id: str):
    return await make_spreadsheet()


# ── Analytics / insights ───────────────────────────────────────────────────────

def _compute_stats(results: list[dict]) -> dict:
    """Aggregate spend stats for the insights dashboard. Pure function."""
    def _amt(r) -> float:
        try:
            return round(float(r.get("amount") or 0), 2)
        except (TypeError, ValueError):
            return 0.0

    total = 0.0
    by_category: dict[str, dict] = {}
    by_vendor: dict[str, dict] = {}
    by_day: dict[str, float] = {}
    by_day_count: dict[str, int] = {}
    flagged = 0
    verified = 0
    proc_times: list[float] = []

    for r in results:
        amt = _amt(r)
        total += amt
        cat = (r.get("_category") or r.get("category") or "misc").lower()
        c = by_category.setdefault(cat, {"count": 0, "total": 0.0})
        c["count"] += 1
        c["total"] = round(c["total"] + amt, 2)

        vendor = (r.get("vendor") or "Unknown").strip() or "Unknown"
        v = by_vendor.setdefault(vendor, {"count": 0, "total": 0.0})
        v["count"] += 1
        v["total"] = round(v["total"] + amt, 2)

        d = sort_key_for_receipt(r)
        if d != date.max:
            key = d.isoformat()
            by_day[key] = round(by_day.get(key, 0.0) + amt, 2)
            by_day_count[key] = by_day_count.get(key, 0) + 1

        if r.get("_flag"):
            flagged += 1
        if r.get("_amount_verified"):
            verified += 1
        try:
            secs = float(r.get("_proc_seconds") or 0)
            if secs > 0:
                proc_times.append(secs)
        except (TypeError, ValueError):
            pass

    top_vendors = sorted(
        ({"vendor": k, **v} for k, v in by_vendor.items()),
        key=lambda x: -x["total"],
    )[:8]
    # Timeline carries per-day count and a running cumulative so the dashboard's
    # spend-over-time chart can show more than bare daily bars.
    timeline: list[dict] = []
    running = 0.0
    for k in sorted(by_day):
        running = round(running + by_day[k], 2)
        timeline.append({
            "date":       k,
            "total":      by_day[k],
            "count":      by_day_count.get(k, 0),
            "cumulative": running,
        })
    dated_total = round(sum(by_day.values()), 2)
    peak = max(timeline, key=lambda t: t["total"]) if timeline else None

    return {
        "count":        len(results),
        "total":        round(total, 2),
        "average":      round(total / len(results), 2) if results else 0.0,
        "flagged":      flagged,
        "verified":     verified,
        "by_category":  by_category,
        "top_vendors":  top_vendors,
        "timeline":     timeline,
        "timeline_total":  dated_total,
        "timeline_peak":   peak,
        "timeline_days":   len(timeline),
        "proc_total_seconds": round(sum(proc_times), 1),
        "proc_avg_seconds":   round(sum(proc_times) / len(proc_times), 1) if proc_times else 0.0,
    }


@app.get("/stats")
async def get_stats():
    with _results_lock:
        results_copy = list(_results)
    return JSONResponse(_compute_stats(results_copy))


# ── CSV export ─────────────────────────────────────────────────────────────────

_CSV_COLUMNS = [
    ("Category",    lambda r: (r.get("_category") or r.get("category") or "misc").upper()),
    ("Date",        lambda r: r.get("date") or ""),
    ("Vendor",      lambda r: r.get("vendor") or ""),
    ("Amount",      lambda r: f"{float(r.get('amount') or 0):.2f}"),
    ("Job Name",    lambda r: r.get("job_name") or ""),
    ("Job Number",  lambda r: r.get("job_number") or ""),
    ("Description", lambda r: r.get("expense_description") or ""),
    ("Summary",     lambda r: r.get("ai_summary") or r.get("summary") or ""),
    ("Flag",        lambda r: r.get("_flag") or ""),
    ("File",        lambda r: r.get("_new_filename") or r.get("_file") or ""),
]


def _results_to_csv(results: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in _CSV_COLUMNS])
    for r in sorted(results, key=sort_key_for_receipt):
        writer.writerow([fn(r) for _, fn in _CSV_COLUMNS])
    return buf.getvalue()


@app.get("/export/csv")
async def export_csv():
    with _results_lock:
        results_copy = copy.deepcopy(_results)
    if not results_copy:
        return JSONResponse({"error": "No processed results available"}, status_code=404)
    csv_text = _results_to_csv(results_copy)
    fname = f"Reimbursements_{time.strftime('%Y-%m-%d')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Report history ─────────────────────────────────────────────────────────────

@app.get("/reports")
async def list_reports():
    """Previously generated workbooks in the output folder, newest first."""
    reports = []
    try:
        for p in OUT_FOLDER.glob("Reimbursements_*.xlsx"):
            try:
                st = p.stat()
                reports.append({
                    "filename": p.name,
                    "size":     st.st_size,
                    "modified": int(st.st_mtime),
                })
            except OSError:
                pass
    except Exception:
        pass
    reports.sort(key=lambda r: -r["modified"])
    return JSONResponse({"reports": reports})


@app.get("/reports/download")
async def download_report(filename: str = ""):
    if (not filename or "/" in filename or "\\" in filename or ".." in filename
            or not filename.startswith("Reimbursements_")
            or not filename.endswith((".xlsx", ".csv"))):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    p = OUT_FOLDER / filename
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    media = ("text/csv" if filename.endswith(".csv")
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(str(p), media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{p.name}"'})


@app.post("/reports/clear")
async def clear_reports():
    """Manually clear the report history: delete every generated workbook/CSV from
    the output folder. Scoped to the safe ``Reimbursements_*`` glob inside
    OUT_FOLDER — never touches receipt images or anything else."""
    removed = 0
    errors: list[str] = []
    try:
        for pattern in ("Reimbursements_*.xlsx", "Reimbursements_*.csv"):
            for p in OUT_FOLDER.glob(pattern):
                try:
                    if p.is_file():
                        p.unlink()
                        removed += 1
                except OSError as exc:
                    errors.append(f"{p.name}: {exc}")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "removed": removed, "errors": errors})


# ── Results management ─────────────────────────────────────────────────────────

_EDITABLE_FIELDS = {"vendor", "date", "amount", "category", "job_name",
                    "job_number", "ai_summary", "expense_description"}
_DUP_FLAG_KEYWORDS = ("potential duplicate", "duplicate of")


class UpdateResultRequest(BaseModel):
    filename: str
    field: str
    value: str = ""


@app.post("/results/update")
async def update_result(body: UpdateResultRequest):
    """Inline-edit a single field of a completed receipt."""
    field = body.field
    if field not in _EDITABLE_FIELDS:
        return JSONResponse({"ok": False, "error": f"Field not editable: {field}"},
                            status_code=400)
    value: object = body.value.strip()

    if field == "amount":
        try:
            value = round(float(str(value).replace("$", "").replace(",", "")), 2)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Amount must be a number"},
                                status_code=400)
        # "inf"/"nan" parse fine as floats but would poison every downstream
        # total/average and produce invalid JSON (NaN) that breaks the SSE feed
        # and the persisted state file the browser reads back.
        if not math.isfinite(value):
            return JSONResponse({"ok": False, "error": "Amount must be a finite number"},
                                status_code=400)
    elif field == "category":
        if value not in ("fuel", "mats", "misc"):
            return JSONResponse({"ok": False, "error": "Invalid category"},
                                status_code=400)

    with _results_lock:
        target = None
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                target = r
                break
        if target is None:
            return JSONResponse({"ok": False, "error": "Receipt not found in results"},
                                status_code=404)

        if field == "category":
            target["category"]  = value
            target["_category"] = value
        else:
            target[field] = value if value != "" else None

        # Vendor/date/amount edits change duplicate identity — recompute flags
        if field in ("vendor", "date", "amount"):
            for r in _results:
                flag = (r.get("_flag") or "").lower()
                if any(kw in flag for kw in _DUP_FLAG_KEYWORDS):
                    r["_flag"] = ""
            _detect_duplicates(_results)

        updated = dict(target)

    kanban_key = updated.get("_file") or body.filename
    _update_kanban(kanban_key, "done", updated)
    _persist_state()
    _broadcast({
        "type":     "kanban_update",
        "filename": kanban_key,
        "status":   "done",
        "data":     _safe_receipt_data(updated),
        "model":    "",
    })
    return JSONResponse({"ok": True, "data": _safe_receipt_data(updated)})


# ── Review / approval endpoints ────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    filename: str
    approved: bool


@app.post("/results/set-approval")
async def set_approval(body: ApprovalRequest):
    """Set or remove approval on a completed receipt."""
    with _results_lock:
        target = None
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                target = r
                break
        if target is None:
            return JSONResponse({"ok": False, "error": "Receipt not found"}, status_code=404)
        target["_approved"] = body.approved
        updated = dict(target)

    kanban_key = updated.get("_file") or body.filename
    _update_kanban(kanban_key, "done", updated)
    _persist_state()
    _broadcast({
        "type":     "kanban_update",
        "filename": kanban_key,
        "status":   "done",
        "data":     _safe_receipt_data(updated),
        "model":    "",
    })
    return JSONResponse({"ok": True, "data": _safe_receipt_data(updated)})


class ReviewRequiredRequest(BaseModel):
    filename: str
    review_required: bool


@app.post("/results/set-review-required")
async def set_review_required_endpoint(body: ReviewRequiredRequest):
    """Toggle the 'review required' flag on a completed receipt."""
    with _results_lock:
        target = None
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                target = r
                break
        if target is None:
            return JSONResponse({"ok": False, "error": "Receipt not found"}, status_code=404)
        target["_review_required"] = body.review_required
        updated = dict(target)

    kanban_key = updated.get("_file") or body.filename
    _update_kanban(kanban_key, "done", updated)
    _persist_state()
    _broadcast({
        "type":     "kanban_update",
        "filename": kanban_key,
        "status":   "done",
        "data":     _safe_receipt_data(updated),
        "model":    "",
    })
    return JSONResponse({"ok": True})


@app.post("/results/clear")
async def clear_results():
    """Clear completed results and remove done/failed entries from the kanban."""
    with _results_lock:
        _results.clear()
    with _kanban_lock:
        to_remove = [fn for fn, v in _kanban.items() if v["status"] in ("done", "failed")]
        for fn in to_remove:
            del _kanban[fn]
    _persist_state()
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

    # 3. Try processing folder (exact match and fuzzy extension match for renamed files)
    if not img_path_str or not Path(img_path_str).exists():
        stem = Path(filename).stem
        for name in [filename] + [stem + ext for ext in (".jpg", ".jpeg", ".png", ".webp")]:
            candidate = PROCESSING_FOLDER / name
            if candidate.exists():
                img_path_str = str(candidate)
                break

    # 4. Try intake folder directly
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
    _cache_item(item)
    with _work_lock:
        _work_queue.appendleft(item)

    _update_kanban(filename, "queued", None)
    _persist_state()
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
    _ensure_worker_alive()   # revive the worker if the cancel toggle stopped it

    with _kanban_lock:
        _kanban.clear()
    with _results_lock:
        _results.clear()
    with _seen_lock:
        _seen_intake.clear()   # allows intake files to be re-queued after board reset

    _persist_state()
    _broadcast({"type": "kanban_cleared"})
    return JSONResponse({"ok": True, "cleared": cleared})


@app.post("/queue/unstick")
async def queue_unstick():
    """Manually re-queue all items currently stuck in ocr or distilling status."""
    with _kanban_lock:
        stalled = [
            fname for fname, entry in _kanban.items()
            if entry["status"] in ("ocr", "distilling")
        ]

    unstuck: list[str] = []
    for fname in stalled:
        with _item_cache_lock:
            cached = _item_cache.get(fname)
        if not cached:
            # Try processing folder (exact + fuzzy)
            stem = Path(fname).stem
            for name in [fname] + [stem + ext for ext in (".jpg", ".jpeg", ".png", ".webp")]:
                candidate = PROCESSING_FOLDER / name
                if candidate.exists():
                    cached = {
                        "filename":   fname,
                        "path":       str(candidate),
                        "employee":   _last_context.get("employee", "Employee"),
                        "job_name":   _last_context.get("job_name", ""),
                        "job_number": _last_context.get("job_number", ""),
                    }
                    break
        if not cached:
            candidate = INTAKE_FOLDER / fname
            if candidate.exists():
                cached = {
                    "filename":   fname,
                    "path":       str(candidate),
                    "employee":   _last_context.get("employee", "Employee"),
                    "job_name":   _last_context.get("job_name", ""),
                    "job_number": _last_context.get("job_number", ""),
                }
        if not cached:
            continue
        item = dict(cached)
        with _work_lock:
            _work_queue.appendleft(item)
        _update_kanban(fname, "queued", None)
        _broadcast({
            "type": "kanban_update", "filename": fname,
            "status": "queued", "data": {}, "model": "",
        })
        _broadcast({"type": "log", "message": f"[unstick] {fname} manually re-queued"})
        unstuck.append(fname)

    return JSONResponse({"ok": True, "unstuck": unstuck, "count": len(unstuck)})


@app.post("/queue/nudge")
async def queue_nudge():
    """Manual push button for a stalled pipeline.

    Clears any stuck cancel flag, restarts the worker thread if it died, and
    re-queues every board item sitting in queued/ocr/distilling that isn't already
    in the work queue. Use this when items aren't moving instead of restarting the
    whole container.
    """
    _worker_cancel.clear()
    revived = _ensure_worker_alive()

    with _work_lock:
        in_queue = {it["filename"] for it in _work_queue}
    with _kanban_lock:
        candidates = [
            fname for fname, entry in _kanban.items()
            if entry.get("status") in ("queued", "ocr", "distilling")
        ]

    requeued: list[str] = []
    for fname in candidates:
        if fname in in_queue:
            continue
        with _item_cache_lock:
            cached = _item_cache.get(fname)
        if not cached:
            # Try processing folder first
            stem = Path(fname).stem
            for name in [fname] + [stem + ext for ext in (".jpg", ".jpeg", ".png", ".webp")]:
                candidate = PROCESSING_FOLDER / name
                if candidate.exists():
                    cached = {
                        "filename":   fname,
                        "path":       str(candidate),
                        "employee":   _last_context.get("employee", "Employee"),
                        "job_name":   _last_context.get("job_name", ""),
                        "job_number": _last_context.get("job_number", ""),
                    }
                    break
        if not cached:
            candidate = INTAKE_FOLDER / fname
            if candidate.exists():
                cached = {
                    "filename":   fname,
                    "path":       str(candidate),
                    "employee":   _last_context.get("employee", "Employee"),
                    "job_name":   _last_context.get("job_name", ""),
                    "job_number": _last_context.get("job_number", ""),
                }
        if not cached:
            continue
        with _work_lock:
            _work_queue.append(dict(cached))
        _update_kanban(fname, "queued", None)
        _broadcast({
            "type": "kanban_update", "filename": fname,
            "status": "queued", "data": {}, "model": "",
        })
        requeued.append(fname)

    bits = []
    if revived:
        bits.append("worker restarted")
    bits.append(f"{len(requeued)} item(s) re-queued" if requeued else "queue already moving")
    _broadcast({"type": "log", "message": f"[nudge] {', '.join(bits)}"})
    return JSONResponse({
        "ok": True, "requeued": requeued,
        "count": len(requeued), "worker_restarted": revived,
    })


@app.post("/kanban/remove")
async def kanban_remove(body: RetryRequest):
    """Remove a single item from the kanban (client-initiated dismiss).

    Also removes the item from _results so the insights dashboard reflects the
    dismissal immediately.
    """
    filename = body.filename
    with _kanban_lock:
        _kanban.pop(filename, None)
    with _results_lock:
        _results[:] = [
            r for r in _results
            if r.get("_file") != filename and r.get("_new_filename") != filename
        ]
    _persist_state()
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
            "models":         models,
            "active_distill": _pr._active_distill_model,
            "active_ocr":     _pr._active_ocr_model,
            "thinking":       _pr._thinking_enabled,
            "ok":             True,
        })
    except Exception as exc:
        return JSONResponse({
            "models":         [],
            "active_distill": _pr._active_distill_model,
            "active_ocr":     _pr._active_ocr_model,
            "thinking":       _pr._thinking_enabled,
            "ok":             False,
            "error":          str(exc),
        })


class ModelSwapRequest(BaseModel):
    model: str


@app.post("/models/distill")
async def swap_distill_model(body: ModelSwapRequest):
    """Set distillation model. LM Studio JIT will load it on first use."""
    target = body.model.strip()
    _pr._active_distill_model = target
    return JSONResponse({"ok": True, "active_distill": target})


@app.post("/models/ocr")
async def swap_ocr_model(body: ModelSwapRequest):
    """Set (or clear) the dedicated OCR model. LM Studio JIT loads on first use."""
    target = body.model.strip()
    _pr._active_ocr_model = target        # empty string = disable OCR stage
    return JSONResponse({"ok": True, "active_ocr": target})


class ThinkingRequest(BaseModel):
    enabled: bool = False


@app.post("/models/thinking")
async def set_thinking(body: ThinkingRequest):
    """Toggle reasoning ("thinking") mode for distillation/vision and persist it.
    The OCR transcription pass always runs without reasoning, regardless."""
    _pr._thinking_enabled = bool(body.enabled)
    cfg = _load_config()
    cfg["thinking_enabled"] = _pr._thinking_enabled
    _save_config(cfg)
    return JSONResponse({"ok": True, "thinking": _pr._thinking_enabled})


@app.get("/models/lmstudio")
async def get_lmstudio_models():
    def _fetch():
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio",
                    timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES)
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
    paths = {
        "output":     str(OUTPUT_FOLDER),
        "processing": str(PROCESSING_FOLDER),
    }
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
        "host_export_path": cfg.get("host_export_path") or os.getenv("HOST_EXPORT_PATH", ""),
        "docker": Path("/.dockerenv").exists(),
        "version": APP_VERSION,
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


@app.get("/version")
async def get_version():
    return JSONResponse({"version": APP_VERSION})


# ── Image-processing settings ──────────────────────────────────────────────────

class ProcessingSettingsRequest(BaseModel):
    autorotate:   bool | None = None
    autocrop:     bool | None = None
    grayscale:    bool | None = None
    compress:     bool | None = None
    local_ocr:    bool | None = None
    jpeg_quality: int | None = None


@app.get("/settings/processing")
async def get_processing_settings():
    return JSONResponse(_processing_settings())


@app.post("/settings/processing")
async def save_processing_settings(body: ProcessingSettingsRequest):
    try:
        cfg = _load_config()
        proc = cfg.get("processing") or {}
        if body.autorotate is not None: proc["autorotate"] = bool(body.autorotate)
        if body.autocrop  is not None: proc["autocrop"]  = bool(body.autocrop)
        if body.grayscale is not None: proc["grayscale"] = bool(body.grayscale)
        if body.compress  is not None: proc["compress"]  = bool(body.compress)
        if body.local_ocr is not None: proc["local_ocr"] = bool(body.local_ocr)
        if body.jpeg_quality is not None:
            proc["jpeg_quality"] = max(40, min(95, int(body.jpeg_quality)))
        cfg["processing"] = proc
        _save_config(cfg)
        applied = _apply_processing_config(cfg)
        return JSONResponse({"ok": True, **applied})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Review / approval settings ────────────────────────────────────────────────

class ReviewSettingsRequest(BaseModel):
    require_approval: bool | None = None


@app.get("/settings/review")
async def get_review_settings():
    cfg = _load_config()
    return JSONResponse({"require_approval": bool(cfg.get("require_approval", False))})


@app.post("/settings/review")
async def save_review_settings(body: ReviewSettingsRequest):
    try:
        cfg = _load_config()
        if body.require_approval is not None:
            cfg["require_approval"] = bool(body.require_approval)
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Email settings ─────────────────────────────────────────────────────────────

class EmailSettingsRequest(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""      # blank = keep previously saved password
    smtp_from: str = ""
    email_to:  str = ""
    email_subject: str = "Weekly Reimbursement Report"


@app.get("/settings/email")
async def get_email_settings():
    # Resolve from the same source the sender uses (UI config over env), and report
    # only whether a password is present — never echo the secret back to the client.
    from watch_mode import load_email_config
    em = load_email_config()
    password_set = bool(em["pass"])
    return JSONResponse({
        "smtp_host":     em["host"],
        "smtp_port":     em["port"],
        "smtp_user":     em["user"],
        "smtp_from":     em["from"],
        "email_to":      em["to"],
        "email_subject": em["subject"],
        "password_set":  password_set,
        "configured":    bool(em["host"] and em["user"] and em["to"] and password_set),
    })


@app.post("/settings/email")
async def save_email_settings(body: EmailSettingsRequest):
    try:
        cfg = _load_config()
        email = cfg.get("email") or {}
        email["smtp_host"]     = body.smtp_host.strip()
        email["smtp_port"]     = int(body.smtp_port or 587)
        email["smtp_user"]     = body.smtp_user.strip()
        email["smtp_from"]     = body.smtp_from.strip()
        email["email_to"]      = body.email_to.strip()
        email["email_subject"] = body.email_subject.strip() or "Weekly Reimbursement Report"
        email.pop("smtp_pass", None)   # migrate any legacy secret out of the synced config
        if body.smtp_pass:             # blank keeps the previously saved password
            app_secrets.save_secret("smtp_pass", body.smtp_pass)
        cfg["email"] = email
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/settings/email/test")
async def test_email_settings():
    def _run():
        from watch_mode import send_test_email
        return send_test_email()
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ── Scheduled export endpoints ─────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    enabled: bool = False
    time: str = "17:00"
    days: str = "thu"
    dropbox_token: str = ""
    email: bool = False


def _schedule_status() -> dict:
    cfg = _get_schedule_config()
    saved = _load_config().get("schedule") or {}
    nxt = scheduler.next_run(cfg, datetime.now())
    return {
        "enabled":        cfg.enabled,
        "time":           cfg.time_str,
        "days":           cfg.days_str,
        "email":          cfg.email,
        "dropbox":        bool(cfg.dropbox_token),
        "export_folder":  str(scheduler.EXPORT_FOLDER),
        "next_run":       nxt.isoformat(timespec="minutes") if nxt else None,
        "last_run":       saved.get("last_run"),
    }


@app.get("/schedule")
async def get_schedule():
    return JSONResponse(_schedule_status())


@app.post("/schedule")
async def set_schedule(body: ScheduleRequest):
    try:
        new = {
            "enabled": body.enabled,
            "time":    body.time.strip(),
            "days":    body.days.strip(),
            "email":   body.email,
            "dropbox_token": body.dropbox_token.strip(),
        }
        scheduler.parse_schedule(new)  # validate before persisting
    except scheduler.ScheduleError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    cfg = _load_config()
    saved = cfg.get("schedule") or {}
    # The Dropbox token is a secret: persist it outside the (often cloud-synced)
    # config file, and never write it back into .app_config.json. A blank input
    # keeps whatever was previously saved.
    if new["dropbox_token"]:
        app_secrets.save_secret("dropbox_token", new["dropbox_token"])
    new["dropbox_token"] = ""
    saved.pop("dropbox_token", None)   # migrate any legacy token out of the config
    new["last_run"] = saved.get("last_run")
    cfg["schedule"] = new
    _save_config(cfg)
    if _schedule_wakeup is not None:
        _schedule_wakeup.set()
    return JSONResponse({"ok": True, **_schedule_status()})


@app.post("/schedule/run-now")
async def schedule_run_now():
    cfg = _get_schedule_config()
    results, employee = _schedule_results_snapshot()

    def _run():
        return scheduler.run_export(cfg, results, employee)

    report = await asyncio.get_event_loop().run_in_executor(None, _run)
    report["ran_at"] = datetime.now().isoformat(timespec="seconds")
    _on_schedule_result(report)
    status = 200 if report.get("ok") else 400
    return JSONResponse(report, status_code=status)


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


# ── Local OCR (RapidOCR) diagnostics ────────────────────────────────────────────

@app.get("/debug/ocr-status")
async def ocr_status():
    """Check whether the local OCR engine (RapidOCR) is installed and loadable."""
    def _check():
        try:
            _pr._import_rapidocr()
        except ImportError as exc:
            return {
                "available": False,
                "reason": f"RapidOCR is not installed in this Python environment: {exc}",
                "fix": ("Install it (it's in requirements.txt): "
                        "pip install 'rapidocr-onnxruntime' 'onnxruntime' "
                        "— or rebuild the Docker image."),
            }
        # Retry a previously failed init so a fixed environment is picked up
        # without restarting the server.
        _pr._reset_ocr_engine_failure()
        engine = _pr._get_ocr_engine()
        if engine is None:
            init_err = _pr._ocr_init_error or "unknown error during RapidOCR init"
            return {
                "available": False,
                "reason": f"RapidOCR imported but engine failed to initialise: {init_err}",
                "fix": ("The ONNX models ship inside the wheel, so no download is needed. "
                        "Reinstall the OCR stack: pip install --force-reinstall "
                        "'rapidocr-onnxruntime' 'onnxruntime' (or rebuild the Docker image); "
                        "if it persists, check available RAM/disk."),
            }
        return {"available": True, "engine": str(type(engine).__name__)}

    result = await asyncio.get_event_loop().run_in_executor(None, _check)
    return JSONResponse(result)


@app.post("/debug/ocr-test")
async def ocr_test(files: list[UploadFile] = File(...)):
    """Run the local OCR engine (RapidOCR) on an uploaded image, return the text."""
    if not files:
        return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)

    f = files[0]
    suffix = Path(f.filename or "test.jpg").suffix or ".jpg"
    tmp = PROCESSING_FOLDER / f"_ocr_test{suffix}"
    PROCESSING_FOLDER.mkdir(parents=True, exist_ok=True)
    try:
        content = await f.read()
        tmp.write_bytes(content)

        def _run():
            return _pr._extract_local_ocr(tmp)

        text = await asyncio.get_event_loop().run_in_executor(None, _run)
        return JSONResponse({
            "ok": text is not None,
            "text": text or "",
            "chars": len(text) if text else 0,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        tmp.unlink(missing_ok=True)


# ── Admin / maintenance ────────────────────────────────────────────────────────

import signal as _signal


@app.post("/admin/restart")
async def admin_restart():
    """Gracefully stop the server process.

    Docker (restart: always / unless-stopped) will immediately relaunch the
    container.  Outside Docker, pair this with a process manager (systemd,
    supervisor, etc.) configured to restart on exit.
    """
    async def _shutdown():
        await asyncio.sleep(0.4)         # let the response reach the browser
        _worker_cancel.set()             # stop background threads cleanly
        os.kill(os.getpid(), _signal.SIGTERM)

    asyncio.create_task(_shutdown())
    return JSONResponse({"ok": True, "message": "Restarting…"})


def _referenced_filenames() -> set[str]:
    """Every filename (and stem) the app still tracks — results, board cards,
    queued work, and the stall-recovery item cache.  Stems are included so a
    file whose extension changed during compression (photo.png → photo.jpg)
    still counts as referenced."""
    names: set[str] = set()

    def _add(value) -> None:
        if not value:
            return
        base = Path(str(value)).name
        names.add(base.lower())
        names.add(Path(base).stem.lower())

    data_dicts: list[dict] = []
    with _results_lock:
        data_dicts.extend(dict(r) for r in _results)
    with _kanban_lock:
        for fname, entry in _kanban.items():
            _add(fname)
            data_dicts.append(dict(entry.get("data") or {}))
    for d in data_dicts:
        for key in ("_file", "_new_filename", "_compressed_file", "_image_path"):
            _add(d.get(key))
    with _work_lock:
        for item in _work_queue:
            _add(item.get("filename"))
            _add(item.get("path"))
    with _item_cache_lock:
        for fname, item in _item_cache.items():
            _add(fname)
            _add((item or {}).get("path"))
    return names


def _collect_orphans() -> tuple[list[dict], list[str], int]:
    """Walk the working folders and return ``(orphans, empty_dirs, scanned)``.

    Pure scan — nothing is deleted. Shared by the report endpoint and the
    bulk-delete endpoint so both judge "orphaned" by exactly the same rules:
    a file no result, board card, or queue item references, found in the
    completed-receipts or processing folders (recursively) or in a stale
    _pdf_* page folder. Intake-root files are pending input, never orphans.
    """
    referenced = _referenced_filenames()
    orphans: list[dict] = []
    empty_dirs: list[str] = []
    scanned = 0

    def _scan(folder: Path, label: str) -> None:
        nonlocal scanned
        if not folder.exists():
            return
        for p in sorted(folder.rglob("*")):
            if p.is_dir():
                # Stale temp dirs (_pdf_* page folders, _upload_* staging,
                # _zip_* archive extractions) with nothing left inside are clutter
                # worth reporting too.
                if p.name.startswith(("_pdf_", "_upload_", "_zip_")) and not any(p.iterdir()):
                    empty_dirs.append(f"{label}/{p.relative_to(folder)}")
                continue
            if not p.is_file() or p.name.startswith("."):
                continue
            scanned += 1
            if p.name.lower() in referenced or p.stem.lower() in referenced:
                continue
            # Original PDFs are archived next to the receipts after their pages
            # were queued; they stay referenced through their converted pages
            # (named "<pdf stem><page suffix>.jpg").
            if p.suffix.lower() in PDF_EXTENSIONS and any(
                    n.startswith(p.stem.lower()) for n in referenced):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            orphans.append({
                "folder":   label,
                "name":     str(p.relative_to(folder)),
                "path":     str(p.resolve()),          # full on-disk location
                "size":     st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })

    _scan(IMAGES_FOLDER, "receipts")
    _scan(PROCESSING_FOLDER, "processing")
    try:
        for d in sorted(INTAKE_FOLDER.iterdir()):
            if d.is_dir() and d.name.startswith(("_pdf_", "_zip_")):
                _scan(d, f"intake/{d.name}")
                if not any(d.iterdir()):
                    empty_dirs.append(f"intake/{d.name}")
    except OSError:
        pass

    return orphans, empty_dirs, scanned


@app.get("/maintenance/orphans")
async def find_orphaned_files():
    """Report files in the working folders that no result, board card, or queue
    item references — leftovers from clears, crashes, or interrupted renames.

    Scans the completed-receipts and processing folders (including their
    subfolders) plus stale _pdf_* page folders in the intake folder.  Files in
    the intake folder root are pending input, not orphans.  Report-only —
    nothing is deleted.
    """
    orphans, empty_dirs, scanned = _collect_orphans()
    return JSONResponse({
        "ok":         True,
        "scanned":    scanned,
        "count":      len(orphans),
        "total_size": sum(o["size"] for o in orphans),
        "orphans":    orphans,
        "empty_dirs": empty_dirs,
    })


@app.post("/maintenance/delete-orphans")
async def delete_orphaned_files():
    """Delete every orphaned file the scan currently reports.

    Re-runs the same scan as ``/maintenance/orphans`` so the referenced set is
    fresh, then unlinks each reported file — guarding that every target still
    resolves inside one of the working folders before removing it. Empty folders
    are left to the separate cleanup endpoint.
    """
    orphans, _empty_dirs, _scanned = _collect_orphans()
    roots = [p.resolve() for p in (IMAGES_FOLDER, PROCESSING_FOLDER, INTAKE_FOLDER)
             if p.exists()]

    def _within_roots(path: Path) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    deleted: list[dict] = []
    errors: list[dict] = []
    freed = 0
    for o in orphans:
        try:
            rp = Path(o["path"]).resolve()
        except OSError:
            continue
        # Belt-and-braces: never unlink anything outside the working folders.
        if not _within_roots(rp) or not rp.is_file():
            continue
        try:
            size = rp.stat().st_size
        except OSError:
            size = o.get("size") or 0
        try:
            rp.unlink()
            deleted.append({"folder": o["folder"], "name": o["name"], "path": str(rp)})
            freed += size
        except OSError as exc:
            errors.append({"path": str(rp), "error": str(exc)})

    return JSONResponse({
        "ok":      True,
        "count":   len(deleted),
        "freed":   freed,
        "deleted": deleted,
        "errors":  errors,
    })


def _prune_empty_dirs(root: Path) -> list[Path]:
    """Remove empty directories anywhere under ``root`` (but not ``root`` itself).

    Walks bottom-up so a folder that holds only empty subfolders collapses in a
    single pass. Returns the directories that were removed.
    """
    removed: list[Path] = []
    if not root.exists() or not root.is_dir():
        return removed
    # Deepest paths first so nested empties are cleared before their parents
    for p in sorted((d for d in root.rglob("*") if d.is_dir()),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            if not any(p.iterdir()):
                p.rmdir()
                removed.append(p)
        except OSError:
            pass
    return removed


def _run_empty_dir_cleanup() -> list[dict]:
    """Remove emptied, orphaned job folders — temp staging (_upload_*, _pdf_*) or
    real per-job / dated subfolders — left behind in the working directories. Only
    ever removes directories that contain no files; never the intake root or
    pending input files. Returns one record per removed directory.

    Shared by the maintenance endpoint and the session-start sweep so both judge
    "removable" by exactly the same rules.
    """
    removed: list[dict] = []

    for root, label in ((IMAGES_FOLDER, "receipts"), (PROCESSING_FOLDER, "processing")):
        for d in _prune_empty_dirs(root):
            removed.append({"folder": label, "name": str(d.name), "path": str(d.resolve())})

    # Intake temp dirs (PDF page folders / upload staging / zip extractions) —
    # never the intake root or pending input files.
    try:
        for d in sorted(INTAKE_FOLDER.iterdir()):
            if d.is_dir() and d.name.startswith(("_pdf_", "_upload_", "_zip_")):
                _prune_empty_dirs(d)
                try:
                    if not any(d.iterdir()):
                        loc = str(d.resolve())
                        d.rmdir()
                        removed.append({"folder": "intake", "name": d.name, "path": loc})
                except OSError:
                    pass
    except OSError:
        pass

    return removed


@app.post("/maintenance/cleanup-empty-dirs")
async def cleanup_empty_dirs():
    """Delete emptied, orphaned job folders — temp staging (_upload_*, _pdf_*) or
    real per-job subfolders — left behind in the working directories. Only ever
    removes directories that contain no files. Report includes each location.
    """
    removed = _run_empty_dir_cleanup()
    return JSONResponse({"ok": True, "count": len(removed), "removed": removed})


# ── Watch-mode / email ─────────────────────────────────────────────────────────

@app.get("/watch/status")
async def watch_status():
    try:
        from watch_mode import load_state, load_email_config
        state = load_state()
        em = load_email_config()
        return JSONResponse({
            "receipts":        len(state.get("receipts", [])),
            "last_emailed":    state.get("last_emailed"),
            "smtp_configured": bool(em["host"] and em["user"] and em["pass"] and em["to"]),
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
        client = OpenAI(base_url=_pr.LMSTUDIO_BASE_URL, api_key="lmstudio",
                    timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES)
        result = send_report(state, client=client)
        status = 200 if result.get("ok") else 503
        return JSONResponse(result, status_code=status)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
