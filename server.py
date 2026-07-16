#!/usr/bin/env python3
"""server.py — FastAPI web frontend for the receipt processor (queue-based architecture)."""
from __future__ import annotations

import asyncio
import base64
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
import urllib.request
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from openai import OpenAI

import app_secrets
import email_intake
import gdrive_intake
import gmail_filter
import multiuser
import onedrive_intake
import process_receipts as _pr
import scheduler
import users
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
#
# In single-user mode these names behave exactly as before: real Path constants
# rooted at OUTPUT_FOLDER. In multi-user mode they are *context proxies* that
# resolve to the current user's Workspace folders (multiuser.cur_ws()), so every
# existing ``IMAGES_FOLDER / x`` / ``.mkdir()`` / ``.iterdir()`` call site is
# transparently per-user with no edit. The default workspace below pins the
# single-user layout so behaviour (and tests that monkeypatch these names) are
# unchanged. See multiuser.py for the proxy mechanics.
_DEFAULT_OUT = Path(OUTPUT_FOLDER)
_default_ws  = multiuser.Workspace(multiuser.DEFAULT_USER, _DEFAULT_OUT)
# Pin the default workspace to today's exact single-user folder layout (the base
# Workspace would otherwise put intake under <root>/intake, etc.).
_default_ws.intake_folder     = Path(RECEIPTS_FOLDER)
_default_ws.images_folder     = _DEFAULT_OUT / "receipts"     # completed receipt images
_default_ws.processing_folder = _DEFAULT_OUT / "processing"   # in-flight and failed images
_default_ws.rejected_folder   = _DEFAULT_OUT / "unsupported"  # files we can't read
# Receipts the user chose to keep (not delete) after exporting land here. It sits
# OUTSIDE the scanned working folders, so archived receipts never resurface as
# "orphaned" files in the maintenance scan.
_default_ws.archive_folder    = _DEFAULT_OUT / "archive"
_default_ws.state_file        = _DEFAULT_OUT / ".app_state.json"  # crash-safe snapshot
multiuser.configure(_default_ws, users_base=_DEFAULT_OUT / "users")

INTAKE_FOLDER      = multiuser.path_proxy("intake_folder")
OUT_FOLDER         = multiuser.path_proxy("out_folder")
IMAGES_FOLDER      = multiuser.path_proxy("images_folder")
PROCESSING_FOLDER  = multiuser.path_proxy("processing_folder")
REJECTED_FOLDER    = multiuser.path_proxy("rejected_folder")
ARCHIVE_FOLDER     = multiuser.path_proxy("archive_folder")
# CONFIG_FILE is the single authoritative app-config path, defined once in
# process_receipts and imported here so the server, watcher, and scheduler all
# read/write the same file (see process_receipts.CONFIG_FILE). It stays a SHARED,
# instance-level file in multi-user mode (one model/pipeline config per box).
STATE_FILE    = multiuser.path_proxy("state_file")   # crash-safe results/board snapshot

# ── Stall checker config ───────────────────────────────────────────────────────

STALL_TIMEOUT_SECS  = int(os.getenv("STALL_TIMEOUT_SECS",  "180"))  # 3 min
STALL_CHECK_INTERVAL = int(os.getenv("STALL_CHECK_INTERVAL", "60"))   # 1 min

# Largest single uploaded file we'll stage to disk. A receipt photo/PDF is a few
# MB at most; the generous default keeps the whole file out of memory only when
# something pathological (or a mis-targeted upload) arrives. 0 disables the cap.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))  # 100 MiB

# SSE tuning. Poll the per-client queue often so the live board/log feels
# instant, but only emit a keep-alive comment every SSE_HEARTBEAT_SECS — the two
# were previously coupled at 1s, which added up to a second of delivery latency
# and sent 15× more idle traffic than a keep-alive needs.
SSE_POLL_SECS      = float(os.getenv("SSE_POLL_SECS", "0.25"))
SSE_HEARTBEAT_SECS = float(os.getenv("SSE_HEARTBEAT_SECS", "15"))

# ── Global state ───────────────────────────────────────────────────────────────
#
# Per-user containers are *context proxies* (multiuser.cur_ws()) so the single-user
# path and the existing tests see today's behaviour, while each logged-in user gets
# an isolated board/results/run-log in multi-user mode. Genuinely shared infra —
# the work queue, the SSE subscriber list, the worker, the concurrency gate (one
# model/VRAM per box) — stays a plain global. See multiuser.py.

# The work queue is SHARED; each item carries a ``user_id`` so the worker can route
# its board/results writes to the right workspace (the LLM is serial anyway).
_work_queue: deque = deque()
_work_lock   = threading.Lock()

_kanban       = multiuser.container_proxy("kanban")
_kanban_lock  = multiuser.lock_proxy("kanban_lock")

_results      = multiuser.container_proxy("results")
_results_lock = multiuser.lock_proxy("results_lock")

# Serialises the .app_state.json write so two concurrent persisters (e.g. the
# worker thread + an event-loop handler) can't interleave their write/replace and
# publish a half-written file. Shared (the unique-tmp name already keeps per-user
# state files from colliding).
_persist_lock = threading.Lock()

_last_context = multiuser.container_proxy("last_context")

# The workbook produced by the most recent POST /generate-spreadsheet, kept so the
# "Send Report Now" button can email the exact file the user just generated —
# without rebuilding it and without depending on the separate watch-mode state
# file (the old behaviour, which read an unrelated empty store and failed with
# "no receipts in state"). The .xlsx itself lives on disk in OUT_FOLDER; this just
# remembers which one and how many receipts it covered.
_last_report_path: str | None = None
_last_report_count: int = 0

# Per-batch processing-time log, for comparing model speed across runs. Newest
# first, capped; survives restarts via the (per-user) state file.
_benchmarks   = multiuser.container_proxy("benchmarks")
_bench_lock   = multiuser.lock_proxy("bench_lock")
BENCH_MAX_ENTRIES = 100

# Full per-run (per-batch) log — every detail of one processing run: the exact
# instructions/prompts sent to the model, every image-processing + extraction
# step per receipt, and the full streamed log. Newest first, capped, persisted.
# The run being assembled while a batch drains lives at ``cur_ws().current_run``
# (None when idle); every `type:"log"` broadcast is captured into it, so the Run
# Log viewer and the Processing & Errors panel see the same stream.
_runs         = multiuser.container_proxy("runs")
_runs_lock    = multiuser.lock_proxy("runs_lock")
RUNS_MAX_ENTRIES = int(os.getenv("RUNS_MAX_ENTRIES", "25"))
RUN_MAX_LINES = int(os.getenv("RUN_MAX_LINES", "4000"))
_current_run_lock = multiuser.lock_proxy("current_run_lock")

_seen_intake  = multiuser.container_proxy("seen_intake")
_seen_lock    = multiuser.lock_proxy("seen_lock")

# Sent-ledger: identity of every receipt already included in a sent report, so a
# re-add can be skipped (with an "Include anyway" override). Per-workspace.
_sent_ledger      = multiuser.container_proxy("sent_ledger")
_sent_ledger_lock = multiuser.lock_proxy("sent_ledger_lock")
SENT_LEDGER_MAX = int(os.getenv("SENT_LEDGER_MAX", "5000"))

# Why each quarantined file in REJECTED_FOLDER was moved there, keyed by its
# on-disk name. The folder is the source of truth for *which* files exist; this
# just remembers the human-readable reason to show alongside each one.
_rejected_reasons = multiuser.container_proxy("rejected_reasons")
_rejected_lock    = multiuser.lock_proxy("rejected_lock")

_worker_cancel = threading.Event()


class _ConcurrencyGate:
    """A live-resizable concurrency limiter for the worker pool.

    Unlike ``threading.Semaphore`` (whose size is fixed at construction), the
    cap is re-read from ``_pr.MAX_PARALLEL_REQUESTS`` on every acquire, so moving
    the "process N at a time" slider takes effect *within the running batch*:
    lowering it lets in-flight receipts drain without admitting new ones until
    the active count drops; raising it wakes blocked workers immediately
    (``bump()`` is called by the settings endpoint). The executor itself is
    sized to a fixed ceiling; this gate decides how many of those threads may do
    real work at once.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active = 0

    @staticmethod
    def _limit() -> int:
        try:
            return max(1, int(_pr.MAX_PARALLEL_REQUESTS or 1))
        except (TypeError, ValueError):
            return 1

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit():
                # Time-boxed so a raised cap is honoured even if bump() is missed.
                self._cond.wait(timeout=0.5)
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def bump(self) -> None:
        """Wake blocked workers — call after the limit is raised."""
        with self._cond:
            self._cond.notify_all()


# Worker threads spin up to this ceiling; the live gate above caps how many run
# real work concurrently, so the slider can move mid-batch.
CONCURRENCY_CEILING = 8
_concurrency_gate = _ConcurrencyGate()

# Reference to the background worker thread + a guard so a crashed worker can be
# revived (by the stall checker, the lifespan startup, or a manual queue nudge).
_worker_thread: threading.Thread | None = None
_worker_start_lock = threading.Lock()

_subscribers: list = []   # list[_Subscriber] — each tagged with its owner user_id
_sub_lock = threading.Lock()
# Cap each SSE client's pending-event queue so a slow/stuck client (or a stalled
# proxy) can't grow memory without bound during a busy batch. On overflow the
# oldest event is dropped; the next full_state re-syncs the board.
SSE_QUEUE_MAX = int(os.getenv("SSE_QUEUE_MAX", "2000"))

# Item metadata cache — preserves queue item data for stall recovery (per-user)
_item_cache      = multiuser.container_proxy("item_cache")
_item_cache_lock = multiuser.lock_proxy("item_cache_lock")

# Status change timestamps — used by stall checker (per-user)
_status_timestamps = multiuser.container_proxy("status_timestamps")
_status_ts_lock    = multiuser.lock_proxy("status_ts_lock")


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
        "autorotate":              _pr.AUTOROTATE_ENABLED,
        "grayscale":               _pr.GRAYSCALE_ENABLED,
        "autocrop":                _pr.AUTOCROP_ENABLED,
        "autocrop_aggressiveness": _pr.AUTOCROP_AGGRESSIVENESS,
        "local_ocr":               _pr.LOCAL_OCR_ENABLED,
        "compress":                _pr.COMPRESS_ENABLED,
        "jpeg_quality":            _pr.JPEG_QUALITY,
        "max_parallel":            _pr.MAX_PARALLEL_REQUESTS,
        # Advanced tunables (previously env-only — now user-settable).
        "llm_timeout":             _pr.LLM_TIMEOUT,
        "llm_max_retries":         _pr.LLM_MAX_RETRIES,
        "store_max_px":            _pr.STORE_MAX_PX,
        "pdf_max_pages":           _pr.PDF_MAX_PAGES,
        "max_upload_mb":           (MAX_UPLOAD_BYTES // (1024 * 1024)) if MAX_UPLOAD_BYTES else 0,
        "rate_limit_enabled":      _pr.LLM_RATE_LIMIT_ENABLED,
        "rate_limit_per_min":      _pr.LLM_RATE_LIMIT_PER_MIN,
        "llm_429_wait_enabled":    _pr.LLM_429_WAIT_ENABLED,
        "llm_429_max_wait":        _pr.LLM_429_MAX_WAIT,
    }


def _apply_processing_config(cfg: dict | None = None) -> dict:
    """Push persisted image-processing settings into the process_receipts module."""
    global MAX_UPLOAD_BYTES
    cfg = cfg if cfg is not None else _load_config()
    if "thinking_enabled" in cfg:
        _pr._thinking_enabled = bool(cfg["thinking_enabled"])
    proc = cfg.get("processing") or {}
    if "autorotate" in proc:
        _pr.AUTOROTATE_ENABLED = bool(proc["autorotate"])
    if "autocrop" in proc:
        _pr.AUTOCROP_ENABLED = bool(proc["autocrop"])
    if proc.get("autocrop_aggressiveness") is not None:
        try:
            _pr.AUTOCROP_AGGRESSIVENESS = max(0, min(100, int(proc["autocrop_aggressiveness"])))
        except (TypeError, ValueError):
            pass
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
    if proc.get("max_parallel") is not None:
        try:
            _pr.MAX_PARALLEL_REQUESTS = max(1, min(8, int(proc["max_parallel"])))
            # Wake any workers blocked on the old (lower) limit so a raised
            # "process N at a time" slider takes effect on the running batch.
            _concurrency_gate.bump()
        except (TypeError, ValueError):
            pass
    # Advanced tunables (previously env-only); each clamped to a safe range.
    if proc.get("llm_timeout") is not None:
        try:
            _pr.LLM_TIMEOUT = float(max(10.0, min(600.0, float(proc["llm_timeout"]))))
        except (TypeError, ValueError):
            pass
    if proc.get("llm_max_retries") is not None:
        try:
            _pr.LLM_MAX_RETRIES = int(max(0, min(5, int(proc["llm_max_retries"]))))
        except (TypeError, ValueError):
            pass
    if proc.get("store_max_px") is not None:
        try:
            _pr.STORE_MAX_PX = int(max(512, min(4000, int(proc["store_max_px"]))))
        except (TypeError, ValueError):
            pass
    if proc.get("pdf_max_pages") is not None:
        try:
            _pr.PDF_MAX_PAGES = int(max(1, min(200, int(proc["pdf_max_pages"]))))
        except (TypeError, ValueError):
            pass
    if proc.get("max_upload_mb") is not None:
        try:
            MAX_UPLOAD_BYTES = max(0, min(2000, int(proc["max_upload_mb"]))) * 1024 * 1024
        except (TypeError, ValueError):
            pass
    # LLM request-rate cap (free-tier 429 guard). Either key may be set alone.
    rl_enabled = proc.get("rate_limit_enabled")
    rl_per_min = proc.get("rate_limit_per_min")
    if rl_enabled is not None or rl_per_min is not None:
        try:
            _pr.set_rate_limit(
                per_min=(max(1, min(1000, int(rl_per_min))) if rl_per_min is not None else None),
                enabled=(bool(rl_enabled) if rl_enabled is not None else None),
            )
        except (TypeError, ValueError):
            pass
    # Wait-for-bucket-refill on a 429'd essential call. Either key may be set alone.
    w_enabled = proc.get("llm_429_wait_enabled")
    w_max     = proc.get("llm_429_max_wait")
    if w_enabled is not None or w_max is not None:
        try:
            _pr.set_429_wait(
                enabled=(bool(w_enabled) if w_enabled is not None else None),
                max_wait=(max(0.0, min(120.0, float(w_max))) if w_max is not None else None),
            )
        except (TypeError, ValueError):
            pass
    return _processing_settings()


def _apply_model_config(cfg: dict | None = None) -> None:
    """Restore the persisted single-model selection + LLM-OCR toggle.

    Run BEFORE initialize_models so a user's saved model choice survives a
    restart: initialize_models only overrides the model when it isn't loaded.
    """
    cfg = cfg if cfg is not None else _load_config()
    models = cfg.get("models") or {}
    if models.get("llm_ocr") is not None:
        _pr.set_llm_ocr(bool(models["llm_ocr"]))
    if models.get("active"):
        _pr.set_active_model(str(models["active"]))


def _normalize_llm_url(url: str) -> str:
    """Ensure the URL ends with /v1 (OpenAI-compatible path)."""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def _in_docker() -> bool:
    """True when the app itself is running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _docker_llm_url() -> str:
    """Resolve the bundled "docker" LLM server URL for the current runtime.

    The ``model-server`` hostname only resolves *inside* the docker-compose
    network.  When the app itself runs on the host (no ``/.dockerenv``), the same
    bundled server is reachable on its published port at ``127.0.0.1:1234``.
    Using the service name in that case strands the connection on an unresolvable
    host — so pick the address that actually works for where we're running.
    """
    host = "model-server" if _in_docker() else "127.0.0.1"
    return f"http://{host}:1234/v1"


def _is_docker_bundled_url(url: str) -> bool:
    """True when *url* points to the bundled model-server compose service.

    Catches the case where the URL was injected via environment variable (not
    saved config), so there is no ``server_type`` key to inspect.
    """
    u = (url or "").lower()
    return "model-server:" in u or (_in_docker() and "host.docker.internal:" in u)


def _probe_llm_url(base_url: str, timeout: float = 1.5) -> tuple[bool, int]:
    """Quick reachability check for an OpenAI-compatible LLM server.

    Returns ``(reachable, model_count)``. A GET ``{base_url}/models`` that
    answers HTTP 200 with a JSON ``data`` list counts as reachable; the count is
    how many models that server currently reports loaded.
    """
    if not base_url:
        return (False, 0)
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer lmstudio"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return (False, 0)
            data = json.loads(resp.read() or b"{}")
            return (True, len(data.get("data") or []))
    except Exception:
        return (False, 0)


def _candidate_llm_urls() -> list[str]:
    """Ordered, de-duplicated LLM endpoints to try when auto-detecting.

    The currently-configured URL is tried first (so a working setup is never
    disturbed), then the well-known endpoints for the common deployments: a host
    LM Studio on :1234, the bundled Docker ``model-server`` on :1234, and the
    ``host.docker.internal`` variants used when the app itself runs in Docker.
    The legacy :11434 ports are kept as final fallbacks for users with old configs.
    """
    cands = [
        getattr(_pr, "LMSTUDIO_BASE_URL", "") or "",
        "http://127.0.0.1:1234/v1",
        "http://localhost:1234/v1",
        "http://host.docker.internal:1234/v1",
        _docker_llm_url(),                       # runtime-aware bundled server (:1234)
        "http://127.0.0.1:11434/v1",             # legacy fallback (old bundled port)
        "http://host.docker.internal:11434/v1",  # legacy fallback (old bundled port)
    ]
    seen: set[str] = set()
    out: list[str] = []
    for u in cands:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _autodetect_llm_url(timeout: float = 1.5) -> str | None:
    """Probe candidate endpoints; return the best reachable one (or None).

    Prefers an endpoint that already has a model loaded; otherwise falls back to
    any endpoint that merely answers.
    """
    reachable_no_model: str | None = None
    for url in _candidate_llm_urls():
        ok, n = _probe_llm_url(url, timeout=timeout)
        if ok and n > 0:
            return url
        if ok and reachable_no_model is None:
            reachable_no_model = url
    return reachable_no_model


def _ensure_llm_reachable() -> None:
    """Safety net: if the configured endpoint is dead, adopt a working one.

    Stops a stale saved choice (e.g. a "docker" server-type pinned to :11434
    while LM Studio is actually on :1234) from permanently stranding the
    connection. Non-destructive — it only changes the in-memory URL for this
    process, leaving the persisted preference intact so a corrected setup still
    wins on a clean start. ``POST /llm-server/autodetect`` persists when the user
    asks for it explicitly.
    """
    # OpenRouter is a cloud endpoint with its own auth/reachability; the
    # localhost autodetect candidates don't apply, so never override it here.
    if (_load_config().get("provider") or "local").strip() == "openrouter":
        return
    current = getattr(_pr, "LMSTUDIO_BASE_URL", "") or ""
    if current and _probe_llm_url(current)[0]:
        return
    found = _autodetect_llm_url()
    if found and found != current:
        _pr.LMSTUDIO_BASE_URL = found
        print(f"[models] configured LLM endpoint {current or '(none)'} unreachable; "
              f"auto-switched to {found}")
    elif not found:
        print(f"[models] no LLM endpoint reachable (tried: {_candidate_llm_urls()})")


def _startup_models() -> None:
    """Background-thread startup: auto-recover the endpoint, then init models.

    For the OpenRouter provider the model is chosen explicitly (the free router
    or a pinned id), so the local auto-select in initialize_models() is SKIPPED —
    otherwise it would query the cloud catalogue, find the router slug "missing,"
    and clobber the selection. We instead best-effort fill the quick-first vision
    fallback list here, off the event loop.
    """
    try:
        _ensure_llm_reachable()
    except Exception as exc:
        print(f"[models] auto-detect skipped: {exc}")
    cfg = _load_config()
    if (cfg.get("provider") or "local").strip() == "openrouter":
        print(f"[models] OpenRouter provider active "
              f"(model: {_pr._active_distill_model or 'openrouter/free'}); "
              "skipping local model auto-select.")
        orc = cfg.get("openrouter") or {}
        if not orc.get("models_fallback"):
            try:
                fb = _openrouter_vision_fallback()
                if fb:
                    orc["models_fallback"] = fb
                    cfg["openrouter"] = orc
                    _save_config(cfg)
                    _apply_openrouter_config(cfg)   # refresh LLM_EXTRA_BODY w/ fallback
                    print(f"[models] OpenRouter vision fallback pinned: {fb}")
            except Exception:
                pass
        return
    initialize_models()


# ── OpenRouter (cloud LLM router) integration ────────────────────────────────
# OpenRouter exposes an OpenAI-compatible API, so the existing pipeline/clients
# work unchanged — we just point the base URL there and authenticate with the
# user's key. It is OPT-IN and OFF by default: selecting it sends receipt data to
# a third-party cloud, which breaks the app's local-only default, so it is never
# chosen automatically. The user picks it in Settings and supplies an API key.

OPENROUTER_ATTR_HEADERS = {
    "HTTP-Referer": "https://github.com/duedev/reimbursements",
    "X-Title":      "Reimbursements",
}
# Token families that read receipts well and tend to ship usable free tiers —
# used only to RANK the free vision models, never to exclude any.
_OPENROUTER_PREFERRED = ("gemini", "qwen", "llama", "mistral", "gemma",
                         "internvl", "pixtral", "phi")


def _openrouter_api_key() -> str:
    """The user's OpenRouter API key (secrets file → legacy → env)."""
    return app_secrets.get_secret("openrouter_api_key", env="OPENROUTER_API_KEY")


# OpenRouter's free router meta-model: given a request, it auto-selects among
# free models. We pin it as the default `model` and STEER it (provider sort for
# speed/uptime + a pinned vision fallback list) toward quick, reliable, image-
# capable models — "implement using this free router with a preference for quick,
# reliable, vision models."
OPENROUTER_FREE_ROUTER = "openrouter/free"
# OpenRouter caps the routing `models` fallback array at 3 entries — sending more
# 400s the request ("'models' array must have 3 items or fewer."), which silently
# drops every call to the offline parser. Pin at most this many vision fallbacks.
_OPENROUTER_MODELS_MAX = 3
_OPENROUTER_FALLBACK_N = _OPENROUTER_MODELS_MAX   # free vision models pinned as router fallbacks


def _openrouter_default_cfg() -> dict:
    return {"model": OPENROUTER_FREE_ROUTER, "send_image": True, "free_only": True,
            "resolved_model": "", "models_fallback": []}


def _fetch_openrouter_models(timeout: float = 6.0) -> list[dict]:
    """GET the OpenRouter model catalogue. Returns the raw model list (or [])."""
    headers = {"Accept": "application/json", **OPENROUTER_ATTR_HEADERS}
    key = _openrouter_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = _pr.OPENROUTER_BASE_URL.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return []
            data = json.loads(resp.read() or b"{}")
            return data.get("data") or []
    except Exception:
        return []


# OpenRouter free-tier DAILY cap on :free models: 50 requests/day under $10 of
# lifetime credits, 1000/day at or over. We query the lifetime-purchased credits
# from /credits to decide which applies (the live per-day count is tallied locally
# in process_receipts). The per-minute cap is a fixed ~20.
_OPENROUTER_FREE_CAP_LOW    = 50
_OPENROUTER_FREE_CAP_HIGH   = 1000
_OPENROUTER_CREDIT_THRESHOLD = 10.0
_OPENROUTER_PER_MIN         = 20
_OR_CAP_TTL                 = 300.0   # seconds to cache the /credits lookup
_or_cap_cache: dict = {"at": 0.0, "data": None}


def _fetch_openrouter_credits(timeout: float = 6.0) -> dict | None:
    """GET /credits → {total_credits, total_usage} (lifetime, USD). None on failure."""
    key = _openrouter_api_key()
    if not key:
        return None
    url = _pr.OPENROUTER_BASE_URL.rstrip("/") + "/credits"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {key}",
               **OPENROUTER_ATTR_HEADERS}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read() or b"{}")
            return data.get("data") or data
    except Exception:
        return None


def _openrouter_cap_info(force: bool = False) -> dict:
    """Daily free-request cap (50/1000) inferred from purchased credits, cached.

    The cap rarely changes, so the /credits lookup is cached for _OR_CAP_TTL; the
    live daily *count* (tallied locally) is always fresh regardless of this cache.
    """
    now = time.time()
    cached = _or_cap_cache.get("data")
    if not force and cached is not None and now - _or_cap_cache["at"] < _OR_CAP_TTL:
        return cached
    info: dict = {"cap": _OPENROUTER_FREE_CAP_LOW, "per_min": _OPENROUTER_PER_MIN,
                  "total_credits": None, "total_usage": None, "credits_known": False}
    credits = _fetch_openrouter_credits()
    if credits is not None:
        try:
            tc = float(credits.get("total_credits"))
            info["total_credits"] = tc
            info["credits_known"] = True
            info["cap"] = (_OPENROUTER_FREE_CAP_HIGH if tc >= _OPENROUTER_CREDIT_THRESHOLD
                           else _OPENROUTER_FREE_CAP_LOW)
        except (TypeError, ValueError):
            pass
        try:
            info["total_usage"] = float(credits.get("total_usage"))
        except (TypeError, ValueError):
            pass
    _or_cap_cache["at"] = now
    _or_cap_cache["data"] = info
    return info


def _model_is_free(m: dict) -> bool:
    """Free = zero prompt AND completion token cost (image/request cost ignored)."""
    pricing = m.get("pricing") or {}
    def _zero(v) -> bool:
        try:
            return float(v) == 0.0
        except (TypeError, ValueError):
            return False
    return _zero(pricing.get("prompt")) and _zero(pricing.get("completion"))


def _model_is_vision(m: dict) -> bool:
    """True when the model accepts image input (needed to read receipt photos)."""
    arch = m.get("architecture") or {}
    mods = arch.get("input_modalities") or []
    if isinstance(mods, list) and any("image" in str(x).lower() for x in mods):
        return True
    return "image" in str(arch.get("modality") or "").lower()


# Reasoning/thinking models spend their token budget on hidden reasoning and
# routinely return EMPTY content for a plain transcription/extraction — a poor OCR
# / vision pick (and the likely source of the "OCR (LLM) – no text" failures). We
# don't exclude them (a free vision list can be thin), just rank them LAST so the
# fallback chain only loops back to a reasoning model after the non-reasoning ones
# are exhausted.
_REASONING_MARKERS = ("reasoning", "thinking", "-think", ":think", "qwq",
                      "-r1", "r1-", ":r1", "/r1", "o1-", "o3-", "deepseek-r")


def _model_is_reasoning(m: dict) -> bool:
    """True for a reasoning-first model (id/name marker), which tends to emit an
    empty answer on a transcription/extraction task."""
    blob = f"{m.get('id') or ''} {m.get('name') or ''}".lower()
    return any(t in blob for t in _REASONING_MARKERS)


def _openrouter_score(m: dict) -> tuple:
    """Sort key (higher first): non-reasoning first, then preferred family, then
    'quick' (small/fast) variants, then larger context. Biases the pick toward
    quick, reliable, *non-reasoning* vision models."""
    mid = str(m.get("id") or "").lower()
    ctx = m.get("context_length") or (m.get("top_provider") or {}).get("context_length") or 0
    try:
        ctx = int(ctx)
    except (TypeError, ValueError):
        ctx = 0
    fam = 0
    for i, tag in enumerate(_OPENROUTER_PREFERRED):
        if tag in mid:
            fam = len(_OPENROUTER_PREFERRED) - i
            break
    # "quick": small / distilled variants respond fastest.
    fast = 1 if any(t in mid for t in
                    ("flash", "mini", "lite", "small", "nano", "fast",
                     "8b", "7b", "4b", "3b", "2b", "1b")) else 0
    # Most significant: reasoning models sort to the very end of the chain.
    not_reasoning = 0 if _model_is_reasoning(m) else 1
    return (not_reasoning, fam, fast, ctx)


def _openrouter_free_vision_models() -> list[dict]:
    """Free, image-capable OpenRouter models, best first. [] when offline/no key."""
    models = _fetch_openrouter_models()
    free_vision = [m for m in models if _model_is_free(m) and _model_is_vision(m)]
    free_vision.sort(key=_openrouter_score, reverse=True)
    return free_vision


def _openrouter_autopick() -> str:
    """Resolve the single best free vision model id (or '' if none reachable)."""
    cands = _openrouter_free_vision_models()
    return str(cands[0].get("id") or "") if cands else ""


def _openrouter_vision_fallback() -> list[str]:
    """Top free, image-capable model ids (quick-first) to pin as router fallbacks."""
    return [str(m.get("id")) for m in _openrouter_free_vision_models()
            if m.get("id")][:_OPENROUTER_FALLBACK_N]


def _openrouter_extra_body(orc: dict) -> dict:
    """OpenRouter routing preferences merged into every completion request.

    Biases the free router toward quick + reliable providers (`provider.sort`,
    `allow_fallbacks`) and pins a vision-capable free fallback list (`models`) so
    an image request never lands on a text-only model. Built from stored config
    only — no network at apply time. The documented pattern is `model` (primary,
    here the free router) + `models` (fallbacks tried if the primary is down).
    """
    body: dict = {
        "provider": {
            "sort":            "throughput",  # "quick" — fastest-generating providers first
            "allow_fallbacks": True,          # reliability — fail over if a provider is down
        },
    }
    # Cap at OpenRouter's hard limit even if an older config persisted more (a
    # 4+-item array 400s every request) — truncating here fixes saved configs
    # without needing a re-Apply.
    fb = (orc.get("models_fallback") or [])[:_OPENROUTER_MODELS_MAX]
    if fb:
        body["models"] = list(fb)
    return body


def _reset_local_llm_runtime() -> None:
    """Restore local-server client defaults: no cloud key/headers/body, image OK."""
    _pr.LLM_API_KEY = os.getenv("LLM_API_KEY") or "lmstudio"
    _pr.LLM_EXTRA_HEADERS = {}
    _pr.LLM_EXTRA_BODY = {}
    _pr.LLM_ALLOW_IMAGE = True


def _apply_openrouter_config(cfg: dict) -> None:
    """Point the inference client at OpenRouter for this session (per cfg)."""
    orc = {**_openrouter_default_cfg(), **(cfg.get("openrouter") or {})}
    key = _openrouter_api_key()
    _pr.LMSTUDIO_BASE_URL = _pr.OPENROUTER_BASE_URL
    _pr.LLM_API_KEY       = key or "lmstudio"
    _pr.LLM_EXTRA_HEADERS = dict(OPENROUTER_ATTR_HEADERS)
    _pr.LLM_EXTRA_BODY    = _openrouter_extra_body(orc)
    _pr.LLM_ALLOW_IMAGE   = bool(orc.get("send_image", True))
    model = (orc.get("resolved_model") or "").strip()
    if not model:
        chosen = str(orc.get("model") or "").strip()
        # "auto" is resolved at save time; the free-router slug or an explicit id
        # is used directly as the request model.
        model = "" if chosen == "auto" else chosen
    if model:
        _pr.set_active_model(model)


def _first_run_provider_default() -> None:
    """First-run convenience: adopt the OpenRouter free router when an
    OPENROUTER_API_KEY is present in the environment AND nothing has been
    configured yet — so exporting the key (or putting it in .env) is enough to use
    the cloud free router with zero extra clicks.

    It NEVER overrides an explicit choice: it acts only on a truly fresh config (no
    provider / llm_server / llm_model_config / openrouter keys) and persists the
    decision so it's visible and stable. Run BEFORE _apply_llm_server_config.
    """
    if not (os.getenv("OPENROUTER_API_KEY") or "").strip():
        return
    cfg = _load_config()
    if (cfg.get("provider") or cfg.get("llm_server")
            or cfg.get("llm_model_config") or cfg.get("openrouter")):
        return
    cfg["provider"]   = "openrouter"
    cfg["openrouter"] = _openrouter_default_cfg()        # model = openrouter/free
    _save_config(cfg)
    print("[models] First run: OPENROUTER_API_KEY detected — defaulting to the "
          "OpenRouter free router (openrouter/free). Change it in Settings → AI Model.")


def _apply_local_llm_config(cfg: dict) -> None:
    """Restore the persisted LOCAL server URL into process_receipts.LMSTUDIO_BASE_URL.

    Handles the legacy ``llm_model_config`` key (Configure Model dialog) and the
    canonical ``llm_server`` key (LLM Server card); ``llm_server`` wins. An
    EXPLICIT ``server_type: custom`` selection is always honoured — even with a
    blank URL it resolves to the localhost default, NEVER silently to the bundled
    docker URL. (That old fall-through is what stranded users on :11434.)
    """
    _reset_local_llm_runtime()

    llm_model_cfg = cfg.get("llm_model_config") or {}
    if llm_model_cfg.get("model_id"):
        _pr.set_active_model(str(llm_model_cfg["model_id"]))
    _legacy_url = ""
    if llm_model_cfg.get("server_type") == "docker":
        _legacy_url = _docker_llm_url()
    elif llm_model_cfg.get("base_url"):
        _legacy_url = _normalize_llm_url(str(llm_model_cfg["base_url"]))

    llm_srv  = cfg.get("llm_server") or {}
    srv_type = llm_srv.get("server_type")
    if srv_type == "docker":
        _pr.LMSTUDIO_BASE_URL = _docker_llm_url()
    elif srv_type == "custom":
        _pr.LMSTUDIO_BASE_URL = (_normalize_llm_url(str(llm_srv["base_url"]))
                                 if llm_srv.get("base_url")
                                 else "http://127.0.0.1:1234/v1")
    elif llm_srv.get("base_url"):
        _pr.LMSTUDIO_BASE_URL = _normalize_llm_url(str(llm_srv["base_url"]))
    elif _legacy_url:
        _pr.LMSTUDIO_BASE_URL = _legacy_url


def _apply_llm_server_config(cfg: dict | None = None) -> None:
    """Restore the active LLM provider/endpoint before any model query.

    Dispatches on ``cfg['provider']`` — ``"local"`` (default: LM Studio / custom
    URL / bundled docker) or ``"openrouter"`` (cloud router). Kept under this name
    because the startup path and the test-suite call it. Run BEFORE
    initialize_models so the very first query uses the right endpoint + key.
    """
    cfg = cfg if cfg is not None else _load_config()
    if (cfg.get("provider") or "local").strip() == "openrouter":
        _apply_openrouter_config(cfg)
    else:
        _apply_local_llm_config(cfg)


def _persist_model_config() -> None:
    """Save the current single-model selection + LLM-OCR toggle to config."""
    cfg = _load_config()
    cfg["models"] = {
        "active":  _pr._active_distill_model,
        "llm_ocr": bool(_pr._llm_ocr_enabled),
    }
    _save_config(cfg)


def _audit_settings() -> dict:
    """Current spending/date-warning thresholds as the UI sees them (None = off)."""
    return {
        "amount_limits": dict(_pr.AMOUNT_LIMITS),
        "max_age_days":  _pr.MAX_RECEIPT_AGE_DAYS,
    }


def _coerce_pos_num(v):
    """Parse a settings value to a positive number, or None for blank/invalid/≤0."""
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _apply_audit_config(cfg: dict | None = None) -> dict:
    """Push persisted spending/date-warning thresholds into process_receipts."""
    cfg = cfg if cfg is not None else _load_config()
    audit = cfg.get("audit") or {}
    limits = audit.get("amount_limits") or {}
    for cat in ("fuel", "mats", "misc"):
        if cat in limits:
            _pr.AMOUNT_LIMITS[cat] = _coerce_pos_num(limits[cat])
    if "max_age_days" in audit:
        n = _coerce_pos_num(audit["max_age_days"])
        _pr.MAX_RECEIPT_AGE_DAYS = int(n) if n is not None else None
    return _audit_settings()


# ── SSE broadcast helpers ──────────────────────────────────────────────────────

def _broadcast(event: dict) -> None:
    # Every log line that streams to the Processing & Errors panel is also captured
    # into the in-progress run (if any), so the Run Log is a complete, reviewable
    # record of the same stream — no extra plumbing at the ~20 log call sites.
    if event.get("type") == "log":
        _append_run_line(event.get("message", ""), event.get("level", "info"))
    # In multi-user mode an event belongs to the user whose context is broadcasting
    # it (the request handler's user, or the worker task's user) and is delivered
    # ONLY to that user's SSE clients — so one user never sees another's board/log.
    # Single-user mode: everyone is "default", so this is a no-op filter.
    target = multiuser.cur_ws().user_id
    with _sub_lock:
        for sub in list(_subscribers):
            if multiuser.ENABLED and sub.user_id != target:
                continue
            try:
                sub.q.put_nowait(event)
            except Full:
                # Slow/stuck client: drop its oldest queued event to bound memory,
                # then enqueue the newest so it still gets the latest state.
                try:
                    sub.q.get_nowait()
                    sub.q.put_nowait(event)
                except Exception:
                    pass
            except Exception:
                pass


def _emit_log(message: str, level: str = "info") -> None:
    """Broadcast a log line (→ Processing & Errors panel) with a severity level.
    `level` is one of ``info`` / ``warn`` / ``error`` and rides along to the UI
    and into the captured run log."""
    _broadcast({"type": "log", "message": message, "level": level})


def _append_run_line(message: str, level: str = "info") -> None:
    """Append one log line to the run currently being assembled, if any.
    Thread-safe and capped so a giant batch can't grow the buffer unbounded."""
    with _current_run_lock:
        run = multiuser.cur_ws().current_run
        if run is None:
            return
        lines = run["lines"]
        lines.append({"t": datetime.now().strftime("%H:%M:%S"),
                      "level": level, "message": message})
        if len(lines) > RUN_MAX_LINES:
            del lines[:len(lines) - RUN_MAX_LINES]


class _Subscriber:
    """An SSE client's event queue tagged with the user it belongs to, so
    ``_broadcast`` can deliver each event only to its owner's clients."""
    __slots__ = ("q", "user_id")

    def __init__(self, q: Queue, user_id: str):
        self.q = q
        self.user_id = user_id


def _add_subscriber(user_id: str | None = None) -> _Subscriber:
    q: Queue = Queue(maxsize=SSE_QUEUE_MAX)
    sub = _Subscriber(q, user_id or multiuser.cur_ws().user_id)
    with _sub_lock:
        _subscribers.append(sub)
    return sub


def _remove_subscriber(sub: "_Subscriber") -> None:
    with _sub_lock:
        try:
            _subscribers.remove(sub)
        except ValueError:
            pass


def _pending_count(user_id: str) -> int:
    """How many queued (not-yet-processed) items belong to ``user_id``. The work
    queue is shared, so each item is user-tagged; single-user mode counts them all."""
    with _work_lock:
        if not multiuser.ENABLED:
            return len(_work_queue)
        return sum(1 for it in _work_queue
                   if it.get("user_id", multiuser.DEFAULT_USER) == user_id)


def _tag_item(item: dict) -> dict:
    """Stamp a queue item with the enqueuing user so the worker routes its board /
    results writes to the right workspace. Idempotent; defaults to the current
    request/task user (the default user in single-user mode)."""
    item.setdefault("user_id", multiuser.cur_ws().user_id)
    return item


def _watch_workspaces() -> list:
    """Workspaces the background loops (watcher, stall checker) should scan: just
    the default in single-user mode; the default plus every user with data on disk
    in multi-user mode."""
    if not multiuser.ENABLED:
        return [multiuser.default_workspace()]
    seen = {multiuser.DEFAULT_USER}
    out = [multiuser.default_workspace()]
    for uid in multiuser.discover_user_ids():
        if uid not in seen:
            out.append(multiuser.get_workspace(uid))
            seen.add(uid)
    return out


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
              "_llm_field_boxes", "_review_required", "_approved", "notes",
              "_already_sent", "_force_included"):
        if k in data:
            out[k] = data[k]
    return out


def _cache_item(item: dict) -> None:
    """Cache queue item data for stall recovery."""
    with _item_cache_lock:
        _item_cache[item["filename"]] = item


# ── Sent-ledger (dedup across reports) ─────────────────────────────────────────
# When a report is *sent*, every receipt in it is recorded here (per workspace).
# A later add of the same receipt (same vendor/date/amount) is then surfaced as
# "already reported" and excluded from the next report unless the user overrides.

def _already_sent(identity: tuple) -> dict | None:
    """Return the ledger entry matching this receipt identity, else None.

    An amount of 0 means "no usable identity" — never treated as a match.
    JSON round-trips the stored key as a list, so compare list-to-list.
    """
    if not identity or identity[2] == 0:
        return None
    target = list(identity)
    with _sent_ledger_lock:
        for entry in _sent_ledger:
            if entry.get("key") == target:
                return entry
    return None


def _record_sent(results, report_name: str = "") -> int:
    """Record every receipt in a just-sent report into the per-workspace ledger.

    Returns the number of newly-added entries. Skips receipts with no usable
    amount (unreliable identity) and de-dupes against what's already recorded.
    Also advances the ``last_report_date`` max-date watermark.
    """
    now = int(time.time())
    added = 0
    with _sent_ledger_lock:
        existing = {tuple(e["key"]) for e in _sent_ledger if e.get("key")}
        ws = multiuser.cur_ws()
        max_date = ws.last_report_date or ""
        for r in results:
            if not isinstance(r, dict):
                continue
            key = _pr.receipt_identity(r)
            if key[2] == 0:
                continue
            d = r.get("date") or ""
            if d and d > max_date:
                max_date = d
            if tuple(key) in existing:
                continue
            existing.add(tuple(key))
            _sent_ledger.append({
                "key":      list(key),
                "vendor":   r.get("vendor") or "",
                "date":     d,
                "amount":   key[2],
                "filename": r.get("_new_filename") or r.get("_file") or "",
                "report":   report_name,
                "sent_at":  now,
            })
            added += 1
        if len(_sent_ledger) > SENT_LEDGER_MAX:  # cap, dropping oldest
            del _sent_ledger[:len(_sent_ledger) - SENT_LEDGER_MAX]
        ws.last_report_date = max_date
    return added


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
        with _bench_lock:
            bench_copy = list(_benchmarks)
        with _runs_lock:
            runs_copy = copy.deepcopy(_runs)
        with _sent_ledger_lock:
            ledger_copy = copy.deepcopy(_sent_ledger)
            last_report_date = multiuser.cur_ws().last_report_date
        payload = {
            "results":      results_copy,
            "kanban":       kanban_copy,
            "last_context": context_copy,
            "benchmarks":   bench_copy,
            "runs":         runs_copy,
            "sent_ledger":  ledger_copy,
            "last_report_date": last_report_date,
            # Live OpenRouter daily-request tally so the cap count survives a
            # restart within the same UTC day (a stale day is dropped on restore).
            "openrouter_usage": _pr.get_openrouter_usage(),
        }
        OUT_FOLDER.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(payload, default=str)
        # Unique tmp name + a lock so concurrent persisters can't clobber each
        # other's tmp file and replace() a half-written one into place.
        with _persist_lock:
            tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(blob)
                tmp.replace(STATE_FILE)
            finally:
                tmp.unlink(missing_ok=True)
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

    bench = payload.get("benchmarks")
    if isinstance(bench, list):
        with _bench_lock:
            _benchmarks.clear()
            _benchmarks.extend(b for b in bench if isinstance(b, dict))
            del _benchmarks[BENCH_MAX_ENTRIES:]

    runs = payload.get("runs")
    if isinstance(runs, list):
        with _runs_lock:
            _runs.clear()
            _runs.extend(r for r in runs if isinstance(r, dict))
            del _runs[RUNS_MAX_ENTRIES:]

    ledger = payload.get("sent_ledger")
    if isinstance(ledger, list):
        with _sent_ledger_lock:
            _sent_ledger.clear()
            _sent_ledger.extend(e for e in ledger if isinstance(e, dict))
            del _sent_ledger[SENT_LEDGER_MAX:]
    lrd = payload.get("last_report_date")
    if isinstance(lrd, str):
        multiuser.cur_ws().last_report_date = lrd

    # OpenRouter usage is an instance-level (cross-user) daily tally — only the
    # default workspace's state file is its source of truth, so per-user restores
    # don't clobber it with a stale copy.
    if multiuser.cur_ws().user_id == multiuser.DEFAULT_USER:
        or_usage = payload.get("openrouter_usage")
        if isinstance(or_usage, dict):
            _pr.set_openrouter_usage(or_usage.get("date"), or_usage.get("count"))

    with _results_lock:
        n = len(_results)
    if n:
        who = "" if multiuser.cur_ws().user_id == multiuser.DEFAULT_USER else f" for {multiuser.cur_ws().user_id}"
        print(f"[state] Restored {n} completed receipt(s) from previous session{who}")


def _restore_all() -> None:
    """Restore the default workspace and every per-user workspace with data on disk
    (multi-user mode). Single-user mode restores just the default."""
    _restore_state()
    if multiuser.ENABLED:
        for uid in multiuser.discover_user_ids():
            tok = multiuser.bind_user(uid)
            try:
                _restore_state()
            finally:
                multiuser.reset(tok)


def _persist_all() -> None:
    """Persist every known workspace (used on shutdown). Per-call persists during
    runtime are already scoped to the acting user via the context proxies."""
    for ws in multiuser.iter_workspaces():
        tok = multiuser.bind(ws)
        try:
            _persist_state()
        finally:
            multiuser.reset(tok)


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
            _abort_current_run()   # don't strand a half-built run on a crash
            _emit_log(f"[worker] recovered from error: {exc}", level="error")
            time.sleep(1)


# ── Run log: what gets sent + full per-run detail ──────────────────────────────

def _llm_instructions_payload() -> dict:
    """A self-documenting snapshot of EXACTLY what the app sends to the LLM for the
    active provider: the system+user prompt for every pipeline stage, the privacy
    gate, the OpenRouter routing body and attribution headers. Nothing is hidden.
    Powers the "Instructions sent to the model" panel and is embedded in each run
    log so a saved run is self-contained."""
    cfg         = _load_config()
    provider    = (cfg.get("provider") or "local").strip()
    allow_image = bool(getattr(_pr, "LLM_ALLOW_IMAGE", True))
    ocr_model   = _pr._active_ocr_model or ""
    stages = [
        {
            "stage": "1 · OCR transcription (vision LLM)",
            "runs_when": ("When an LLM-OCR model is selected and images may be sent — "
                          "transcribes the image so the distiller can cross-check it"
                          if (ocr_model and allow_image) else
                          "Off — built-in RapidOCR only (no LLM-OCR model, or image "
                          "sending disabled)"),
            "sends_image": True,
            "system": "",
            "user": _pr.OLMOCR_RAW_PROMPT,
        },
        {
            "stage": "2 · Distillation (OCR text → structured fields)",
            "runs_when": "Every receipt that produced OCR text — the main extraction call",
            "sends_image": False,
            "system": "You are a receipt data extractor. Respond with valid JSON only.",
            "user": _pr._UNIFIED_DISTILLATION_TEMPLATE.replace(
                "{ocr_text}", "<the receipt's OCR text is inserted here>"),
        },
        {
            "stage": "3 · Vision rescue (image → fields)",
            "runs_when": ("Only when OCR text is missing or low-confidence AND images "
                          "may be sent — a vision model reads the receipt directly"
                          if allow_image else
                          "Off — image sending is disabled (OCR-text-only privacy mode)"),
            "sends_image": True,
            "system": "You are a receipt data extractor. Always respond with valid JSON only.",
            "user": _pr._GEMMA_VISION_TEMPLATE,
        },
    ]
    return {
        "provider":         provider,
        "endpoint":         getattr(_pr, "LMSTUDIO_BASE_URL", ""),
        "distill_model":    _pr._active_distill_model or "(auto-selected)",
        "ocr_model":        ocr_model or "(built-in RapidOCR only)",
        "send_image":       allow_image,
        "thinking_enabled": bool(getattr(_pr, "_thinking_enabled", True)),
        "extra_headers":    dict(getattr(_pr, "LLM_EXTRA_HEADERS", {}) or {}),
        "extra_body":       dict(getattr(_pr, "LLM_EXTRA_BODY", {}) or {}),
        "stages":           stages,
    }


def _begin_run(batch: list) -> dict:
    """Start assembling a fresh run log for a batch; capture what we'll send.
    The run is held on the current user's workspace (per-user run log)."""
    ws = multiuser.cur_ws()
    ws.run_seq += 1
    run = {
        "id":            datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{ws.run_seq:04d}",
        "ts_start":      datetime.now().isoformat(timespec="seconds"),
        "ts_end":        None,
        "count":         len(batch),
        "total_seconds": None,
        "instructions":  _llm_instructions_payload(),
        "lines":         [],
        "receipts":      [],
    }
    with _current_run_lock:
        ws.current_run = run
    return run


def _record_run_receipt(fname: str, status: str, data, steps, *, error: str = "") -> None:
    """Attach a finished receipt to the active run AND stream its full per-step
    breakdown into the live log (so the Processing & Errors panel shows every
    detail: image-prep, OCR, distillation, classify, audit …)."""
    safe = data or {}
    cat  = safe.get("_category") or ""
    entry = {
        "filename":     fname,
        "new_filename": safe.get("_new_filename", ""),
        "status":       status,
        "vendor":       safe.get("vendor", ""),
        "amount":       safe.get("amount", None),
        "date":         safe.get("date", ""),
        "category":     cat,
        "confidence":   safe.get("_confidence", None),
        "ocr_engine":   safe.get("_ocr_engine", ""),
        "proc_seconds": safe.get("_proc_seconds", None),
        "error":        error or safe.get("_error", ""),
        "steps":        [dict(s) for s in (steps or [])],
    }
    with _current_run_lock:
        _run = multiuser.cur_ws().current_run
        if _run is not None:
            _run["receipts"].append(entry)

    head = f"▸ {fname}"
    if entry["new_filename"] and entry["new_filename"] != fname:
        head += f" → {entry['new_filename']}"
    _emit_log(f"{head}  [{status}]", level=("error" if status == "failed" else "info"))
    for s in entry["steps"]:
        ok  = s.get("ok", True)
        dur = f" · {s['duration_s']}s" if s.get("duration_s") else ""
        det = f" — {s['detail']}" if s.get("detail") else ""
        _emit_log(f"    {'✓' if ok else '✗'} {s.get('label') or s.get('step')}{det}{dur}",
                  level=("info" if ok else "error"))
    if status == "done":
        amt  = entry["amount"] or 0
        conf = f" · {entry['confidence']}% conf" if entry["confidence"] is not None else ""
        try:
            _emit_log(f"    = {cat or '?'} · {entry['vendor'] or '?'} · ${float(amt):.2f}{conf}")
        except (TypeError, ValueError):
            _emit_log(f"    = {cat or '?'} · {entry['vendor'] or '?'} · {amt}{conf}")
    elif error:
        _emit_log(f"    ! {error}", level="error")


def _finalize_run(run: "dict | None", total_seconds: "float | None") -> None:
    """Close out a run and push it onto the newest-first, capped history.
    Runs in the current user's workspace context (bound by the caller)."""
    if run is None:
        return
    run["ts_end"] = datetime.now().isoformat(timespec="seconds")
    if total_seconds is not None and run.get("total_seconds") is None:
        run["total_seconds"] = round(total_seconds, 1)
    with _runs_lock:
        _runs.insert(0, run)
        del _runs[RUNS_MAX_ENTRIES:]
    ws = multiuser.cur_ws()
    with _current_run_lock:
        if ws.current_run is run:
            ws.current_run = None


def _abort_current_run() -> None:
    """Salvage a half-built run after a worker crash so it isn't stranded — across
    every workspace, since the crash may have unbound the worker's user context."""
    for ws in multiuser.iter_workspaces():
        with ws.current_run_lock:
            run = ws.current_run
        if run is not None:
            tok = multiuser.bind(ws)
            try:
                _finalize_run(run, None)
            finally:
                multiuser.reset(tok)


def _format_run_text(run: dict) -> str:
    """Render a run as a plain-text report for download/review."""
    instr = run.get("instructions") or {}
    out: list[str] = []
    out.append(f"RUN {run.get('id')}")
    out.append(f"Started: {run.get('ts_start')}   Ended: {run.get('ts_end')}")
    out.append(f"Receipts: {run.get('count')}   Total: {run.get('total_seconds')}s")
    out.append("")
    out.append("=== WHAT WAS SENT TO THE MODEL ===")
    out.append(f"  Provider:      {instr.get('provider')}")
    out.append(f"  Endpoint:      {instr.get('endpoint')}")
    out.append(f"  Distill model: {instr.get('distill_model')}")
    out.append(f"  OCR model:     {instr.get('ocr_model')}")
    out.append(f"  Send image:    {instr.get('send_image')}")
    out.append(f"  Reasoning:     {instr.get('thinking_enabled')}")
    if instr.get("extra_headers"):
        out.append(f"  Extra headers: {json.dumps(instr['extra_headers'])}")
    if instr.get("extra_body"):
        out.append(f"  Routing body:  {json.dumps(instr['extra_body'])}")
    for st in instr.get("stages") or []:
        out.append("")
        out.append(f"  -- {st.get('stage')} --")
        out.append(f"     When: {st.get('runs_when')}")
        out.append(f"     Sends image: {st.get('sends_image')}")
        if st.get("system"):
            out.append(f"     System prompt: {st['system']}")
        out.append("     Instructions:")
        for ln in (st.get("user") or "").splitlines():
            out.append(f"       {ln}")
    out.append("")
    out.append("=== LOG ===")
    for ln in run.get("lines") or []:
        out.append(f"  {ln.get('t','')}  {ln.get('message','')}")
    out.append("")
    out.append("=== RECEIPTS ===")
    for r in run.get("receipts") or []:
        head = f"  - {r.get('filename')} [{r.get('status')}]"
        if r.get("new_filename") and r.get("new_filename") != r.get("filename"):
            head += f" -> {r.get('new_filename')}"
        out.append(head)
        if r.get("vendor") or r.get("amount") is not None:
            try:
                amt = f"${float(r.get('amount') or 0):.2f}"
            except (TypeError, ValueError):
                amt = str(r.get("amount"))
            out.append(f"      {r.get('category') or '?'} · {r.get('vendor') or '?'} · {amt}")
        for s in r.get("steps") or []:
            mark = "ok " if s.get("ok", True) else "ERR"
            line = f"      [{mark}] {s.get('label') or s.get('step')}"
            if s.get("detail"):
                line += f" — {s['detail']}"
            if s.get("duration_s"):
                line += f" ({s['duration_s']}s)"
            out.append(line)
        if r.get("error"):
            out.append(f"      error: {r.get('error')}")
    return "\n".join(out)


def _receipts_output_dir() -> Path:
    """Completed receipts are grouped into a short, dated subfolder under the
    completed-receipts folder — e.g. ``receipts/Processed_2026-06-13`` — so each
    day's processed receipts stay tidily together instead of piling into one flat
    directory. The subfolder is created on demand and reused for the day."""
    d = IMAGES_FOLDER / f"Processed_{date.today().isoformat()}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drain_once() -> bool:
    """Process one batch from the queue. Returns False when the queue was empty.

    The shared work queue carries items from every user; one call processes the
    items belonging to the oldest-queued user as a single batch (its own run log,
    bound to that user's workspace) and leaves other users' items for the next
    call — a simple round-robin that stops one flooding user from starving another.
    In single-user mode every item is "default", so this is one batch as before.
    """
    with _work_lock:
        if not _work_queue:
            return False
        batch_uid = _work_queue[0].get("user_id", multiuser.DEFAULT_USER)
        batch = [it for it in _work_queue
                 if it.get("user_id", multiuser.DEFAULT_USER) == batch_uid]
        rest  = [it for it in _work_queue
                 if it.get("user_id", multiuser.DEFAULT_USER) != batch_uid]
        _work_queue.clear()
        _work_queue.extend(rest)

    token = multiuser.bind_user(batch_uid)
    try:
        _run_batch(batch, batch_uid)
    finally:
        multiuser.reset(token)
    return True


def _run_batch(batch: list, batch_uid: str) -> None:
    """Process one user's batch — the caller has bound that user's workspace, so
    every board/results/run-log/persist call below is scoped to them."""
    _batch_t0 = time.perf_counter()
    # Reset the per-batch LLM-OCR throttle breaker so a previous batch's free-tier
    # throttling doesn't carry over and pre-suspend the vision pass for this one.
    _pr.reset_batch_llm_state()
    # Open a fresh run log BEFORE the first line so the whole batch is captured.
    run = _begin_run(batch)
    instr = run["instructions"]
    _emit_log(f"[worker] Processing {len(batch)} receipt(s)…")
    _emit_log(f"[run {run['id']}] provider={instr['provider']} · "
              f"model={instr['distill_model']} · ocr={instr['ocr_model']} · "
              f"send_image={instr['send_image']} · reasoning={instr['thinking_enabled']}")
    # Build the extraction client for the active provider (local LM Studio or
    # OpenRouter). Snapshotted once per batch — config is stable mid-batch.
    client = _pr.make_client()

    def _gated_extract(*args):
        # Pool tasks don't inherit the worker thread's contextvars, so re-bind this
        # batch's user inside the task — the per-receipt status callback writes the
        # kanban and broadcasts under it.
        tok = multiuser.bind_user(batch_uid)
        # The pool is sized to a fixed ceiling; this gate enforces the live
        # "process N at a time" limit so the slider takes effect mid-batch.
        _concurrency_gate.acquire()
        try:
            return _extract_receipt_with_status(*args)
        finally:
            _concurrency_gate.release()
            multiuser.reset(tok)

    futures_map: dict = {}
    ceiling = max(CONCURRENCY_CEILING, int(_pr.MAX_PARALLEL_REQUESTS or 0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=ceiling) as ex:
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
                _gated_extract, client, path, make_cb(fname, step_log), step_log,
                bool(item.get("force_llm_ocr", False)),
            )
            futures_map[future] = item

        # Drive the "Processing & Errors" progress bar: emit a progress event as
        # each receipt finishes so the widget actually moves (it was wired in the
        # SPA but the worker never emitted the event — bar was stuck at 0%).
        _progress_total = len(futures_map)
        _progress_done  = 0
        _broadcast({"type": "progress", "current": 0,
                    "total": _progress_total, "filename": ""})

        for future in concurrent.futures.as_completed(futures_map):
            if _worker_cancel.is_set():
                break
            item  = futures_map[future]
            fname = item["filename"]
            path  = Path(item["path"])
            steps = item.get("_steps", [])
            _progress_done += 1
            _broadcast({"type": "progress", "current": _progress_done,
                        "total": _progress_total, "filename": fname})
            try:
                data = future.result()
            except Exception as exc:
                data = None
                _emit_log(f"[worker] ERROR {fname}: {exc}", level="error")

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
                _record_run_receipt(fname, "failed", partial,
                                    partial.get("_steps", []), error=fail_reason)
                continue

            # Canonicalize the vendor against the known-vendor database (rules-based,
            # no LLM) just before classification: an exact/glyph match rewrites the
            # displayed name to the canonical brand and settles the category.
            _pr.canonicalize_vendor(data)
            category = classify_category(data)
            data["_category"]  = category
            data["job_name"]   = item.get("job_name") or DEFAULT_JOB_NAME
            data["job_number"] = item.get("job_number") or DEFAULT_JOB_NUMBER
            audit_flag = audit_amount(data, data.get("_raw_ocr") or "")
            # User-configured spending/date warnings (default none). Prepended so
            # a warning shows up as the card's headline flag when present.
            warn_flags = [{"flag": w} for w in _pr.audit_warning_flags(data, category)]
            flags = warn_flags + _pr._normalize_flags(data.get("flags") or [])
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

            # Sent-ledger: if this receipt was already included in a previously
            # sent report, mark it "already reported" so the board surfaces it and
            # report generation excludes it — unless the user forced it back in.
            if not data.get("_force_included"):
                prior = _already_sent(_pr.receipt_identity(data))
                if prior:
                    data["_already_sent"] = {
                        "report":  prior.get("report", ""),
                        "date":    prior.get("date", ""),
                        "sent_at": prior.get("sent_at"),
                    }
                    if not data.get("_flag"):
                        data["_flag"] = "Already reported in a previously sent report"
                    data["_review_required"] = True

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
            # An emailed e-receipt has no photo, but the pipeline renders a filable
            # JPEG copy (faithful HTML render or a text-based fallback) so the report
            # and preview show the actual receipt document the office requires. When
            # that copy exists, it becomes the canonical image (renamed + moved like
            # any photo); the original .html/.txt stays staged and is ignored.
            render_path = data.pop("_render_path", "")
            have_copy   = bool(render_path and Path(render_path).exists())
            src_path    = Path(render_path) if have_copy else path
            final_path  = rename_receipt_image(src_path, data, category, dest_dir)
            data["_new_filename"] = final_path.name
            data["_file"]         = fname
            # Point the spreadsheet/preview at the image unless this is a text source
            # with no rendered copy (then show a clean "image not available" instead
            # of a decode error on the .html/.txt).
            if not data.get("_text_source") or have_copy:
                data["_image_path"] = str(final_path)

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
            # Stream the full per-receipt breakdown (image-prep → OCR → distill →
            # classify → audit) into the live log and capture it in the run.
            _record_run_receipt(fname, "done", data, data.get("_steps", []))

    with _results_lock:
        _detect_duplicates(_results)
        n_done = len(_results)
    n_pending = _pending_count(batch_uid)
    elapsed = time.perf_counter() - _batch_t0
    bench = _record_benchmark(len(batch), elapsed, run.get("receipts"))
    _emit_log(f"[worker] Batch finished — {len(run['receipts'])} receipt(s) in "
              f"{round(elapsed, 1)}s")
    _finalize_run(run, elapsed)
    _persist_state()
    _broadcast({"type": "batch_done", "completed": n_done, "pending": n_pending,
                "benchmark": bench, "run_id": run["id"]})


def _aggregate_step_durations(receipts: list | None) -> list[dict]:
    """Roll a batch's per-receipt step logs into per-step totals.

    Returns one row per distinct pipeline step (OCR, distillation, classify,
    audit, image-prep …) with how many times it ran, how many failed, and the
    total + average seconds it took across the batch — so the benchmark shows
    WHERE the time actually went, not just a single batch total.
    """
    agg: dict[str, dict] = {}
    order: list[str] = []
    for r in (receipts or []):
        for s in (r.get("steps") or []):
            key = s.get("step") or s.get("label") or "?"
            a = agg.get(key)
            if a is None:
                a = agg[key] = {
                    "step":          key,
                    "label":         s.get("label") or key,
                    "count":         0,
                    "failures":      0,
                    "total_seconds": 0.0,
                }
                order.append(key)
            a["count"] += 1
            if not s.get("ok", True):
                a["failures"] += 1
            try:
                a["total_seconds"] += float(s.get("duration_s") or 0)
            except (TypeError, ValueError):
                pass
    rows = []
    for key in order:
        a = agg[key]
        a["total_seconds"] = round(a["total_seconds"], 2)
        a["avg_seconds"] = round(a["total_seconds"] / a["count"], 2) if a["count"] else 0.0
        rows.append(a)
    return rows


def _record_benchmark(count: int, seconds: float,
                      receipts: list | None = None) -> dict | None:
    """Log this batch's wall-time + per-receipt average, tagged with the active
    models AND a per-step time breakdown, so a user can compare LLM speed across
    runs and see which stage dominates. Returns the entry."""
    if count <= 0 or _worker_cancel.is_set():
        return None
    entry = {
        "ts":            datetime.now().isoformat(timespec="seconds"),
        "count":         count,
        "total_seconds": round(seconds, 1),
        "avg_seconds":   round(seconds / count, 2),
        "distill_model": _pr._active_distill_model or "(none)",
        "ocr_model":     _pr._active_ocr_model or "(built-in only)",
        "steps":         _aggregate_step_durations(receipts),
    }
    with _bench_lock:
        _benchmarks.insert(0, entry)
        del _benchmarks[BENCH_MAX_ENTRIES:]
    return entry


def _benchmark_insights(entries: list[dict]) -> dict | None:
    """Aggregate the per-batch benchmark log into headline insights.

    Returns ``None`` when there's nothing recorded yet. Otherwise: totals,
    the weighted average seconds per receipt, throughput (receipts/min), the
    fastest/slowest per-receipt batch, a recent-vs-overall trend, and a
    per-distill-model comparison so a user can see which LLM is quicker as
    runs accumulate.
    """
    rows = [e for e in entries if isinstance(e, dict) and (e.get("count") or 0) > 0]
    if not rows:
        return None

    total_receipts = sum(int(e.get("count") or 0) for e in rows)
    total_seconds = round(sum(float(e.get("total_seconds") or 0) for e in rows), 1)
    avg_per_receipt = round(total_seconds / total_receipts, 2) if total_receipts else 0.0
    throughput = round(total_receipts * 60.0 / total_seconds, 1) if total_seconds else 0.0
    avgs = [float(e.get("avg_seconds") or 0) for e in rows if e.get("avg_seconds") is not None]
    fastest = round(min(avgs), 2) if avgs else 0.0
    slowest = round(max(avgs), 2) if avgs else 0.0

    # Recent (newest entry) vs overall, to surface whether things are speeding up.
    recent_avg = round(float(rows[0].get("avg_seconds") or 0), 2)
    trend = round(recent_avg - avg_per_receipt, 2)

    # Per-distill-model rollup (entries are newest-first; keep that order of first
    # appearance for stable display).
    per_model: dict[str, dict] = {}
    for e in rows:
        key = e.get("distill_model") or "(auto)"
        m = per_model.setdefault(key, {"model": key, "batches": 0, "receipts": 0, "_secs": 0.0})
        m["batches"] += 1
        m["receipts"] += int(e.get("count") or 0)
        m["_secs"] += float(e.get("total_seconds") or 0)
    models = []
    for m in per_model.values():
        m["avg_seconds"] = round(m["_secs"] / m["receipts"], 2) if m["receipts"] else 0.0
        m.pop("_secs", None)
        models.append(m)
    fastest_model = min(models, key=lambda m: m["avg_seconds"])["model"] if len(models) > 1 else ""

    # Per-step rollup across every batch — total time spent in each pipeline stage
    # (OCR, distillation, classify, audit, image-prep …), so the slowest stage is
    # obvious. Newest-first order of first appearance, then sorted by total time.
    step_agg: dict[str, dict] = {}
    for e in rows:
        for s in (e.get("steps") or []):
            key = s.get("step") or s.get("label") or "?"
            a = step_agg.setdefault(key, {
                "step": key, "label": s.get("label") or key,
                "count": 0, "failures": 0, "total_seconds": 0.0,
            })
            a["count"] += int(s.get("count") or 0)
            a["failures"] += int(s.get("failures") or 0)
            a["total_seconds"] += float(s.get("total_seconds") or 0)
    step_totals = []
    for a in step_agg.values():
        a["total_seconds"] = round(a["total_seconds"], 2)
        a["avg_seconds"] = round(a["total_seconds"] / a["count"], 2) if a["count"] else 0.0
        step_totals.append(a)
    step_totals.sort(key=lambda a: a["total_seconds"], reverse=True)

    return {
        "batches":          len(rows),
        "receipts":         total_receipts,
        "total_seconds":    total_seconds,
        "avg_per_receipt":  avg_per_receipt,
        "throughput_per_min": throughput,
        "fastest_batch_avg": fastest,
        "slowest_batch_avg": slowest,
        "recent_avg":       recent_avg,
        "trend":            trend,
        "models":           models,
        "fastest_model":    fastest_model,
        "step_totals":      step_totals,
    }


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
    """Poll INTAKE_FOLDER every 5 seconds and auto-queue new image/PDF files.

    In multi-user mode this watches the shared/default intake folder (files dropped
    there process as the default user); regular users add receipts through the web
    UI, which scopes them to the uploader. Per-user watched folders are a follow-up
    (see MULTIUSER.md)."""
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
                                    _work_queue.append(_tag_item(item))
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
                            _work_queue.append(_tag_item(item))
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
                                    _work_queue.append(_tag_item(item))
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

        # Scan each user's board for stalled items (single-user: just the default).
        for _ws in _watch_workspaces():
            _stok = multiuser.bind(_ws)
            try:
                _stall_scan_once()
            except Exception as exc:
                _broadcast({"type": "log", "message": f"[stall] scan error: {exc}"})
            finally:
                multiuser.reset(_stok)


def _stall_scan_once() -> None:
    """Detect + re-queue items stuck in ocr/distilling for the current workspace."""
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

        item = _tag_item(dict(cached))
        with _work_lock:
            _work_queue.appendleft(_tag_item(item))
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


_last_schedule_snapshot: list[dict] = []


def _schedule_results_snapshot() -> tuple[list[dict], str]:
    global _last_schedule_snapshot
    with _results_lock:
        results = copy.deepcopy(_results)
        employee = _last_context.get("employee", "Employee")
    # Drop receipts already included in a previously sent report (sent-ledger dedup).
    results = [r for r in results
               if not r.get("_already_sent") or r.get("_force_included")]
    _detect_duplicates(results)
    _last_schedule_snapshot = results
    return results, employee


def _on_schedule_result(report: dict) -> None:
    cfg = _load_config()
    cfg.setdefault("schedule", {})["last_run"] = report
    _save_config(cfg)
    # Record what was just exported so it isn't re-sent in the next scheduled run.
    if report.get("ok") and "email" in (report.get("delivered") or []):
        try:
            _record_sent(_last_schedule_snapshot, report.get("filename", ""))
            _persist_state()
        except Exception as exc:  # never let bookkeeping break the scheduler
            print(f"[schedule] sent-ledger record failed: {exc}")
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
    users.ensure_seed()          # seed an admin from env on a fresh multi-user box
    _restore_all()               # restore the default + every per-user workspace
    _apply_processing_config()   # restore UI-saved auto-crop / compress / local-OCR settings
    _apply_audit_config()        # restore UI-saved spending/date warning thresholds
    _first_run_provider_default()  # zero-click OpenRouter free router if a key is set & nothing chosen
    _apply_llm_server_config()   # restore LLM server URL before any model query
    _apply_model_config()        # restore saved single-model choice before auto-select
    # Session-start housekeeping: sweep away empty, non-active orphaned folders
    # (collapsed dated/job subfolders, leftover _pdf_*/_upload_* staging) left
    # behind by a previous run before the new session starts handing out work.
    try:
        removed = _run_empty_dir_cleanup()
        if removed:
            print(f"[startup] cleaned {len(removed)} empty orphaned folder(s)")
    except Exception as exc:
        print(f"[startup] empty-folder cleanup skipped: {exc}")
    threading.Thread(target=_startup_models,    daemon=True).start()
    _ensure_worker_alive()       # start the self-healing worker thread
    threading.Thread(target=_run_watcher,       daemon=True).start()
    threading.Thread(target=_run_stall_checker, daemon=True).start()
    threading.Thread(target=_run_email_poller,  daemon=True).start()  # IMAP receipt intake
    threading.Thread(target=_run_gdrive_poller, daemon=True).start()  # Google Drive intake
    threading.Thread(target=_run_onedrive_poller, daemon=True).start()  # OneDrive intake
    sched_task = asyncio.create_task(scheduler.run_scheduler(
        _get_schedule_config, _schedule_results_snapshot,
        _on_schedule_result, _schedule_wakeup,
    ))
    yield
    sched_task.cancel()
    _worker_cancel.set()
    _persist_all()               # flush every workspace's state on shutdown


# ── Per-user identity binding ───────────────────────────────────────────────────
# A single global dependency binds the request's resolved user (set by the auth
# middleware on request.state) as the current Workspace for the whole handler and
# its deep calls — so the context proxies (_results / IMAGES_FOLDER / …) scope to
# the right user. Single-user mode resolves everyone to "default" (unchanged).

async def _bind_ws(request: Request):
    uid = getattr(request.state, "user_id", multiuser.DEFAULT_USER)
    token = multiuser.bind_user(uid)
    try:
        yield
    finally:
        multiuser.reset(token)


app = FastAPI(title="Receipt Processor", lifespan=lifespan,
              dependencies=[Depends(_bind_ws)])


# ── Optional shared-secret auth ─────────────────────────────────────────────────
# The app is a local-network tool with no login. When APP_AUTH_TOKEN is set,
# every request must present it (via the X-Auth-Token header, an auth_token
# cookie, or a ?token= query param — opening the page once with ?token= drops the
# cookie so the SPA's fetch/SSE calls authenticate automatically). When the env
# var is unset the gate is a no-op, preserving the open localhost behaviour.
# The token is read per-request so it can be configured without a code change.
_AUTH_EXEMPT_PATHS = {"/", "/manifest.json", "/icon.svg"}
# Paths reachable WITHOUT a logged-in session in multi-user mode: the shell page +
# icons (the SPA renders its own login overlay), and the auth endpoints themselves.
# Everything else 401s until the user signs in, so no data endpoint is exposed.
_MU_EXEMPT_PATHS = _AUTH_EXEMPT_PATHS | {
    "/login", "/logout", "/me", "/multiuser/status", "/setup",
}


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    path = request.url.path
    set_token_cookie: str | None = None

    # ── Coarse instance-wide shared-secret gate (APP_AUTH_TOKEN), unchanged. ──
    token = os.getenv("APP_AUTH_TOKEN", "")
    if token:
        import secrets as _secrets
        if path in _AUTH_EXEMPT_PATHS:
            # Let the shell page + icons load so the token can be supplied via
            # ?token=; drop it as a cookie so the SPA's later cookie-only requests
            # (images, SSE) authenticate. SameSite=Lax; Secure only over HTTPS.
            supplied = request.query_params.get("token", "")
            if supplied and _secrets.compare_digest(supplied, token):
                set_token_cookie = token
        else:
            supplied = (
                request.headers.get("X-Auth-Token", "")
                or request.cookies.get("auth_token", "")
                or request.query_params.get("token", "")
            )
            if not (supplied and _secrets.compare_digest(supplied, token)):
                return JSONResponse({"error": "unauthorized"}, status_code=401)

    # ── Per-user identity (multi-user mode). Single-user → everyone is "default". ──
    user_id = multiuser.DEFAULT_USER
    if multiuser.ENABLED:
        sess_uid = users.verify_session(request.cookies.get(users.SESSION_COOKIE, ""))
        if sess_uid:
            user_id = sess_uid
        elif path not in _MU_EXEMPT_PATHS:
            return JSONResponse({"error": "login_required"}, status_code=401)
    request.state.user_id = user_id

    resp = await call_next(request)
    if set_token_cookie is not None:
        https = (request.url.scheme == "https"
                 or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
        resp.set_cookie("auth_token", set_token_cookie, max_age=31_536_000, path="/",
                        httponly=True, samesite="lax", secure=https)
    return resp


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


# ── Multi-user auth / account routes ────────────────────────────────────────────
# All no-ops / "single admin user" in single-user mode (MULTIUSER_ENABLED off), so
# the SPA can call them unconditionally.

class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


def _set_session_cookie(resp, request: Request, token: str) -> None:
    https = (request.url.scheme == "https"
             or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
    resp.set_cookie(users.SESSION_COOKIE, token, max_age=users.SESSION_TTL_SECS,
                    path="/", httponly=True, samesite="lax", secure=https)


@app.get("/multiuser/status")
async def multiuser_status():
    """Whether multi-user mode is on, and whether the first admin still needs
    creating — drives the SPA's login overlay."""
    return {
        "enabled": multiuser.ENABLED,
        "needs_setup": bool(multiuser.ENABLED and users.user_count() == 0),
    }


@app.get("/me")
async def whoami(request: Request):
    """The current identity. Single-user mode reports a synthetic admin so the SPA
    behaves identically to today."""
    if not multiuser.ENABLED:
        return {"multiuser": False, "authenticated": True,
                "user_id": multiuser.DEFAULT_USER, "display": "", "is_admin": True}
    sess = users.verify_session(request.cookies.get(users.SESSION_COOKIE, ""))
    if not sess:
        return {"multiuser": True, "authenticated": False,
                "needs_setup": users.user_count() == 0}
    rec = users.get_user(sess) or {}
    return {"multiuser": True, "authenticated": True, "user_id": sess,
            "display": rec.get("display") or sess, "is_admin": bool(rec.get("is_admin"))}


@app.post("/login")
async def login(req: LoginRequest, request: Request):
    if not multiuser.ENABLED:
        return JSONResponse({"error": "multiuser_disabled"}, status_code=400)
    uid = (req.username or "").strip().lower()
    if not users.authenticate(uid, req.password):
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)
    resp = JSONResponse({"ok": True, "user_id": uid, "is_admin": users.is_admin(uid)})
    _set_session_cookie(resp, request, users.make_session(uid))
    return resp


@app.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(users.SESSION_COOKIE, path="/")
    return resp


@app.post("/setup")
async def setup_first_admin(req: LoginRequest, request: Request):
    """Create the very first admin on a fresh multi-user instance, then log them
    in. Only works while no users exist (otherwise it'd be an open account-creation
    hole)."""
    if not multiuser.ENABLED:
        return JSONResponse({"error": "multiuser_disabled"}, status_code=400)
    if users.user_count() > 0:
        return JSONResponse({"error": "already_setup"}, status_code=400)
    try:
        users.create_user(req.username, req.password, is_admin=True)
    except users.UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    uid = (req.username or "").strip().lower()
    resp = JSONResponse({"ok": True, "user_id": uid, "is_admin": True})
    _set_session_cookie(resp, request, users.make_session(uid))
    return resp


def _admin_or_403(request: Request):
    """Return the caller's user_id if they're an admin in multi-user mode, else a
    JSONResponse(403) the caller should return."""
    if not multiuser.ENABLED:
        return JSONResponse({"error": "multiuser_disabled"}, status_code=400)
    uid = getattr(request.state, "user_id", "")
    if not users.is_admin(uid):
        return JSONResponse({"error": "admin_only"}, status_code=403)
    return uid


class NewUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    display: str = ""


class PasswordRequest(BaseModel):
    password: str


class AdminFlagRequest(BaseModel):
    is_admin: bool


@app.get("/users")
async def users_list(request: Request):
    guard = _admin_or_403(request)
    if isinstance(guard, JSONResponse):
        return guard
    return {"users": users.list_users()}


@app.post("/users")
async def users_create(req: NewUserRequest, request: Request):
    guard = _admin_or_403(request)
    if isinstance(guard, JSONResponse):
        return guard
    try:
        rec = users.create_user(req.username, req.password,
                                is_admin=req.is_admin, display=req.display)
    except users.UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "user": rec}


@app.delete("/users/{uid}")
async def users_delete(uid: str, request: Request):
    guard = _admin_or_403(request)
    if isinstance(guard, JSONResponse):
        return guard
    if uid == guard:
        return JSONResponse({"error": "cannot_delete_self"}, status_code=400)
    try:
        users.delete_user(uid)
    except users.UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True}


@app.post("/users/{uid}/password")
async def users_set_password(uid: str, req: PasswordRequest, request: Request):
    # Admins can reset anyone's password; a user may change their own.
    if not multiuser.ENABLED:
        return JSONResponse({"error": "multiuser_disabled"}, status_code=400)
    caller = getattr(request.state, "user_id", "")
    if caller != uid and not users.is_admin(caller):
        return JSONResponse({"error": "admin_only"}, status_code=403)
    try:
        users.set_password(uid, req.password)
    except users.UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True}


@app.post("/users/{uid}/admin")
async def users_set_admin(uid: str, req: AdminFlagRequest, request: Request):
    guard = _admin_or_403(request)
    if isinstance(guard, JSONResponse):
        return guard
    try:
        users.set_admin(uid, req.is_admin)
    except users.UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True}


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
                        _work_queue.append(_tag_item(item))
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
                _work_queue.append(_tag_item(item))
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
                _work_queue.append(_tag_item(item))
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
                        _work_queue.append(_tag_item(item))
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
    """Per-user SSE stream — each client receives only its own user's events
    (single-user mode: one user, so effectively a global stream as before)."""
    sub = _add_subscriber()
    q = sub.q

    # Send full state snapshot on connect (the caller's own board/queue/results)
    with _kanban_lock:
        kanban_snapshot = {fn: dict(v) for fn, v in _kanban.items()}
    n_pending = _pending_count(sub.user_id)
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
            last_beat = time.monotonic()
            while True:
                try:
                    msg = q.get_nowait()
                except Empty:
                    # Idle: emit a keep-alive comment only every heartbeat
                    # interval so proxies don't drop the connection, then yield
                    # control briefly. A short poll keeps real events snappy —
                    # they're delivered within SSE_POLL_SECS, not up to a whole
                    # heartbeat later.
                    now = time.monotonic()
                    if now - last_beat >= SSE_HEARTBEAT_SECS:
                        yield ": heartbeat\n\n"
                        last_beat = now
                    await asyncio.sleep(SSE_POLL_SECS)
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                last_beat = time.monotonic()
        finally:
            _remove_subscriber(sub)

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
    notes:           str = ""


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
        "notes":            body.notes.strip()[:500],
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

    # Exclude receipts already included in a previously sent report (the sent-ledger
    # dedup) unless the user forced one back in via "Include anyway".
    results_copy = [r for r in results_copy
                    if not r.get("_already_sent") or r.get("_force_included")]

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
    #
    # NB: results_copy was already snapshotted from _results under _results_lock
    # above. We deliberately do NOT re-hold the lock across this whole PIL loop —
    # doing so froze the background worker (which appends results under the same
    # lock) and every results-reading endpoint (/queue/status, /stats, /events,
    # _persist_state) for the entire compression window. Each record's path update
    # is an atomic single-field string swap, so a concurrent reader always sees a
    # consistent old-or-new path.
    def _compress_live():
        compress_result_images(
            results_copy,
            log=lambda m: _broadcast({"type": "log", "message": m}),
        )
    await asyncio.get_running_loop().run_in_executor(None, _compress_live)

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

    per_diem = _per_diem_config()

    def _build():
        return generate_spreadsheet(
            results=results_copy,
            output_dir=OUT_FOLDER,
            employee_name=employee,
            per_diem=per_diem,
        )

    try:
        output_path = await asyncio.get_event_loop().run_in_executor(None, _build)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if not output_path or not Path(output_path).exists():
        return HTMLResponse("Spreadsheet generation failed", status_code=500)

    # Keep the just-built workbook in memory so "Send Report Now" can email this
    # exact file later (even after the board is cleared) instead of rebuilding.
    global _last_report_path, _last_report_count
    _last_report_path = str(output_path)
    _last_report_count = len(results_copy)

    filename = Path(output_path).name

    # Record everything that went into this workbook in the sent-ledger so the same
    # receipts aren't re-included in a future report. Idempotent (de-duped inside).
    _record_sent(results_copy, filename)
    _persist_state()

    # Mirror the report into the user's Drive Output/<date>/ folder (workbook + the
    # processed receipt images) when Drive output upload is enabled. Best-effort, off
    # the request path so a slow/failed upload never blocks the download.
    _img_paths = [r.get("_image_path") for r in results_copy if r.get("_image_path")]
    _date_str = time.strftime("%Y-%m-%d")
    threading.Thread(
        target=lambda: _gdrive_upload_report(output_path, _img_paths, _date_str),
        daemon=True).start()

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

    # Calendar span of the dated receipts (inclusive), NOT the count of distinct
    # days that happen to have receipts. The full Y/M/D dates are used so a range
    # spanning multiple years reports the true number of days, e.g. receipts on
    # 173 distinct days across two years span ~730 days, not 173.
    if timeline:
        first_day = date.fromisoformat(timeline[0]["date"])
        last_day = date.fromisoformat(timeline[-1]["date"])
        span_days = (last_day - first_day).days + 1
    else:
        span_days = 0

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
        "timeline_span_days": span_days,
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


def _csv_safe(value: str) -> str:
    """Neutralise CSV/spreadsheet formula injection for one field.

    Receipt fields (vendor, summary, description, job…) come from OCR/LLM
    extraction. A value beginning with ``= + - @`` (or a leading tab/CR) is
    interpreted as a *formula* by Excel/Sheets when the exported CSV is opened —
    the classic CSV-injection vector. Prefix such values with a single quote so
    they are treated as text (OWASP-recommended mitigation). Untrusted leads are
    rare, so normal values are untouched.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _results_to_csv(results: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in _CSV_COLUMNS])
    for r in sorted(results, key=sort_key_for_receipt):
        writer.writerow([_csv_safe(fn(r)) for _, fn in _CSV_COLUMNS])
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
                    "job_number", "ai_summary", "expense_description", "notes"}
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
    elif field == "notes":
        value = str(value)[:500]

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


class ForceIncludeRequest(BaseModel):
    filename: str
    include: bool = True


@app.post("/results/force-include")
async def force_include(body: ForceIncludeRequest):
    """Override the sent-ledger skip for a receipt flagged "already reported".

    With include=True the receipt is forced back into the next report; with
    include=False it is re-excluded. Clears the "already reported" headline flag
    so the card no longer reads as a warning once the user has decided.
    """
    with _results_lock:
        target = None
        for r in _results:
            if r.get("_file") == body.filename or r.get("_new_filename") == body.filename:
                target = r
                break
        if target is None:
            return JSONResponse({"ok": False, "error": "Receipt not found"}, status_code=404)
        target["_force_included"] = bool(body.include)
        if body.include and (target.get("_flag") or "").lower().startswith("already reported"):
            target["_flag"] = ""
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


class FinishBatchRequest(BaseModel):
    mode: str = "archive"   # "archive" → move kept files aside; "delete" → remove


@app.post("/results/finish")
async def finish_batch(body: FinishBatchRequest):
    """Wrap up a batch after its report was downloaded.

    The workbook already embeds the receipt images, so the originals in the
    working folders are now temporary. This either deletes them
    (``mode="delete"``) or moves them into ARCHIVE_FOLDER (``mode="archive"``)
    so they're preserved but no longer flagged as orphaned. Either way the
    completed results are cleared off the board, finishing the batch.
    """
    mode = (body.mode or "archive").lower()
    if mode not in ("archive", "delete"):
        return JSONResponse({"error": "mode must be 'archive' or 'delete'"}, status_code=400)

    # Snapshot the image files referenced by the current completed results.
    with _results_lock:
        paths: list[Path] = []
        for r in _results:
            ip = r.get("_image_path")
            cand = Path(ip) if ip else None
            if not cand or not cand.exists():
                # Fall back to locating by name under the completed-receipts folder.
                name = r.get("_new_filename") or r.get("_file") or ""
                for base in (IMAGES_FOLDER, PROCESSING_FOLDER):
                    hit = next(base.rglob(name), None) if name else None
                    if hit and hit.is_file():
                        cand = hit
                        break
            if cand and cand.exists() and cand.is_file():
                paths.append(cand)

    moved = 0
    removed = 0
    errors: list[str] = []
    dest_dir = ARCHIVE_FOLDER / f"Archived_{date.today().isoformat()}"
    for p in paths:
        try:
            if mode == "delete":
                p.unlink()
                removed += 1
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / p.name
                if target.exists():
                    target = dest_dir / f"{p.stem}_{uuid4().hex[:6]}{p.suffix}"
                shutil.move(str(p), str(target))
                moved += 1
        except OSError as exc:
            errors.append(f"{p.name}: {exc}")

    # Clear the completed results + done/failed board cards (the report is done).
    with _results_lock:
        _results.clear()
    with _kanban_lock:
        for fn in [fn for fn, v in _kanban.items() if v["status"] in ("done", "failed")]:
            del _kanban[fn]
    _persist_state()
    _broadcast({"type": "results_cleared"})
    return JSONResponse({
        "ok": True, "mode": mode, "archived": moved, "deleted": removed,
        "archive_dir": str(dest_dir) if mode == "archive" and moved else "",
        "errors": errors,
    })


# ── Retry endpoint ─────────────────────────────────────────────────────────────

class RetryRequest(BaseModel):
    filename: str
    # A manual retry from the review screen turns the optional LLM-OCR vision
    # cross-reference ON for this one receipt (default), to rescue fringe cases the
    # built-in OCR mangles — even when the batch-level toggle is off. Honoured only
    # when a model is selected and image-sending is allowed.
    force_llm_ocr: bool = True


@app.post("/retry-receipt")
async def retry_receipt(body: RetryRequest):
    """Re-queue a failed receipt for reprocessing (sends it back to the front of the queue)."""
    filename = body.filename

    # Reject path-traversal in the supplied name before it is used to build
    # filesystem candidates below (``PROCESSING_FOLDER / "../.."`` does NOT collapse
    # the ``..`` — ``.exists()`` would stat the traversed path and the worker would
    # then ``shutil.move`` an arbitrary file into the pipeline). Mirror the guard
    # used by ``/receipt-image``.
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse(
            {"ok": False, "error": "invalid filename"}, status_code=400)

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
        # Force the LLM-OCR vision cross-reference for this retry (see RetryRequest).
        "force_llm_ocr": bool(body.force_llm_ocr),
    }
    _cache_item(item)
    with _work_lock:
        _work_queue.appendleft(_tag_item(item))

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
            _work_queue.appendleft(_tag_item(item))
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
            _work_queue.append(_tag_item(dict(cached)))
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
    """List models for the local model selector UI.

    For the OpenRouter provider the catalogue is hundreds of entries and lives in
    its own selector (``/models/openrouter``), so here we just echo the resolved
    active model rather than flooding the local dropdown.
    """
    provider = (_load_config().get("provider") or "local").strip()
    base = {
        "active_distill": _pr._active_distill_model,
        "active_ocr":     _pr._active_ocr_model,
        "llm_ocr":        _pr._llm_ocr_enabled,
        "thinking":       _pr._thinking_enabled,
        "provider":       provider,
    }
    if provider == "openrouter":
        active = _pr._active_distill_model
        return JSONResponse({**base, "models": [active] if active else [], "ok": True})

    def _fetch():
        return list_available_models()

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({**base, "models": models, "ok": True})
    except Exception as exc:
        return JSONResponse({**base, "models": [], "ok": False, "error": str(exc)})


class ModelSwapRequest(BaseModel):
    model: str


@app.post("/models/distill")
async def swap_distill_model(body: ModelSwapRequest):
    """Set the single active AI model (used for distillation and, optionally, OCR).

    OCR and distillation are consolidated onto one model, so this also re-points
    the OCR alias when the LLM-OCR cross-reference is enabled. The choice is
    persisted and LM Studio loads the model on first use.
    """
    target = _pr.set_active_model(body.model)
    _persist_model_config()
    return JSONResponse({"ok": True, "active_distill": target,
                         "active_ocr": _pr._active_ocr_model})


class LlmOcrRequest(BaseModel):
    enabled: bool = False


@app.post("/models/ocr")
async def toggle_llm_ocr(body: LlmOcrRequest):
    """Toggle whether the single active model also transcribes the receipt (LLM OCR).

    There is no separate OCR model any more; when enabled, the active model's
    transcription is cross-referenced against the built-in RapidOCR reading.
    """
    enabled = _pr.set_llm_ocr(body.enabled)
    _persist_model_config()
    return JSONResponse({"ok": True, "llm_ocr": enabled,
                         "active_ocr": _pr._active_ocr_model})


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
        client = _pr.make_client()
        response = client.models.list()
        return [m.id for m in response.data]

    try:
        models = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({"loaded": models, "ok": True,
                             "base_url": _pr.LMSTUDIO_BASE_URL})
    except Exception as exc:
        return JSONResponse({"loaded": [], "ok": False, "error": str(exc),
                             "base_url": _pr.LMSTUDIO_BASE_URL})


# ── LLM model config endpoint (Feature 2) ─────────────────────────────────────

@app.post("/settings/llm-model")
async def set_llm_model_config(request: Request):
    """Save LLM model configuration (applied immediately and on next startup)."""
    body = await request.json()
    model_id    = str(body.get("model_id",    "")).strip()
    server_type = str(body.get("server_type", "other")).strip()  # "docker" or "other"
    base_url    = str(body.get("base_url",    "")).strip()

    cfg = _load_config()
    cfg["llm_model_config"] = {
        "model_id":    model_id,
        "server_type": server_type,
        "base_url":    base_url,
    }
    _save_config(cfg)
    # Apply the model ID immediately for the current session.
    # URL/server-type changes (docker vs custom) are intentionally deferred to the
    # next startup so the Configure Model dialog cannot silently overwrite the active
    # LMSTUDIO_BASE_URL — which would break a working LM Studio connection without
    # any visible feedback.  Use POST /settings/llm-server for immediate URL changes.
    if model_id:
        _pr.set_active_model(model_id)
    return JSONResponse({"ok": True,
                         "message": "Settings saved — model will load on next startup."})


# ── LLM server settings endpoints (Feature 12) ────────────────────────────────

@app.get("/settings/llm-server")
async def get_llm_server_settings():
    """Return the current LOCAL LLM server configuration.

    ``base_url`` is the CONFIGURED endpoint (what the user saved) so the UI shows
    the user's own choice — not whatever session-only fallback the startup probe
    may have adopted. ``effective_base_url`` is the in-memory endpoint actually in
    use, returned separately for transparency (they differ only when the saved
    server was unreachable at startup and a temporary fallback was adopted).
    """
    cfg = _load_config()
    llm_srv = cfg.get("llm_server") or {}
    server_type = llm_srv.get("server_type", "custom")
    effective = getattr(_pr, "LMSTUDIO_BASE_URL", "")
    # Infer docker mode when the URL was injected via env pointing at the bundled server
    # (no saved server_type key in that case, so we must check the effective URL too).
    if server_type != "docker" and _is_docker_bundled_url(effective):
        server_type = "docker"
    if server_type == "docker":
        configured = _docker_llm_url()
    elif llm_srv.get("base_url"):
        configured = _normalize_llm_url(str(llm_srv["base_url"]))
    else:
        configured = "http://127.0.0.1:1234/v1"
    return JSONResponse({
        "server_type":        server_type,
        "base_url":           configured,
        "effective_base_url": effective or configured,
        "provider":           (cfg.get("provider") or "local").strip(),
    })


@app.post("/settings/llm-server")
async def set_llm_server(request: Request):
    """Update the LOCAL LLM server URL used for model queries and inference.

    Choosing a local URL also switches the active provider back to ``local`` (off
    any cloud provider). The saved value is what the user typed; reachability is
    probed and returned so the UI can warn WITHOUT silently rewriting the choice.
    """
    body = await request.json()
    server_type = str(body.get("server_type", "custom")).strip()
    base_url    = str(body.get("base_url", "")).strip()

    if server_type == "docker":
        effective_url = _docker_llm_url()
    elif base_url:
        effective_url = _normalize_llm_url(base_url)
    else:
        effective_url = "http://127.0.0.1:1234/v1"

    _reset_local_llm_runtime()
    _pr.LMSTUDIO_BASE_URL = effective_url

    cfg = _load_config()
    cfg["provider"]   = "local"
    cfg["llm_server"] = {"server_type": server_type, "base_url": base_url}
    _save_config(cfg)
    reachable = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _probe_llm_url(effective_url)[0])
    return JSONResponse({"ok": True, "base_url": effective_url,
                         "reachable": reachable})


# ── Unified LLM provider settings (local server vs. OpenRouter cloud) ──────────

@app.get("/settings/llm-provider")
async def get_llm_provider():
    """Return the full LLM-provider configuration for the Settings UI."""
    cfg      = _load_config()
    provider = (cfg.get("provider") or "local").strip()
    orc      = {**_openrouter_default_cfg(), **(cfg.get("openrouter") or {})}
    llm_srv  = cfg.get("llm_server") or {}
    # Has the user made any explicit LLM choice yet? On a truly fresh config the UI
    # defaults the mode selector to OpenRouter (the zero-setup free option) rather
    # than On-host, so it can tell "never configured" apart from "chose local".
    configured = bool(cfg.get("provider") or cfg.get("llm_server")
                      or cfg.get("llm_model_config") or cfg.get("openrouter"))
    effective_url = getattr(_pr, "LMSTUDIO_BASE_URL", "")
    local_server_type = llm_srv.get("server_type", "custom")
    # Infer docker mode when URL was injected via env pointing at the bundled server.
    if local_server_type != "docker" and _is_docker_bundled_url(effective_url):
        local_server_type = "docker"
    return JSONResponse({
        "provider":   provider,
        "configured": configured,
        "local": {
            "server_type": local_server_type,
            "base_url":    llm_srv.get("base_url", "") or "",
        },
        "openrouter": {
            "model":          orc.get("model", "auto"),
            "resolved_model": orc.get("resolved_model", ""),
            "send_image":     bool(orc.get("send_image", True)),
            "free_only":      bool(orc.get("free_only", True)),
            "has_key":        bool(_openrouter_api_key()),
        },
        "effective_base_url": getattr(_pr, "LMSTUDIO_BASE_URL", ""),
        "active_model":       _pr._active_distill_model,
    })


@app.post("/settings/llm-provider")
async def set_llm_provider(request: Request):
    """Switch the active LLM provider and persist its settings.

    ``provider`` is ``"local"`` or ``"openrouter"``. For OpenRouter, an optional
    ``api_key`` is stored as a secret (blank keeps the existing key), and a
    ``model`` of ``"auto"`` is resolved to the best free vision-capable model.
    The ``send_image`` flag chooses between sending the receipt image (better
    accuracy) and OCR-text-only (the image never leaves the machine).
    """
    body     = await request.json()
    provider = (str(body.get("provider", "local")).strip() or "local")
    cfg      = _load_config()
    cfg["provider"] = provider

    # OpenRouter API key is a secret; a blank/absent value keeps the saved one.
    if "api_key" in body:
        key = str(body.get("api_key") or "").strip()
        if key:
            app_secrets.save_secret("openrouter_api_key", key)

    if provider != "openrouter":
        _save_config(cfg)
        _apply_local_llm_config(cfg)
        return JSONResponse({"ok": True, "provider": "local",
                             "base_url": _pr.LMSTUDIO_BASE_URL})

    orc = {**_openrouter_default_cfg(), **(cfg.get("openrouter") or {})}
    if "model" in body:
        orc["model"] = (str(body.get("model") or "").strip() or OPENROUTER_FREE_ROUTER)
    if "send_image" in body:
        orc["send_image"] = bool(body.get("send_image"))
    if "free_only" in body:
        orc["free_only"] = bool(body.get("free_only"))

    loop = asyncio.get_event_loop()
    if orc["model"] == "auto":
        orc["resolved_model"] = await loop.run_in_executor(None, _openrouter_autopick)
    else:
        orc["resolved_model"] = orc["model"]   # free-router slug or explicit id
    # Pin a quick-first free VISION fallback list so the router never lands on a
    # text-only model for an image request (best-effort; [] when offline).
    orc["models_fallback"] = await loop.run_in_executor(None, _openrouter_vision_fallback)

    cfg["openrouter"] = orc
    _save_config(cfg)
    _apply_openrouter_config(cfg)

    has_key = bool(_openrouter_api_key())
    warning = None
    if not has_key:
        warning = "No OpenRouter API key set — add your key to use the cloud provider."
    elif orc["model"] == "auto" and not orc["resolved_model"]:
        warning = "Could not reach OpenRouter to auto-select a model — check your key/connection."
    return JSONResponse({
        "ok":             has_key,
        "provider":       "openrouter",
        "model":          orc["model"],
        "resolved_model": orc["resolved_model"],
        "send_image":     orc["send_image"],
        "free_only":      orc["free_only"],
        "fallback_count": len(orc.get("models_fallback") or []),
        "has_key":        has_key,
        "warning":        warning,
        "base_url":       _pr.LMSTUDIO_BASE_URL,
    })


@app.get("/models/openrouter")
async def get_openrouter_models():
    """List free, image-capable OpenRouter models (best first) for the selector."""
    loop   = asyncio.get_event_loop()
    models = await loop.run_in_executor(None, _openrouter_free_vision_models)
    out = [{
        "id":             m.get("id"),
        "name":           m.get("name") or m.get("id"),
        "context_length": m.get("context_length"),
    } for m in models if m.get("id")]
    return JSONResponse({
        "ok":       True,
        "has_key":  bool(_openrouter_api_key()),
        "count":    len(out),
        "models":   out,
    })


# ── LLM server control endpoints (Feature 11) ─────────────────────────────────

@app.get("/llm-server/status")
async def llm_server_status():
    """Check if the configured LLM server is reachable."""
    import httpx
    base_url = getattr(_pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    reachable    = False
    model_loaded = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base_url}/models")
            if r.status_code == 200:
                reachable    = True
                data         = r.json()
                model_loaded = len(data.get("data", [])) > 0
    except Exception:
        pass
    return JSONResponse({
        "reachable":    reachable,
        "model_loaded": model_loaded,
        "is_docker":    _in_docker(),
        "base_url":     base_url,
    })


@app.get("/llm-server/availability")
async def llm_server_availability():
    """Probe every model mode at once so the UI can show which are live.

    Returns reachability (+ loaded-model count) for the On-host and Docker-bundled
    endpoints and whether an OpenRouter key is set, plus the currently-active mode
    and model. Drives the per-mode availability indicators and the header engine
    chip, and is refreshed automatically while the AI Model settings are open.
    """
    cfg      = _load_config()
    provider = (cfg.get("provider") or "local").strip()
    llm_srv  = cfg.get("llm_server") or {}
    srv_type = llm_srv.get("server_type", "custom")
    effective_url = getattr(_pr, "LMSTUDIO_BASE_URL", "")
    # On a fresh config (no explicit choice) the default mode is OpenRouter, so the
    # header chip and the mode selector agree before the user picks anything.
    configured = bool(cfg.get("provider") or cfg.get("llm_server")
                      or cfg.get("llm_model_config") or cfg.get("openrouter"))
    active_mode = ("openrouter" if (provider == "openrouter" or not configured)
                   else ("docker" if (srv_type == "docker"
                                      or _is_docker_bundled_url(effective_url))
                         else "host"))

    # On-host candidate = the saved custom URL (if any) else the LM Studio default.
    host_url = ("http://127.0.0.1:1234/v1"
                if not (srv_type == "custom" and llm_srv.get("base_url"))
                else _normalize_llm_url(str(llm_srv["base_url"])))
    docker_url = _docker_llm_url()

    if active_mode == "openrouter":
        orc = {**_openrouter_default_cfg(), **(cfg.get("openrouter") or {})}
        active_model = (orc.get("resolved_model")
                        or (orc.get("model") if orc.get("model") != "auto" else ""))
    else:
        active_model = _pr._active_distill_model

    loop = asyncio.get_event_loop()
    (host_ok, host_n), (docker_ok, docker_n) = await asyncio.gather(
        loop.run_in_executor(None, _probe_llm_url, host_url, 1.0),
        loop.run_in_executor(None, _probe_llm_url, docker_url, 1.0),
    )
    return JSONResponse({
        "active_mode":  active_mode,
        "active_model": active_model,
        "host":         {"reachable": host_ok,   "models": host_n,   "base_url": host_url},
        "docker":       {"reachable": docker_ok, "models": docker_n, "base_url": docker_url},
        "openrouter":   {"has_key":   bool(_openrouter_api_key())},
    })


@app.get("/settings/openrouter/usage")
async def openrouter_usage(force: bool = False):
    """Live daily free-request tally + the queried daily cap.

    The count is tracked locally — every OpenRouter request we sent today (UTC),
    failures included (they count toward the quota). The cap (50 vs 1000/day) is
    queried from OpenRouter's /credits endpoint (cached): 1000 once ≥ $10 of credit
    has been purchased, else 50. The per-minute cap (~20) is fixed, shown for context.
    Pass ?force=1 to bypass the cap cache.
    """
    usage   = _pr.get_openrouter_usage()
    has_key = bool(_openrouter_api_key())
    if not has_key:
        return JSONResponse({"has_key": False, "date": usage["date"],
                             "count": usage["count"], "cap": None,
                             "per_min": _OPENROUTER_PER_MIN})
    loop = asyncio.get_event_loop()
    cap_info = await loop.run_in_executor(None, _openrouter_cap_info, bool(force))
    cap = cap_info.get("cap")
    remaining = max(cap - usage["count"], 0) if isinstance(cap, int) else None
    return JSONResponse({
        "has_key":       True,
        "date":          usage["date"],
        "count":         usage["count"],
        "cap":           cap,
        "remaining":     remaining,
        "per_min":       cap_info.get("per_min", _OPENROUTER_PER_MIN),
        "total_credits": cap_info.get("total_credits"),
        "total_usage":   cap_info.get("total_usage"),
        "credits_known": cap_info.get("credits_known", False),
    })


@app.post("/settings/openrouter/test")
async def openrouter_test_connection():
    """Run a real OpenRouter round-trip (full send → receive) and report the log.

    Sends a tiny prompt through the SAME client/headers/routing body the pipeline
    uses, so a green result proves the key, model, attribution headers and routing
    all work end-to-end. Surfaces the exact failure (auth / no model / network)
    when they don't — the "OpenRouter shows no calls" cases are no longer silent.
    """
    logs: list[str] = []
    cfg      = _load_config()
    provider = (cfg.get("provider") or "local").strip()
    has_key  = bool(_openrouter_api_key())
    orc      = {**_openrouter_default_cfg(), **(cfg.get("openrouter") or {})}
    model    = (orc.get("resolved_model") or orc.get("model")
                or OPENROUTER_FREE_ROUTER).strip()
    if model == "auto":
        model = orc.get("resolved_model") or OPENROUTER_FREE_ROUTER

    if provider != "openrouter":
        logs.append("OpenRouter is not the active provider — select ☁️ OpenRouter first.")
        return JSONResponse({"ok": False, "error": "OpenRouter is not the active provider.",
                             "logs": logs})
    if not has_key:
        logs.append("No OpenRouter API key is set — add your key and Apply, then test again.")
        return JSONResponse({"ok": False, "error": "No OpenRouter API key set.", "logs": logs})

    # Make sure the live client reflects the saved OpenRouter config.
    _apply_openrouter_config(cfg)
    base_url = _pr.LMSTUDIO_BASE_URL
    logs.append(f"Endpoint  : {base_url}")
    logs.append(f"Model     : {model}")
    logs.append(f"API key   : set ({'•' * 6}{(_openrouter_api_key() or '')[-4:]})")
    logs.append(f"Headers   : {', '.join(sorted(_pr.LLM_EXTRA_HEADERS)) or '(none)'}")
    fb = (orc.get("models_fallback") or [])[:_OPENROUTER_MODELS_MAX]
    logs.append(f"Routing   : sort=throughput, fallbacks={len(fb)} vision model(s)")
    logs.append("→ Sending: \"Reply with the single word: OK\"")

    def _roundtrip():
        client = _pr.make_client()
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model or OPENROUTER_FREE_ROUTER,
            messages=[{"role": "user",
                       "content": "Reply with the single word: OK"}],
            max_tokens=16, temperature=0,
            extra_body=dict(_pr.LLM_EXTRA_BODY),
        )
        dt = (time.time() - t0) * 1000.0
        text = ""
        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            pass
        used = getattr(resp, "model", "") or ""
        return dt, text, used

    loop = asyncio.get_event_loop()
    try:
        dt, text, used = await loop.run_in_executor(None, _roundtrip)
        logs.append(f"← Received in {round(dt)} ms")
        if used:
            logs.append(f"Model used: {used}")
        logs.append(f"Reply     : {text or '(empty)'}")
        logs.append("✓ Full send → receive round-trip succeeded.")
        return JSONResponse({
            "ok":              True,
            "latency_ms":      round(dt),
            "response_text":   text,
            "model_used":      used,
            "requested_model": model,
            "base_url":        base_url,
            "logs":            logs,
        })
    except Exception as exc:
        msg = str(exc)
        logs.append(f"✗ Request failed: {msg}")
        hint = ""
        low = msg.lower()
        if "401" in msg or "auth" in low or "key" in low:
            hint = "Looks like an authentication problem — double-check your API key."
        elif "404" in msg or "no endpoints" in low or "not a valid model" in low:
            hint = "The selected model may be unavailable — try the free router or Refresh the list."
        elif "429" in msg or "rate" in low:
            hint = "Rate-limited — wait a moment and try again, or pick a different free model."
        elif "timeout" in low or "connection" in low:
            hint = "Network issue reaching openrouter.ai — check your internet connection."
        if hint:
            logs.append(hint)
        return JSONResponse({"ok": False, "error": msg, "hint": hint,
                             "requested_model": model, "base_url": base_url,
                             "logs": logs})


@app.post("/llm-server/autodetect")
async def llm_server_autodetect():
    """Probe well-known endpoints and adopt the first reachable LLM server.

    Recovers automatically from a stale or incorrect saved server choice (e.g. a
    "docker" selection pinned to :11434 while LM Studio is on :1234). On success
    the detected URL is applied immediately AND persisted (as a custom server),
    so the fix sticks across restarts and overrides any bad saved preference.
    """
    loop  = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, _autodetect_llm_url)
    if not found:
        return JSONResponse({
            "ok":       False,
            "base_url": getattr(_pr, "LMSTUDIO_BASE_URL", ""),
            "tried":    _candidate_llm_urls(),
        })
    _reset_local_llm_runtime()
    _pr.LMSTUDIO_BASE_URL = found
    cfg = _load_config()
    cfg["provider"]   = "local"
    cfg["llm_server"] = {"server_type": "custom", "base_url": found}
    _save_config(cfg)
    # Re-adopt a model now that we have a live endpoint (best-effort, off-thread).
    # run_in_executor returns a Future (not a coroutine) — schedule and don't await.
    loop.run_in_executor(None, _pr.initialize_models)
    return JSONResponse({"ok": True, "base_url": found})


@app.post("/llm-server/start")
async def llm_server_start():
    """Start the bundled Docker LLM server (best-effort)."""
    try:
        subprocess.Popen(
            ["docker", "compose", "--profile", "bundled-llm", "up", "-d", "model-server"],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True})


@app.post("/llm-server/stop")
async def llm_server_stop():
    """Stop the bundled Docker LLM server (best-effort)."""
    try:
        subprocess.Popen(
            ["docker", "compose", "stop", "model-server"],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True})


@app.post("/llm-server/restart")
async def llm_server_restart():
    """Restart the bundled Docker LLM server (best-effort)."""
    try:
        subprocess.Popen(
            ["docker", "compose", "--profile", "bundled-llm", "restart", "model-server"],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True})


@app.post("/llm-server/load")
async def llm_server_load():
    """Trigger model warm-up / load into LLM server memory."""
    # run_in_executor returns a Future (not a coroutine), so it must NOT be
    # wrapped in asyncio.create_task (which raises TypeError and 500s the call).
    # Scheduling it is fire-and-forget; we don't await the warm-up.
    asyncio.get_event_loop().run_in_executor(None, _pr.warm_up_model)
    return JSONResponse({"ok": True, "message": "Model warm-up started"})


# ── Folder / file-manager helpers ──────────────────────────────────────────────
# (Docker detection reuses _in_docker() defined above — same /.dockerenv probe.)

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
    if _in_docker():
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
    if _in_docker():
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
        if _in_docker():
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
    autorotate:              bool | None = None
    autocrop:                bool | None = None
    autocrop_aggressiveness: int | None = None
    grayscale:               bool | None = None
    compress:                bool | None = None
    local_ocr:               bool | None = None
    jpeg_quality:            int | None = None
    max_parallel:            int | None = None
    llm_timeout:             float | None = None
    llm_max_retries:         int | None = None
    store_max_px:            int | None = None
    pdf_max_pages:           int | None = None
    max_upload_mb:           int | None = None
    rate_limit_enabled:      bool | None = None
    rate_limit_per_min:      int | None = None
    llm_429_wait_enabled:    bool | None = None
    llm_429_max_wait:        float | None = None


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
        if body.autocrop_aggressiveness is not None:
            proc["autocrop_aggressiveness"] = max(0, min(100, int(body.autocrop_aggressiveness)))
        if body.grayscale is not None: proc["grayscale"] = bool(body.grayscale)
        if body.compress  is not None: proc["compress"]  = bool(body.compress)
        if body.local_ocr is not None: proc["local_ocr"] = bool(body.local_ocr)
        if body.jpeg_quality is not None:
            proc["jpeg_quality"] = max(40, min(95, int(body.jpeg_quality)))
        if body.max_parallel is not None:
            proc["max_parallel"] = max(1, min(8, int(body.max_parallel)))
        if body.llm_timeout is not None:
            proc["llm_timeout"] = max(10.0, min(600.0, float(body.llm_timeout)))
        if body.llm_max_retries is not None:
            proc["llm_max_retries"] = max(0, min(5, int(body.llm_max_retries)))
        if body.store_max_px is not None:
            proc["store_max_px"] = max(512, min(4000, int(body.store_max_px)))
        if body.pdf_max_pages is not None:
            proc["pdf_max_pages"] = max(1, min(200, int(body.pdf_max_pages)))
        if body.max_upload_mb is not None:
            proc["max_upload_mb"] = max(0, min(2000, int(body.max_upload_mb)))
        if body.rate_limit_enabled is not None:
            proc["rate_limit_enabled"] = bool(body.rate_limit_enabled)
        if body.rate_limit_per_min is not None:
            proc["rate_limit_per_min"] = max(1, min(1000, int(body.rate_limit_per_min)))
        if body.llm_429_wait_enabled is not None:
            proc["llm_429_wait_enabled"] = bool(body.llm_429_wait_enabled)
        if body.llm_429_max_wait is not None:
            proc["llm_429_max_wait"] = max(0.0, min(120.0, float(body.llm_429_max_wait)))
        cfg["processing"] = proc
        _save_config(cfg)
        applied = _apply_processing_config(cfg)
        return JSONResponse({"ok": True, **applied})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# Recommended image-processing presets for common receipt sources. A scan app
# (CamScanner, Adobe Scan, Genius Scan, …) already perspective-corrects, crops
# tight to the document edge, and boosts contrast — so re-cropping in this app is
# at best a no-op and at worst trims the receipt edge. The "scanned" preset
# therefore turns auto-crop OFF and keeps auto-rotate + B&W (which only help, and
# are near no-ops on an already-clean scan). "photo" is the opposite: raw phone
# photos need the full auto-rotate → B&W → aggressive auto-crop chain.
_PROCESSING_PRESETS: dict[str, dict] = {
    "scanned": {
        "autorotate": True,
        "autocrop":   False,
        "grayscale":  True,
    },
    "photo": {
        "autorotate":              True,
        "autocrop":                True,
        "autocrop_aggressiveness": 85,
        "grayscale":               True,
    },
}
# CamScanner is the headline scan-app source; alias it to the generic preset.
_PROCESSING_PRESETS["camscanner"] = _PROCESSING_PRESETS["scanned"]


class ProcessingPresetRequest(BaseModel):
    preset: str


@app.post("/settings/processing/preset")
async def apply_processing_preset(body: ProcessingPresetRequest):
    """Apply a named image-processing preset (e.g. ``scanned`` for CamScanner /
    Adobe Scan exports, ``photo`` for raw phone photos) and persist it."""
    name = (body.preset or "").strip().lower()
    preset = _PROCESSING_PRESETS.get(name)
    if preset is None:
        return JSONResponse(
            {"ok": False, "error": f"unknown preset: {name!r}",
             "presets": sorted(_PROCESSING_PRESETS)}, status_code=400)
    cfg = _load_config()
    proc = cfg.get("processing") or {}
    proc.update(preset)
    cfg["processing"] = proc
    _save_config(cfg)
    applied = _apply_processing_config(cfg)
    return JSONResponse({"ok": True, "preset": name, "preset_values": preset, **applied})


class AuditSettingsRequest(BaseModel):
    # Any field may be null/"" to clear (disable) that warning.
    fuel_limit:   float | str | None = None
    mats_limit:   float | str | None = None
    misc_limit:   float | str | None = None
    max_age_days: float | str | None = None


@app.get("/settings/audit")
async def get_audit_settings():
    return JSONResponse(_audit_settings())


@app.post("/settings/audit")
async def save_audit_settings(body: AuditSettingsRequest):
    """Set the optional spending/date warnings. Any blank field clears (disables)
    that warning — by default nothing is set, so no warnings fire."""
    try:
        cfg = _load_config()
        audit = cfg.get("audit") or {}
        limits = audit.get("amount_limits") or {}
        for cat, val in (("fuel", body.fuel_limit), ("mats", body.mats_limit),
                         ("misc", body.misc_limit)):
            limits[cat] = _coerce_pos_num(val)
        audit["amount_limits"] = limits
        n = _coerce_pos_num(body.max_age_days)
        audit["max_age_days"] = int(n) if n is not None else None
        cfg["audit"] = audit
        _save_config(cfg)
        applied = _apply_audit_config(cfg)
        return JSONResponse({"ok": True, **applied})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Per diem ───────────────────────────────────────────────────────────────────
# An opt-in daily allowance added to the report: rate ($/day) × duration (days)
# becomes a "Per Diem" line on the Summary sheet, included in the grand TOTAL.
# Persisted in config; read at generate time (web Generate + Send Report Now —
# the watch-mode/scheduler export paths deliberately stay receipts-only).

class PerDiemRequest(BaseModel):
    enabled: bool = False
    rate:    float | str | None = None
    days:    int | str | None = None


def _per_diem_config() -> dict:
    """The sanitized per-diem block: {"enabled", "rate", "days"} with finite,
    non-negative values (bad/inf/nan stored values read back as 0 → inert)."""
    block = _load_config().get("per_diem") or {}
    try:
        rate = float(block.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    try:
        days = int(block.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if not math.isfinite(rate) or rate < 0:
        rate = 0.0
    return {"enabled": bool(block.get("enabled")), "rate": round(rate, 2),
            "days": max(0, min(days, 3650))}


@app.get("/settings/per-diem")
async def get_per_diem():
    pd = _per_diem_config()
    pd["total"] = round(pd["rate"] * pd["days"], 2) if pd["enabled"] else 0.0
    return JSONResponse(pd)


@app.post("/settings/per-diem")
async def set_per_diem(body: PerDiemRequest):
    try:
        rate = _coerce_pos_num(body.rate) or 0.0
        if not math.isfinite(rate):           # inf/nan would poison the config + total
            rate = 0.0
        try:
            days = int(float(body.days)) if body.days not in (None, "") else 0
        except (TypeError, ValueError, OverflowError):
            days = 0
        cfg = _load_config()
        cfg["per_diem"] = {
            "enabled": bool(body.enabled),
            "rate":    round(rate, 2),
            "days":    max(0, min(days, 3650)),
        }
        _save_config(cfg)
        return JSONResponse({"ok": True, **_per_diem_config()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/benchmarks")
async def get_benchmarks():
    """Per-batch processing-time history, newest first — for comparing LLMs."""
    with _bench_lock:
        entries = list(_benchmarks)
    return JSONResponse({"benchmarks": entries, "insights": _benchmark_insights(entries)})


@app.post("/benchmarks/clear")
async def clear_benchmarks():
    with _bench_lock:
        _benchmarks.clear()
    _persist_state()
    return JSONResponse({"ok": True})


def _benchmarks_csv(entries: list[dict]) -> str:
    """Render the benchmark history as a long-format CSV — one row per batch step
    (plus a summary row for step-less batches) so EVERY step taken is downloadable
    and opens directly in any spreadsheet app."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "when", "receipts", "batch_total_seconds", "batch_avg_seconds",
        "distill_model", "ocr_model",
        "step", "step_label", "step_runs", "step_failures",
        "step_total_seconds", "step_avg_seconds",
    ])
    for e in entries:
        base = [
            e.get("ts", ""), e.get("count", ""),
            e.get("total_seconds", ""), e.get("avg_seconds", ""),
            e.get("distill_model", ""), e.get("ocr_model", ""),
        ]
        steps = e.get("steps") or []
        if not steps:
            w.writerow(base + ["", "", "", "", "", ""])
            continue
        for s in steps:
            w.writerow(base + [
                s.get("step", ""), s.get("label", ""),
                s.get("count", ""), s.get("failures", ""),
                s.get("total_seconds", ""), s.get("avg_seconds", ""),
            ])
    return buf.getvalue()


@app.get("/benchmarks/download")
async def download_benchmarks():
    """Download the full benchmark history (incl. per-step timings) as a CSV file."""
    with _bench_lock:
        entries = list(_benchmarks)
    csv_text = _benchmarks_csv(entries)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="benchmarks_{stamp}.csv"'},
    )


# ── Run log (full per-batch detail) ────────────────────────────────────────────

@app.get("/runs")
async def list_runs():
    """Newest-first summaries of every captured processing run, for the picker."""
    with _runs_lock:
        runs = list(_runs)
    summaries = []
    for r in runs:
        instr    = r.get("instructions") or {}
        receipts = r.get("receipts") or []
        summaries.append({
            "id":            r.get("id"),
            "ts_start":      r.get("ts_start"),
            "ts_end":        r.get("ts_end"),
            "count":         r.get("count"),
            "total_seconds": r.get("total_seconds"),
            "provider":      instr.get("provider"),
            "distill_model": instr.get("distill_model"),
            "send_image":    instr.get("send_image"),
            "done":          sum(1 for x in receipts if x.get("status") == "done"),
            "failed":        sum(1 for x in receipts if x.get("status") == "failed"),
            "lines":         len(r.get("lines") or []),
        })
    return JSONResponse({"runs": summaries})


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Full detail for one run: instructions sent, streamed log, per-receipt steps."""
    with _runs_lock:
        run = next((r for r in _runs if r.get("id") == run_id), None)
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    return JSONResponse(run)


@app.get("/runs/{run_id}/download")
async def download_run(run_id: str):
    """Download one run as a self-contained plain-text report."""
    with _runs_lock:
        run = next((r for r in _runs if r.get("id") == run_id), None)
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    text = _format_run_text(run)
    return Response(
        text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.txt"'},
    )


@app.post("/runs/clear")
async def clear_runs():
    with _runs_lock:
        _runs.clear()
    _persist_state()
    return JSONResponse({"ok": True})


@app.get("/settings/llm-instructions")
async def get_llm_instructions():
    """Exactly what the app sends to the LLM right now — the system + user prompt
    for each pipeline stage, the privacy gate, and the OpenRouter routing/headers.
    Powers the 'Instructions sent to the model' panel so nothing is hidden."""
    return JSONResponse(_llm_instructions_payload())


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
    subject_template: str = ""   # blank = use email_subject / built-in default
    body_template: str = ""      # blank = built-in default body


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
        "subject_template": em.get("subject_template", ""),
        "body_template":    em.get("body_template", ""),
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
        email["subject_template"] = body.subject_template.strip()
        email["body_template"]    = body.body_template.strip()
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


# ── Inbound email (IMAP) receipt intake ─────────────────────────────────────────
# Poll a dedicated mailbox and feed forwarded receipts (any vendor) into the same
# queue/board as uploads. See email_intake.py + GAS_RECEIPT_IMPORT.md. The inbox is
# instance-level (one mailbox per box); in multi-user mode config is admin-only and
# plus-addressing (receipts+<user>@…) can route a message to that user.

def _email_intake_config() -> email_intake.ImapConfig:
    return email_intake.ImapConfig.from_dict(_load_config().get("email_intake"))


def _imap_password() -> str:
    return app_secrets.get_secret("imap_password", "email_intake", "password", "IMAP_PASSWORD")


def _email_seen_path() -> Path:
    return multiuser.default_workspace().out_folder / ".email_seen.json"


def _load_email_seen() -> set[str]:
    try:
        p = _email_seen_path()
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception:
        pass
    return set()


def _save_email_seen(seen: set[str]) -> None:
    try:
        p = _email_seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Keep the newest 5000 ids so the guard file can't grow without bound.
        p.write_text(json.dumps(list(seen)[-5000:]))
    except Exception as exc:
        print(f"[email] could not persist seen ids: {exc}")


def _enqueue_receipt_file(path: Path, *, employee: str = "Email Import",
                          job_name: str = "", job_number: str = "") -> bool:
    """Drop one staged receipt file (image / PDF / text body) onto the work queue,
    expanding a PDF into pages. Mirrors the upload path; tags the current user."""
    suffix = path.suffix.lower()
    targets: list[Path] = []
    if suffix in PDF_EXTENSIONS:
        try:
            pages = pdf_to_images(path, path.parent / f"_pdf_{path.stem}")
            path.unlink(missing_ok=True)
            targets = list(pages)
        except Exception as exc:
            _emit_log(f"[email] PDF error {path.name}: {exc}", level="error")
            return False
    elif suffix in IMAGE_EXTENSIONS or _pr._is_text_source(path):
        targets = [path]
    else:
        return False

    queued = False
    for t in targets:
        if _is_active_in_kanban(t.name):
            continue
        with _seen_lock:
            _seen_intake.add(t.name)
        item = {"filename": t.name, "path": str(t), "employee": employee,
                "job_name": job_name, "job_number": job_number}
        _cache_item(item)
        with _work_lock:
            _work_queue.append(_tag_item(item))
        _update_kanban(t.name, "queued", None)
        _broadcast({"type": "kanban_update", "filename": t.name,
                    "status": "queued", "data": {}, "model": ""})
        queued = True
    return queued


def _ingest_email_message(msg, artifacts) -> bool:
    """Handle one parsed email: route to a user (plus-addressing), stage each
    receipt artifact to that user's workspace, and enqueue it."""
    cfg = _email_intake_config()
    user_id = multiuser.DEFAULT_USER
    if multiuser.ENABLED and cfg.plus_routing:
        tag = email_intake.route_user(msg)
        if tag and multiuser.valid_user_id(tag) and users.get_user(tag):
            user_id = tag

    token = multiuser.bind_user(user_id)
    try:
        staged_dir = IMAGES_FOLDER / f"_email_{uuid4().hex[:8]}"
        staged_dir.mkdir(parents=True, exist_ok=True)
        employee = _last_context.get("employee") or "Email Import"
        queued_any = False
        for art in artifacts:
            name = Path(art.filename).name
            dest = staged_dir / name
            try:
                if isinstance(art.data, str):
                    dest.write_text(art.data)
                else:
                    dest.write_bytes(art.data)
            except Exception as exc:
                _emit_log(f"[email] could not stage {name}: {exc}", level="error")
                continue
            if _enqueue_receipt_file(dest, employee=employee):
                queued_any = True
        subj = email_intake.message_subject(msg)
        whom = "" if user_id == multiuser.DEFAULT_USER else f" → {user_id}"
        _emit_log(f"[email] {subj or '(no subject)'}: {len(artifacts)} receipt(s) queued{whom}")
        if queued_any:
            _ensure_worker_alive()
        return queued_any
    finally:
        multiuser.reset(token)


def _run_email_poller() -> None:
    """Background loop: poll the configured IMAP mailbox and ingest new receipts."""
    seen = _load_email_seen()
    while not _worker_cancel.is_set():
        cfg = _email_intake_config()
        interval = cfg.poll_seconds if cfg.poll_seconds else 120
        if cfg.enabled and cfg.host and cfg.username:
            pw = _imap_password()
            if pw:
                try:
                    summary = email_intake.poll_once(cfg, pw, _ingest_email_message,
                                                     already_seen=seen)
                    for mid in summary.get("seen_ids", []):
                        seen.add(mid)
                    if summary.get("seen_ids"):
                        _save_email_seen(seen)
                    if summary.get("receipts"):
                        _emit_log(f"[email] {cfg.username}: {summary['messages']} message(s), "
                                  f"{summary['receipts']} receipt(s) ingested")
                except Exception as exc:
                    _emit_log(f"[email] poll error: {exc}", level="error")
        _worker_cancel.wait(timeout=max(15, interval))


class EmailIntakeRequest(BaseModel):
    enabled:       bool = False
    host:          str = ""
    port:          int = 993
    username:      str = ""
    password:      str = ""     # blank = keep previously saved app password
    use_ssl:       bool = True
    mailbox:       str = "INBOX"
    poll_seconds:  int = 120
    mark_seen:     bool = True
    process_body:  bool = True
    plus_routing:  bool = False
    allow_senders: str = ""


def _email_admin_or_403(request: Request):
    """The IMAP inbox is instance-level + holds credentials: admin-only in MU mode."""
    if multiuser.ENABLED and not users.is_admin(getattr(request.state, "user_id", "")):
        return JSONResponse({"error": "admin_only"}, status_code=403)
    return None


@app.get("/settings/email-intake")
async def get_email_intake(request: Request):
    guard = _email_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _email_intake_config()
    out = cfg.to_public_dict()
    out["password_set"] = bool(_imap_password())
    out["configured"] = bool(cfg.host and cfg.username and _imap_password())
    out["multiuser"] = multiuser.ENABLED
    # Surface the verified fuel-receipt senders + the recommended label so the UI
    # can offer a one-click sender fill and a "use a label" nudge.
    out["fuel_senders"] = list(email_intake.FUEL_RECEIPT_SENDERS)
    out["recommended_label"] = gmail_filter.DEFAULT_LABEL
    return JSONResponse(out)


@app.post("/settings/email-intake")
async def set_email_intake(body: EmailIntakeRequest, request: Request):
    guard = _email_admin_or_403(request)
    if guard is not None:
        return guard
    try:
        cfg = _load_config()
        block = cfg.get("email_intake") or {}
        block.update({
            "enabled":       bool(body.enabled),
            "host":          body.host.strip(),
            "port":          int(body.port or 993),
            "username":      body.username.strip(),
            "use_ssl":       bool(body.use_ssl),
            "mailbox":       body.mailbox.strip() or "INBOX",
            "poll_seconds":  max(15, int(body.poll_seconds or 120)),
            "mark_seen":     bool(body.mark_seen),
            "process_body":  bool(body.process_body),
            "plus_routing":  bool(body.plus_routing),
            "allow_senders": [s.strip() for s in (body.allow_senders or "").split(",") if s.strip()],
        })
        block.pop("password", None)   # never keep the secret in the synced config
        if body.password:             # blank keeps the previously saved password
            app_secrets.save_secret("imap_password", body.password)
        cfg["email_intake"] = block
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/settings/email-intake/test")
async def test_email_intake(request: Request):
    guard = _email_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _email_intake_config()
    pw = _imap_password()

    def _run():
        return email_intake.test_connection(cfg, pw)
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/settings/email-intake/poll-now")
async def poll_email_now(request: Request):
    guard = _email_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _email_intake_config()
    pw = _imap_password()
    if not (cfg.host and cfg.username and pw):
        return JSONResponse({"ok": False, "error": "Configure host, username and app password first."},
                            status_code=400)

    seen = _load_email_seen()

    def _run():
        return email_intake.poll_once(cfg, pw, _ingest_email_message, already_seen=seen)
    try:
        summary = await asyncio.get_event_loop().run_in_executor(None, _run)
        for mid in summary.get("seen_ids", []):
            seen.add(mid)
        _save_email_seen(seen)
        return JSONResponse({"ok": True, **summary})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/settings/email-intake/gmail-filter")
async def download_gmail_filter(request: Request):
    """Download an importable Gmail filter (Gmail → Settings → Filters → Import) that
    labels receipt mail so the intake can poll a clean label instead of the inbox."""
    guard = _email_admin_or_403(request)
    if guard is not None:
        return guard
    xml = gmail_filter.build_gmail_filter_xml()
    return Response(
        content=xml, media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=gmail_receipts_filter.xml"},
    )


# ── Google Drive receipt intake (opt-in, off by default) ────────────────────────
# Poll a Drive "receipts inbox" folder (filled from a phone or the Gmail→Drive Apps
# Script) and download new image/PDF files into INTAKE_FOLDER, where the existing
# folder watcher + pipeline take over unchanged. Dedup is by Drive file ID. The
# OAuth refresh token + client secret live in app_secrets (never the synced config).
# See gdrive_intake.py + GOOGLE_DRIVE_IMPORT.md. Instance-level (admin-only in MU
# mode), mirroring the email-intake card.

def _gdrive_config() -> gdrive_intake.GDriveConfig:
    return gdrive_intake.GDriveConfig.from_dict(_load_config().get("gdrive"))


def _gdrive_client_secret() -> str:
    return app_secrets.get_secret("gdrive_client_secret", env="GDRIVE_CLIENT_SECRET")


def _gdrive_token() -> str:
    return app_secrets.get_secret("gdrive_token", env="GDRIVE_REFRESH_TOKEN")


def _gdrive_redirect_uri() -> str:
    # Google's standard installed-app loopback. The one-time consent happens in the
    # user's browser; they paste the resulting code (or full redirected URL) back in.
    return os.getenv("GDRIVE_REDIRECT_URI", "http://localhost")


def _gdrive_seen_path() -> Path:
    return multiuser.default_workspace().out_folder / ".gdrive_seen.json"


def _load_gdrive_seen() -> set[str]:
    try:
        p = _gdrive_seen_path()
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception:
        pass
    return set()


def _save_gdrive_seen(seen: set[str]) -> None:
    try:
        p = _gdrive_seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Keep the newest 5000 ids so the guard file can't grow without bound.
        p.write_text(json.dumps(list(seen)[-5000:]))
    except Exception as exc:
        print(f"[gdrive] could not persist seen ids: {exc}")


def _build_gdrive_service():
    """Build the Drive service from the saved config + secrets, or None."""
    cfg = _gdrive_config()
    try:
        return gdrive_intake.build_service(cfg, _gdrive_client_secret(), _gdrive_token())
    except Exception as exc:
        _emit_log(f"[gdrive] could not build Drive client: {exc}", level="error")
        return None


def _gdrive_poll(cfg: gdrive_intake.GDriveConfig, seen: set[str]) -> dict:
    """One Drive poll: download new files into the default intake folder."""
    service = _build_gdrive_service()
    if service is None:
        return {"files": 0, "downloaded": 0, "skipped": 0, "seen_ids": [],
                "error": "not connected"}
    intake_dir = multiuser.default_workspace().intake_folder
    summary = gdrive_intake.poll_once(service, cfg, intake_dir, already_seen=seen)
    for fid in summary.get("seen_ids", []):
        seen.add(fid)
    if summary.get("seen_ids"):
        _save_gdrive_seen(seen)
    if summary.get("downloaded"):
        _emit_log(f"[gdrive] {summary['downloaded']} new receipt(s) downloaded "
                  f"from Drive (folder {cfg.folder_id[:12]}…)")
    return summary


def _gdrive_upload_report(workbook_path, receipt_paths, date_str: str) -> dict | None:
    """Best-effort: upload a finished report (workbook + receipt images) to the
    provisioned ``Receipt App/Output/<date_str>/`` Drive folder. No-op unless Drive
    is connected, the tree was provisioned, and output upload is enabled."""
    block = _load_config().get("gdrive") or {}
    if not block.get("upload_output"):
        return None
    output_id = (block.get("tree") or {}).get("output", "")
    if not output_id or not _gdrive_token():
        return None
    service = _build_gdrive_service()
    if service is None:
        return None
    try:
        summary = gdrive_intake.upload_report_bundle(
            service, output_id, date_str, workbook_path, receipt_paths)
        _emit_log(f"[gdrive] uploaded report to Output/{date_str} "
                  f"({len(summary.get('receipts', []))} receipt(s))")
        return summary
    except Exception as exc:
        _emit_log(f"[gdrive] report upload failed: {exc}", level="error")
        return None


def _run_gdrive_poller() -> None:
    """Background loop: poll the configured Drive folder for new receipts."""
    seen = _load_gdrive_seen()
    while not _worker_cancel.is_set():
        cfg = _gdrive_config()
        interval = cfg.poll_interval if cfg.poll_interval else 300
        if cfg.enabled and cfg.folder_id and _gdrive_token():
            try:
                _gdrive_poll(cfg, seen)
            except Exception as exc:
                _emit_log(f"[gdrive] poll error: {exc}", level="error")
        _worker_cancel.wait(timeout=max(30, interval))


class GDriveSettingsRequest(BaseModel):
    enabled:        bool = False
    folder_id:      str = ""
    poll_interval:  int = 300
    scope:          str = "drive.readonly"
    move_processed: bool = False
    client_id:      str = ""
    client_secret:  str = ""   # blank = keep previously saved secret


class GDriveConnectRequest(BaseModel):
    code:          str = ""    # OAuth authorization code from the consent screen
    refresh_token: str = ""    # or paste a refresh token directly (advanced)
    redirect_uri:  str = ""


def _gdrive_admin_or_403(request: Request):
    """Drive intake is instance-level + holds an OAuth token: admin-only in MU mode."""
    if multiuser.ENABLED and not users.is_admin(getattr(request.state, "user_id", "")):
        return JSONResponse({"error": "admin_only"}, status_code=403)
    return None


@app.get("/settings/gdrive")
async def get_gdrive(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _gdrive_config()
    out = cfg.to_public_dict()
    out["client_secret_set"] = bool(_gdrive_client_secret())
    out["connected"] = bool(_gdrive_token())
    out["configured"] = bool(cfg.folder_id and cfg.client_id and _gdrive_token())
    out["multiuser"] = multiuser.ENABLED
    _block = _load_config().get("gdrive") or {}
    out["provisioned"] = bool((_block.get("tree") or {}).get("output"))
    out["upload_output"] = bool(_block.get("upload_output"))
    return JSONResponse(out)


@app.post("/settings/gdrive")
async def set_gdrive(body: GDriveSettingsRequest, request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    try:
        cfg = _load_config()
        block = cfg.get("gdrive") or {}
        scope = body.scope.strip() if body.scope.strip() in ("drive.readonly", "drive.file") else "drive.readonly"
        block.update({
            "enabled":        bool(body.enabled),
            "folder_id":      body.folder_id.strip(),
            "poll_interval":  max(30, int(body.poll_interval or 300)),
            "scope":          scope,
            "move_processed": bool(body.move_processed),
            "client_id":      body.client_id.strip(),
        })
        block.pop("client_secret", None)   # never keep the secret in the synced config
        block.pop("token", None)
        if body.client_secret:             # blank keeps the previously saved secret
            app_secrets.save_secret("gdrive_client_secret", body.client_secret)
        cfg["gdrive"] = block
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/settings/gdrive/provision")
async def gdrive_provision(request: Request):
    """Create (or reuse) the ``Receipt App/{Intake,Output}`` tree in the user's Drive,
    point the poller's intake at the provisioned Intake folder, and enable uploading
    finished reports into dated Output subfolders. Requires a connected account with
    the drive.file scope."""
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    if not _gdrive_token():
        return JSONResponse({"ok": False, "error": "Connect Google Drive first."},
                            status_code=400)
    service = _build_gdrive_service()
    if service is None:
        return JSONResponse({"ok": False, "error": "Could not build the Drive client."},
                            status_code=400)
    try:
        tree = await asyncio.get_running_loop().run_in_executor(
            None, lambda: gdrive_intake.provision_tree(service))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Provisioning failed: {exc}"},
                            status_code=400)
    cfg = _load_config()
    block = cfg.get("gdrive") or {}
    block["tree"] = tree
    block["folder_id"] = tree["intake"]    # poller now reads the provisioned Intake
    block["upload_output"] = True
    cfg["gdrive"] = block
    _save_config(cfg)
    return JSONResponse({"ok": True, "tree": tree})


@app.get("/settings/gdrive/auth-url")
async def gdrive_auth_url(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _gdrive_config()
    secret = _gdrive_client_secret()
    if not (cfg.client_id and secret):
        return JSONResponse({"ok": False, "error": "Save the OAuth client id and secret first."},
                            status_code=400)
    try:
        url = gdrive_intake.auth_url(cfg, secret, _gdrive_redirect_uri())
        return JSONResponse({"ok": True, "url": url, "redirect_uri": _gdrive_redirect_uri()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/settings/gdrive/connect")
async def gdrive_connect(body: GDriveConnectRequest, request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _gdrive_config()
    secret = _gdrive_client_secret()
    token = ""
    try:
        if body.refresh_token.strip():
            token = body.refresh_token.strip()
        elif body.code.strip():
            redirect = body.redirect_uri.strip() or _gdrive_redirect_uri()
            token = gdrive_intake.exchange_code(cfg, secret, body.code.strip(), redirect)
        else:
            return JSONResponse({"ok": False, "error": "Provide an authorization code or a refresh token."},
                                status_code=400)
        if not token:
            return JSONResponse({"ok": False, "error": "No refresh token returned — re-consent with prompt=consent."},
                                status_code=400)
        app_secrets.save_secret("gdrive_token", token)
        return JSONResponse({"ok": True, "connected": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/settings/gdrive/disconnect")
async def gdrive_disconnect(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    tok = _gdrive_token()

    def _run():
        return gdrive_intake.revoke_token(tok)
    try:
        await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception:
        pass
    app_secrets.save_secret("gdrive_token", "")   # always clear locally
    return JSONResponse({"ok": True, "connected": False})


@app.post("/settings/gdrive/test")
async def test_gdrive(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _gdrive_config()

    def _run():
        service = _build_gdrive_service()
        return gdrive_intake.test_connection(service, cfg)
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/settings/gdrive/poll-now")
async def poll_gdrive_now(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _gdrive_config()
    if not (cfg.folder_id and _gdrive_token()):
        return JSONResponse({"ok": False, "error": "Connect Google and set the inbox folder ID first."},
                            status_code=400)
    seen = _load_gdrive_seen()

    def _run():
        return _gdrive_poll(cfg, seen)
    try:
        summary = await asyncio.get_event_loop().run_in_executor(None, _run)
        if summary.get("downloaded"):
            _ensure_worker_alive()
        return JSONResponse({"ok": True, **summary})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ── Microsoft OneDrive intake ──────────────────────────────────────────────────
# See onedrive_intake.py + ONEDRIVE_IMPORT.md. Instance-level (admin-only in MU
# mode), mirroring the Google Drive card. NB: Microsoft ROTATES refresh tokens —
# every Graph client build persists the replacement token it hands back.

def _onedrive_config() -> onedrive_intake.OneDriveConfig:
    return onedrive_intake.OneDriveConfig.from_dict(_load_config().get("onedrive"))


def _onedrive_client_secret() -> str:
    # Usually blank: the recommended Azure registration is a PUBLIC client (device
    # flow, "Allow public client flows" on) which needs no secret at all.
    return app_secrets.get_secret("onedrive_client_secret", env="ONEDRIVE_CLIENT_SECRET")


def _onedrive_token() -> str:
    return app_secrets.get_secret("onedrive_token", env="ONEDRIVE_REFRESH_TOKEN")


def _onedrive_seen_path() -> Path:
    return multiuser.default_workspace().out_folder / ".onedrive_seen.json"


def _load_onedrive_seen() -> set[str]:
    try:
        p = _onedrive_seen_path()
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return set(str(x) for x in data)
    except Exception:
        pass
    return set()


def _save_onedrive_seen(seen: set[str]) -> None:
    try:
        p = _onedrive_seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Keep the newest 5000 ids so the guard file can't grow without bound.
        p.write_text(json.dumps(list(seen)[-5000:]))
    except Exception as exc:
        print(f"[onedrive] could not persist seen ids: {exc}")


def _build_onedrive_graph():
    """Build the Graph client from the saved config + secrets (or None), persisting
    the rotated refresh token Microsoft returns on every redeem."""
    cfg = _onedrive_config()
    try:
        graph, rotated = onedrive_intake.build_graph(
            cfg, _onedrive_token(), _onedrive_client_secret())
        if graph is not None and rotated:
            app_secrets.save_secret("onedrive_token", rotated)
        return graph
    except Exception as exc:
        _emit_log(f"[onedrive] could not build Graph client: {exc}", level="error")
        return None


def _onedrive_poll(cfg: onedrive_intake.OneDriveConfig, seen: set[str]) -> dict:
    """One OneDrive poll: download new files into the default intake folder."""
    graph = _build_onedrive_graph()
    if graph is None:
        return {"files": 0, "downloaded": 0, "skipped": 0, "seen_ids": [],
                "error": "not connected"}
    intake_dir = multiuser.default_workspace().intake_folder
    summary = onedrive_intake.poll_once(graph, cfg, intake_dir, already_seen=seen)
    for fid in summary.get("seen_ids", []):
        seen.add(fid)
    if summary.get("seen_ids"):
        _save_onedrive_seen(seen)
    if summary.get("downloaded"):
        _emit_log(f"[onedrive] {summary['downloaded']} new receipt(s) downloaded "
                  f"from OneDrive (folder {cfg.folder_path[:24]}…)")
    return summary


def _run_onedrive_poller() -> None:
    """Background loop: poll the configured OneDrive folder for new receipts."""
    seen = _load_onedrive_seen()
    while not _worker_cancel.is_set():
        cfg = _onedrive_config()
        interval = cfg.poll_interval if cfg.poll_interval else 300
        if cfg.enabled and cfg.folder_path and _onedrive_token():
            try:
                _onedrive_poll(cfg, seen)
            except Exception as exc:
                _emit_log(f"[onedrive] poll error: {exc}", level="error")
        _worker_cancel.wait(timeout=max(30, interval))


class OneDriveSettingsRequest(BaseModel):
    enabled:       bool = False
    folder_path:   str = "Receipts"
    poll_interval: int = 300
    scope:         str = "files.read"
    client_id:     str = ""
    tenant:        str = "consumers"
    client_secret: str = ""   # optional (public clients need none); blank keeps saved


class OneDriveConnectRequest(BaseModel):
    device_code:   str = ""   # from POST /settings/onedrive/device-code
    refresh_token: str = ""   # or paste a refresh token directly (advanced)


@app.get("/settings/onedrive")
async def get_onedrive(request: Request):
    guard = _gdrive_admin_or_403(request)   # same policy: OAuth token ⇒ admin-only in MU
    if guard is not None:
        return guard
    cfg = _onedrive_config()
    out = cfg.to_public_dict()
    out["client_secret_set"] = bool(_onedrive_client_secret())
    out["connected"] = bool(_onedrive_token())
    out["configured"] = bool(cfg.folder_path and cfg.client_id and _onedrive_token())
    out["multiuser"] = multiuser.ENABLED
    out["consent_manage_url"] = onedrive_intake.CONSENT_MANAGE_URL
    return JSONResponse(out)


@app.post("/settings/onedrive")
async def set_onedrive(body: OneDriveSettingsRequest, request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    try:
        cfg = _load_config()
        block = cfg.get("onedrive") or {}
        scope = body.scope.strip().lower()
        if scope not in ("files.read", "files.readwrite"):
            scope = "files.read"
        block.update({
            "enabled":       bool(body.enabled),
            "folder_path":   body.folder_path.strip().strip("/"),
            "poll_interval": max(30, int(body.poll_interval or 300)),
            "scope":         scope,
            "client_id":     body.client_id.strip(),
            "tenant":        onedrive_intake._safe_tenant(body.tenant),
        })
        block.pop("client_secret", None)   # never keep the secret in the synced config
        block.pop("token", None)
        if body.client_secret:             # blank keeps the previously saved secret
            app_secrets.save_secret("onedrive_client_secret", body.client_secret)
        cfg["onedrive"] = block
        _save_config(cfg)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/settings/onedrive/device-code")
async def onedrive_device_code(request: Request):
    """Start the device-code sign-in: returns the code + URL to show the user.
    The UI then polls /settings/onedrive/connect with the device_code."""
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _onedrive_config()
    if not cfg.client_id:
        return JSONResponse({"ok": False, "error": "Save the Azure application (client) ID first."},
                            status_code=400)
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: onedrive_intake.device_code_start(cfg))
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/settings/onedrive/connect")
async def onedrive_connect(body: OneDriveConnectRequest, request: Request):
    """Finish connecting: poll the device-code once (the UI retries while `pending`)
    or accept a directly pasted refresh token (advanced)."""
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _onedrive_config()
    try:
        if body.refresh_token.strip():
            token = body.refresh_token.strip()
        elif body.device_code.strip():
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: onedrive_intake.device_code_poll(cfg, body.device_code.strip()))
            if not result.get("ok"):
                status = 200 if result.get("pending") else 400
                return JSONResponse({"ok": False, "pending": bool(result.get("pending")),
                                     "error": result.get("error", "")}, status_code=status)
            token = result["refresh_token"]
        else:
            return JSONResponse({"ok": False, "error": "Provide a device code or a refresh token."},
                                status_code=400)
        app_secrets.save_secret("onedrive_token", token)
        return JSONResponse({"ok": True, "connected": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/settings/onedrive/disconnect")
async def onedrive_disconnect(request: Request):
    """Clear the stored token. Microsoft has no programmatic revoke for consumer
    refresh tokens — the response points at the account consent-manage page."""
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    app_secrets.save_secret("onedrive_token", "")
    return JSONResponse({"ok": True, "connected": False,
                         "revoke_url": onedrive_intake.CONSENT_MANAGE_URL})


@app.post("/settings/onedrive/test")
async def test_onedrive(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _onedrive_config()

    def _run():
        graph = _build_onedrive_graph()
        return onedrive_intake.test_connection(graph, cfg)
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/settings/onedrive/poll-now")
async def poll_onedrive_now(request: Request):
    guard = _gdrive_admin_or_403(request)
    if guard is not None:
        return guard
    cfg = _onedrive_config()
    if not (cfg.folder_path and _onedrive_token()):
        return JSONResponse({"ok": False, "error": "Connect Microsoft and set the inbox folder path first."},
                            status_code=400)
    seen = _load_onedrive_seen()

    def _run():
        return _onedrive_poll(cfg, seen)
    try:
        summary = await asyncio.get_event_loop().run_in_executor(None, _run)
        if summary.get("downloaded"):
            _ensure_worker_alive()
        return JSONResponse({"ok": True, **summary})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


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


@app.post("/debug/autocrop-test")
async def autocrop_test(files: list[UploadFile] = File(...)):
    """Preview the auto-crop on an uploaded image — before/after dims, the
    decision (and why), and a JPEG preview of the result. Lets a user confirm
    auto-crop behaves on their receipts without running a whole batch."""
    if not files:
        return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)

    content = await files[0].read()
    if not content:
        return JSONResponse({"ok": False, "error": "Empty file"}, status_code=400)

    def _run() -> dict:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(content)) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            img = raw.convert("RGB")
            ow, oh = img.size
            info    = _pr.autocrop_analyze(img)        # what it would do + why
            cropped = _pr.autocrop_receipt(img)         # what the pipeline does
            cw, ch  = cropped.size
            buf = io.BytesIO()
            cropped.save(buf, format="JPEG", quality=85, optimize=True)
        return {
            "ok":         True,
            "enabled":    _pr.AUTOCROP_ENABLED,
            "cropped":    (cw, ch) != (ow, oh),
            "would_crop": bool(info["would_crop"]),
            "original":   [ow, oh],
            "result":     [cw, ch],
            "kept_ratio": round(float(info["kept_ratio"]), 4),
            "reason":     info["reason"],
            "preview":    "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        }

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/debug/process-test")
async def process_test(files: list[UploadFile] = File(...)):
    """Run every enabled image-processing step on an uploaded image, in the exact
    order the pipeline uses (auto-rotate → black&white → auto-crop → compress),
    and return a per-step before/after report plus the final image. Confirms the
    steps compose — e.g. a sideways photo comes out upright *and* cropped — and
    lets a user dial in auto-crop aggressiveness against a real receipt."""
    if not files:
        return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)
    content = await files[0].read()
    if not content:
        return JSONResponse({"ok": False, "error": "Empty file"}, status_code=400)
    suffix = Path(files[0].filename or "test.jpg").suffix or ".jpg"

    def _run() -> dict:
        from PIL import Image as PILImage
        PROCESSING_FOLDER.mkdir(parents=True, exist_ok=True)
        cur = PROCESSING_FOLDER / f"_proc_test_{uuid4().hex[:8]}{suffix}"
        cleanup = {cur}
        try:
            cur.write_bytes(content)

            def _dims(p: Path) -> list:
                with PILImage.open(p) as im:
                    return list(im.size)

            start_dims, start_bytes = _dims(cur), cur.stat().st_size
            steps = []

            # The image transforms, applied in series exactly as the pipeline does.
            for label, enabled, fn in (
                ("Auto-rotate to upright", _pr.AUTOROTATE_ENABLED, _pr.autorotate_image_file),
                ("Black & white",          _pr.GRAYSCALE_ENABLED,  _pr.grayscale_image_file),
                ("Auto-crop borders",      _pr.AUTOCROP_ENABLED,   _pr.autocrop_image_file),
            ):
                before = _dims(cur)
                applied = bool(fn(cur))
                steps.append({"step": label, "enabled": bool(enabled),
                              "applied": applied, "before": before, "after": _dims(cur)})

            # Compress is an export-time step that may rewrite the file to .jpg.
            before, before_bytes = _dims(cur), cur.stat().st_size
            new = _pr.compress_image_file(cur)
            cleanup.add(new)
            after, after_bytes = _dims(new), new.stat().st_size
            steps.append({
                "step": "Compress stored image", "enabled": bool(_pr.COMPRESS_ENABLED),
                "applied": bool(_pr.COMPRESS_ENABLED and (str(new) != str(cur)
                            or after_bytes < before_bytes or after != before)),
                "before": before, "after": after,
                "before_bytes": before_bytes, "after_bytes": after_bytes,
            })
            cur = new

            with PILImage.open(cur) as fim:
                buf = io.BytesIO()
                fim.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            return {
                "ok": True,
                "aggressiveness": _pr.AUTOCROP_AGGRESSIVENESS,
                "steps": steps,
                "original": start_dims, "original_bytes": start_bytes,
                "result": _dims(cur), "result_bytes": cur.stat().st_size,
                "preview": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            }
        finally:
            for p in cleanup:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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

    archive_root = ARCHIVE_FOLDER.resolve()

    def _scan(folder: Path, label: str) -> None:
        nonlocal scanned
        if not folder.exists():
            return
        for p in sorted(folder.rglob("*")):
            # Archived receipts the user chose to keep are intentional, not orphans.
            try:
                if p.resolve() == archive_root or archive_root in p.resolve().parents:
                    continue
            except OSError:
                pass
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
    """Email the reimbursement report ("Send Report Now").

    Emails the workbook from the most recent Generate (kept in memory) so it sends
    the exact file the user just produced. If no report has been generated yet, one
    is built on the fly from the live web-UI results. This deliberately does NOT use
    the separate watch-mode state file (the old behaviour, which read an unrelated,
    usually-empty store and failed with "no receipts in state — nothing to build").
    """
    from watch_mode import load_email_config, send_workbook_email

    # Check email is configured FIRST so the UI can guide the user to set up Gmail
    # instead of showing a cryptic failure.
    cfg = load_email_config()
    if not all([cfg["host"], cfg["user"], cfg["pass"], cfg["to"]]):
        return JSONResponse(
            {"ok": False, "needs_email_setup": True,
             "error": "Email isn't set up yet. Add your SMTP account in "
                      "Settings → Email Delivery (for Gmail, create an App Password)."},
            status_code=400,
        )

    global _last_report_path, _last_report_count
    path = _last_report_path
    count = _last_report_count
    # Receipts built on the fly here (cached reports were already recorded at
    # generate time); recorded into the sent-ledger only after the send succeeds.
    built_results: list | None = None

    # No workbook kept (e.g. the app restarted, or the user never clicked Generate)
    # — build one now from the live completed results so Send Report Now still works.
    if not path or not Path(path).exists():
        with _results_lock:
            results_copy = copy.deepcopy(_results)
        # Drop receipts already sent in a prior report (unless forced back in).
        results_copy = [r for r in results_copy
                        if not r.get("_already_sent") or r.get("_force_included")]
        if not results_copy:
            return JSONResponse(
                {"ok": False,
                 "error": "No report to send yet — process some receipts and click "
                          "Generate Spreadsheet first."},
                status_code=404,
            )
        employee = (
            _last_context.get("employee")
            or _load_config().get("default_employee")
            or "Employee"
        )
        try:
            _detect_duplicates(results_copy)
            per_diem = _per_diem_config()
            built = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: generate_spreadsheet(
                    results=results_copy, output_dir=OUT_FOLDER,
                    employee_name=employee, per_diem=per_diem,
                ),
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"Report generation failed: {exc}"},
                status_code=500,
            )
        if not built or not Path(built).exists():
            return JSONResponse(
                {"ok": False, "error": "Report generation failed."}, status_code=500
            )
        path = _last_report_path = str(built)
        count = _last_report_count = len(results_copy)
        built_results = results_copy

    # Per-report context for the templated subject/body (sender stays shared).
    with _results_lock:
        _ctx_total = 0.0
        for r in _results:
            try:
                _ctx_total += float(r.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        email_ctx = {
            "employee":   _last_context.get("employee", ""),
            "job_name":   _last_context.get("job_name", ""),
            "job_number": _last_context.get("job_number", ""),
            "total":      round(_ctx_total, 2),
            "count":      count or 0,
        }
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: send_workbook_email(Path(path), count or 0, email_ctx)
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    # On a successful send of a freshly-built report, record its receipts so they
    # aren't re-sent in a future report.
    if result.get("ok") and built_results is not None:
        _record_sent(built_results, Path(path).name)
        _persist_state()
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)
