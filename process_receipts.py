#!/usr/bin/env python3
"""
process_receipts.py  —  Core receipt-processing logic.

Public API:
  initialize_models()          — check LM Studio connectivity
  process_receipts_batch(...)  — full pipeline, returns a summary dict
  extract_receipt_data(...)    — send one image to LM Studio, get structured dict
  generate_spreadsheet(...)    — build the Excel workbook from processed results
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
import concurrent.futures
import threading
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

from openai import OpenAI
from PIL import Image, ImageChops, ImageOps

from spreadsheet_theme import build_themed_workbook

# ── Configuration ──────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL    = os.getenv("LMSTUDIO_BASE_URL",    "http://127.0.0.1:1234/v1")
OLMOCR_MODEL_ID      = os.getenv("OLMOCR_MODEL_ID",      "")
GEMMA_SMALL_MODEL_ID = os.getenv("GEMMA_SMALL_MODEL_ID", "")
GEMMA_LARGE_MODEL_ID = os.getenv("GEMMA_LARGE_MODEL_ID", "")
GEMMA_MODEL_ID       = os.getenv("GEMMA_MODEL_ID",       "")

# Build tag — surfaced in the web UI footer and the workbook footer so you can
# confirm which build is actually running (handy after a `docker compose up`
# that may have reused a stale image). Override at build time with BUILD_TAG.
APP_VERSION = os.getenv("BUILD_TAG", "2026.06.11")
# Concurrency cap for the batch worker. The local LM Studio model is the
# bottleneck and serves one model instance: flooding it with the ThreadPoolExecutor
# default (~min(32, cpu+4)) does not speed anything up and routinely pushes
# per-request latency past LLM_TIMEOUT, so receipts silently fall back to the
# lower-accuracy offline parser. A small fixed pool overlaps one receipt's OCR
# (CPU) with another's LLM call without VRAM thrash. Raise only when LM Studio is
# configured for true parallelism with VRAM headroom. 0 = no cap (legacy default).
MAX_PARALLEL_REQUESTS = int(os.getenv("MAX_PARALLEL_REQUESTS", "3"))

# Optional, user-configurable audit warnings — all OFF by default (None = no
# warning). Set from Settings → "Spending & date warnings" and applied
# deterministically in Python (audit_warning_flags), NOT by the LLM, so behaviour
# is consistent and there are no warnings at all unless the user opts in.
AMOUNT_LIMITS = {"fuel": None, "mats": None, "misc": None}   # per-category $ caps
MAX_RECEIPT_AGE_DAYS = None                                  # flag receipts older than N days
# Per-request timeout (seconds) for the LM Studio / OpenAI client. Without it a
# hung model request blocks a worker thread forever; bounded retries cover
# transient drops. Override via LLM_TIMEOUT.
LLM_TIMEOUT          = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_MAX_RETRIES      = int(os.getenv("LLM_MAX_RETRIES", "2"))
RECEIPTS_FOLDER      = os.getenv("RECEIPTS_FOLDER", "receipts")
OUTPUT_FOLDER        = os.getenv("OUTPUT_FOLDER",   "output")

# ── Single authoritative app-config location ────────────────────────────────────
# The web server, the watch daemon, and the scheduler ALL read and write this one
# file. It is defined here, in the shared module, so there is exactly one source of
# truth — previously server.py and watch_mode.py each recomputed their own path,
# which let a Docker-internal copy and a host-output copy drift apart. It lives
# under the mounted OUTPUT_FOLDER so settings survive container rebuilds.
APP_CONFIG_FILENAME = ".app_config.json"
CONFIG_FILE         = Path(OUTPUT_FOLDER) / APP_CONFIG_FILENAME

IMAGE_EXTENSIONS     = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PDF_EXTENSIONS       = {".pdf"}
ARCHIVE_EXTENSIONS   = {".zip"}
# Archives are expanded into their member images/PDFs at intake; the members
# (not the archive) are what gets queued, so SUPPORTED_EXTENSIONS — the set the
# pipeline treats as directly processable — deliberately stays images + PDFs.
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS
IMAGE_MAX_PX         = 1568
# Hard cap on pages rendered from a single PDF — a huge or maliciously-crafted
# PDF could otherwise exhaust disk by expanding to thousands of JPEGs. Override
# via PDF_MAX_PAGES.
PDF_MAX_PAGES        = int(os.getenv("PDF_MAX_PAGES", "50"))

# Runtime state — a SINGLE model now drives both stages (consolidated).
# _active_distill_model: the one active model, used for unified extraction + audit
#                        and (when LLM OCR is enabled) transcription too.
# _active_ocr_model:     mirrors the active model when LLM OCR is on; empty = the
#                        dedicated LLM OCR pass is skipped (built-in RapidOCR only).
# _llm_ocr_enabled:      when True the active model also transcribes the receipt and
#                        its reading is cross-referenced with RapidOCR. Off by
#                        default — it doubles the per-receipt model calls. There is
#                        no separate OCR model: OCR and distillation share one model.
_active_ocr_model:    str = ""           # set in lock-step with the active model
_active_distill_model: str = ""           # the single active model — set by initialize_models()
_llm_ocr_enabled:     bool = False        # active model also does OCR when True

# Tiny throwaway "receipt" used to warm the model into memory at startup.
_WARMUP_OCR_TEXT = "QUICK MART\n1 Main St\nCoffee 1.00\nTOTAL $1.00\n01/01/2025"

# Reasoning ("thinking") mode is applied per stage, on purpose:
#   • OCR / transcription  → reasoning is ALWAYS off. Transcribing visible text
#     verbatim never benefits from chain-of-thought and only runs slower.
#   • Distillation / vision extraction → reasoning follows the UI toggle below
#     and is ON by default. Turning raw OCR text into clean structured fields,
#     reconciling totals and catching anomalies is where reasoning helps.
# The UI toggle drives _thinking_enabled (distillation only); OCR ignores it.
_thinking_enabled: bool = True


def _thinking_body(budget: int, *, enabled: Optional[bool] = None) -> dict:
    """LM Studio extra_body fragment for reasoning mode.

    Pass ``enabled=False`` to force reasoning off for a stage (the OCR pass does
    this); leave it ``None`` to follow the user's distillation toggle.
    """
    on = _thinking_enabled if enabled is None else enabled
    if on:
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"thinking": {"type": "disabled"}}


# Placeholder job fields. When the user supplies no job name / number for a batch,
# every receipt is stamped with these literal strings instead of being left blank
# — so the value is visible in the generated spreadsheet and the user can Ctrl+F
# find-and-replace it across the sheet in one pass.
DEFAULT_JOB_NAME   = "Default Job Name"
DEFAULT_JOB_NUMBER = "Default Job Number"

# Brand / keyword sets and the known-vendor database live in vendor_db so the
# offline parser can name a real vendor (not the store address) and so the lists
# have a single home. The category-scoring patterns below are built from them.
from vendor_db import FUEL_VENDORS, FUEL_KEYWORDS, MATS_VENDORS, match_vendor


def _kw_pattern(kw: str) -> "re.Pattern[str]":
    """Word-boundary regex for one vendor/keyword match against lowercased text.

    Plain substring matching misfired badly on raw OCR text: "76" matched street
    addresses, store numbers and any price ending in .76, and "gas" matched
    "Las Vegas".  Purely numeric brands ("76") additionally must not touch
    digits, decimal points, '#', '$' or ',' so prices, store numbers, addresses
    and zip codes never count as a fuel-vendor sighting.
    """
    esc = re.escape(kw)
    if kw.isdigit():
        return re.compile(rf"(?<![a-z0-9.,#$]){esc}(?![a-z0-9.,])")
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")


_FUEL_VENDOR_PATTERNS  = {kw: _kw_pattern(kw) for kw in FUEL_VENDORS}
_FUEL_KEYWORD_PATTERNS = {kw: _kw_pattern(kw) for kw in FUEL_KEYWORDS}
_MATS_VENDOR_PATTERNS  = {kw: _kw_pattern(kw) for kw in MATS_VENDORS}

MONTH_MAP: dict[str, int] = {
    "january": 1,   "february": 2,  "march": 3,
    "april": 4,     "may": 5,       "june": 6,
    "july": 7,      "august": 8,    "september": 9,
    "october": 10,  "november": 11, "december": 12,
    "jaunary": 1, "feburary": 2, "jan": 1, "feb": 2, "mar": 3,
    "apr": 4,     "jun": 6,     "jul": 7, "aug": 8, "sep": 9,
    "oct": 10,    "nov": 11,    "dec": 12,
}


# ── Prompts ────────────────────────────────────────────────────────────────────

OLMOCR_RAW_PROMPT = (
    "Transcribe ALL visible text from this receipt image. "
    "Include every word, number, date, total, and label exactly as shown. "
    "Output only the raw transcribed text — no JSON, no formatting, no commentary."
)

# Stage 2 unified distillation — extraction + audit in one call.
# job_name and job_number are intentionally omitted (user provides those manually).
_UNIFIED_DISTILLATION_TEMPLATE = (
    "You are a receipt data extractor and expense auditor. Parse the following raw OCR text "
    "from a receipt and return ONLY a JSON object:\n\n"
    "{{\n"
    '  "date": "YYYY-MM-DD",\n'
    '  "vendor": "store or vendor name",\n'
    '  "amount": 0.00,\n'
    '  "category": "fuel | mats | misc",\n'
    '  "expense_description": null,\n'
    '  "summary": "one-sentence description WITHOUT the dollar amount, e.g. \'Lunch at a restaurant\' or \'Fuel at a gas station\'",\n'
    '  "flags": []\n'
    "}}\n\n"
    "Category rules:\n"
    '- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, Valero, etc.)\n'
    '  → set expense_description = null\n'
    '- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies\n'
    '- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)\n\n'
    "Field rules:\n"
    "- You may be given more than one OCR transcription of the SAME receipt "
    "(labelled transcription A and B) from different engines — cross-reference "
    "them, prefer values that agree, and use the clearer reading where they differ\n"
    "- vendor: copy the store/business name exactly as printed on the receipt. "
    "If no vendor name is legible, return an empty string \"\" — NEVER guess, "
    "invent, or copy an example name\n"
    "- Use TOTAL or GRAND TOTAL for amount\n"
    "- date must be YYYY-MM-DD; ALWAYS read ambiguous numeric dates as US "
    "month/day order (08/15/24 → 2024-08-15) — never day/month\n"
    "- summary: one sentence, vendor and purpose only, do NOT include the dollar amount\n"
    "- Do NOT include job_name or job_number — user provides those manually\n"
    "- flags: JSON array of flag objects for OCR/extraction problems ONLY:\n"
    '  * amount=0, missing vendor, or garbled date → {{"flag": "OCR error: reason"}}\n'
    "  * Do NOT flag amounts for being high or dates for being old — the app "
    "handles those rules itself\n"
    "  * Return [] if no issues\n\n"
    "Return ONLY valid JSON — no markdown, no extra text.\n\n"
    "Receipt OCR text:\n{ocr_text}"
)

# Direct-vision fallback (same schema)
_GEMMA_VISION_TEMPLATE = """\
You are a receipt data extractor and expense auditor. Analyze this receipt image and return ONLY a JSON object:

{{
  "date": "YYYY-MM-DD",
  "vendor": "store or vendor name",
  "amount": 0.00,
  "category": "fuel | mats | misc",
  "expense_description": null,
  "summary": "one-sentence description WITHOUT the dollar amount, e.g. 'Lunch at a restaurant' or 'Fuel at a gas station'",
  "flags": [],
  "boxes": {{
    "vendor": {{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "confidence": 0}},
    "date":   {{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "confidence": 0}},
    "amount": {{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "confidence": 0}}
  }}
}}

Category rules:
- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, etc.) → expense_description=null
- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies
- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)

Vendor: copy the store/business name exactly as printed. If no vendor name is legible, return an empty string "" — never guess, invent, or copy an example name.
Amount: use TOTAL or GRAND TOTAL.
boxes: for vendor, date and amount, give WHERE that text sits on the image as fractions of the image size — x,y = top-left corner, w,h = width/height, all between 0 and 1 (0,0 = top-left of the image) — plus a confidence 0–100 for that location. If you cannot locate a field, set its confidence to 0.
Date: YYYY-MM-DD from transaction date; ALWAYS read ambiguous numeric dates as US month/day order (08/15/24 → 2024-08-15), never day/month.
Summary: vendor and purpose only — do NOT include the dollar amount.
Do NOT include job_name or job_number.

flags (OCR/extraction problems ONLY):
- amount=0, missing vendor, garbled date → {{"flag": "OCR error: reason"}}
- Do NOT flag amounts for being high or dates for being old — the app handles those rules itself.
Return [] if no issues.

Return ONLY valid JSON, no markdown."""


# ── Model management ───────────────────────────────────────────────────────────

def _api_base() -> str:
    return LMSTUDIO_BASE_URL.rstrip("/").removesuffix("/v1")


def _fuzzy_match(model_id: str, loaded_ids: list[str]) -> bool:
    key = re.sub(r"[-_/]", "", model_id.lower())
    for mid in loaded_ids:
        if key in re.sub(r"[-_/]", "", mid.lower()):
            return True
    return False


def _try_load_model(model_id: str) -> bool:
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models", headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            loaded = [m["id"] for m in data.get("data", [])]
            if _fuzzy_match(model_id, loaded):
                return True
    except Exception:
        pass
    try:
        payload = json.dumps({"identifier": model_id}).encode()
        req = urllib.request.Request(
            f"{base}/api/v0/models/load", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_available_models() -> list[str]:
    """Return all model IDs currently loaded in LM Studio."""
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models", headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


def _looks_like_chat_model(model_id: str) -> bool:
    """Heuristic: exclude embedding / audio / reranker models from auto-selection."""
    low = model_id.lower()
    return not any(tag in low for tag in
                   ("embed", "bge-", "rerank", "whisper", "tts", "clip", "vae"))


def set_active_model(model_id: str) -> str:
    """Select the single AI model used for both distillation and (optional) OCR.

    OCR shares the one model, so the OCR alias is kept in lock-step: it points at
    the active model when LLM OCR is enabled, and is cleared otherwise.
    """
    global _active_distill_model, _active_ocr_model
    _active_distill_model = (model_id or "").strip()
    _active_ocr_model = _active_distill_model if _llm_ocr_enabled else ""
    return _active_distill_model


def set_llm_ocr(enabled: bool) -> bool:
    """Toggle whether the single active model also transcribes the receipt (OCR)."""
    global _active_ocr_model, _llm_ocr_enabled
    _llm_ocr_enabled = bool(enabled)
    _active_ocr_model = _active_distill_model if _llm_ocr_enabled else ""
    return _llm_ocr_enabled


def _make_client() -> OpenAI:
    """Build an LM Studio (OpenAI-compatible) client with the standard settings."""
    return OpenAI(
        base_url=LMSTUDIO_BASE_URL, api_key="lmstudio",
        timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES,
    )


def warm_up_model() -> bool:
    """Prime the active model into memory with a tiny dummy distillation.

    Best-effort, called once at startup after the model is selected/loaded. It
    sends a minimal fake receipt through the real distillation path so the
    weights are resident and the runtime is warm before the first real receipt —
    the first user batch then doesn't pay the cold-start latency. Any failure is
    swallowed; the app still works cold.
    """
    if not _active_distill_model:
        return False
    try:
        _unified_distillation(_make_client(), _WARMUP_OCR_TEXT, _retry=False)
        print(f"[warmup] Primed model into memory: {_active_distill_model}")
        return True
    except Exception as exc:
        print(f"[warmup] skipped: {exc}")
        return False


def initialize_models(warm: bool = True) -> str:
    """Check LM Studio connectivity, adopt a model, load it, and warm it up.

    If the configured model isn't actually loaded, fall back to a loaded Gemma if
    present, otherwise the first loaded chat-capable model — so the app works out
    of the box with whatever the user has running in LM Studio. The chosen model
    is then **auto-loaded** into memory and (when ``warm``) primed with a tiny
    dummy receipt so the first real batch is fast.
    """
    global _active_distill_model, _active_ocr_model
    print(f"[models] LLM endpoint: {LMSTUDIO_BASE_URL}")
    available = list_available_models()
    if available:
        print(f"[models] {len(available)} model(s) available: {available}")
        if not _active_distill_model or _active_distill_model not in available:
            chosen = (
                next((m for m in available if "gemma" in m.lower()), None)
                or next((m for m in available if _looks_like_chat_model(m)), None)
                or available[0]
            )
            _active_distill_model = chosen
            print(f"[models] Auto-selected loaded model: {chosen}")
        # Auto-load the active model into LM Studio memory so the first real
        # receipt doesn't pay the cold-load latency.
        if _active_distill_model and _try_load_model(_active_distill_model):
            print(f"[models] Auto-loaded into memory: {_active_distill_model}")
        # OCR shares the single active model when enabled.
        _active_ocr_model = _active_distill_model if _llm_ocr_enabled else ""
    else:
        print("[models] LM Studio not reachable or no models loaded")

    print(f"[models] OCR model: {_active_ocr_model or '(none — built-in OCR only)'}")
    print(f"[models] Distill model: {_active_distill_model}")
    if warm and available:
        warm_up_model()
    return _active_distill_model


# ── Cloud LLM providers (fallback chain) ─────────────────────────────────────────
#
# Extraction can fall back across several OpenAI-compatible endpoints, tried in
# order. The default chain is:
#
#     Gemini Flash-Lite (cloud)  →  Mistral (cloud)  →  local LM Studio
#
# A cloud provider is only tried when its API key is set (env var or the Settings
# UI). LM Studio is ALWAYS the final fallback, so a fully-local install keeps
# working with no keys at all. If every provider errors, callers fall through to
# the offline regex parser exactly as before — the chain only changes WHERE the
# model call goes, not what happens when there's no model.
#
# Cloud free tiers rate-limit aggressively; the OpenAI SDK already retries 429 /
# 5xx with exponential backoff honouring Retry-After (LLM_MAX_RETRIES). When a
# provider is still exhausted after its retries, the chain moves to the next one.

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "")
# Defaults are the smallest FREE, vision-capable model on each provider's free
# tier. Gemini 2.5 Flash-Lite is the cheapest/stable free Gemini with image input
# (highest free request/day allowance); Mistral Small is the smallest maintained
# free vision model (Pixtral 12B is deprecated). Override per provider as needed.
GEMINI_MODEL    = os.getenv("GEMINI_MODEL",    "gemini-2.5-flash-lite")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")

MISTRAL_API_KEY  = os.getenv("MISTRAL_API_KEY",  "")
MISTRAL_MODEL    = os.getenv("MISTRAL_MODEL",    "mistral-small-latest")
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")

# Ordered cloud providers tried before the local model. enabled=False or a blank
# api_key takes a provider out of the chain. Mutated by configure_providers().
_CLOUD_PROVIDERS: list[dict] = [
    {"name": "gemini",  "base_url": GEMINI_BASE_URL,  "api_key": GEMINI_API_KEY,
     "model": GEMINI_MODEL,  "enabled": True},
    {"name": "mistral", "base_url": MISTRAL_BASE_URL, "api_key": MISTRAL_API_KEY,
     "model": MISTRAL_MODEL, "enabled": True},
]

# Request params the cloud OpenAI-compatible endpoints understand. LM Studio
# extras (the thinking body + repeat_penalty carried in extra_body, and
# frequency_penalty) are dropped for cloud providers so they don't 400 on an
# unknown field.
_CLOUD_SAFE_PARAMS = {"messages", "temperature", "max_tokens", "response_format"}


def _active_cloud_providers() -> list[dict]:
    """Enabled cloud providers with a non-blank API key + model, in fallback order."""
    return [p for p in _CLOUD_PROVIDERS
            if p.get("enabled", True)
            and (p.get("api_key") or "").strip()
            and (p.get("model") or "").strip()]


def configure_providers(specs: list[dict]) -> None:
    """Replace cloud-provider settings at runtime (used by the Settings UI).

    Each spec is a dict with ``name`` plus any of base_url/api_key/model/enabled;
    fields left out keep their current value, and unknown provider names are
    ignored. Does not touch the local LM Studio fallback.
    """
    by_name = {p["name"]: p for p in _CLOUD_PROVIDERS}
    for spec in specs or []:
        name = (spec.get("name") or "").strip()
        cur = by_name.get(name)
        if cur is None:
            continue
        for key in ("base_url", "api_key", "model", "enabled"):
            if key in spec and spec[key] is not None:
                cur[key] = spec[key]


def provider_status() -> list[dict]:
    """Fallback chain as the UI sees it — no API keys, just whether each is live."""
    active = _active_cloud_providers()
    chain = [{
        "name":    p["name"],
        "model":   p.get("model", ""),
        "enabled": bool(p.get("enabled", True)),
        "has_key": bool((p.get("api_key") or "").strip()),
        "active":  p in active,
    } for p in _CLOUD_PROVIDERS]
    chain.append({"name": "lmstudio", "model": _active_distill_model or "",
                  "enabled": True, "has_key": True, "active": True})
    return chain


def active_provider_names() -> list[str]:
    """Ordered names of every provider that would actually be tried."""
    return [p["name"] for p in _active_cloud_providers()] + ["lmstudio"]


def _sanitize_create_kwargs(kind: str, kwargs: dict) -> dict:
    """Strip LM-Studio-only params before sending to a cloud provider."""
    if kind == "lmstudio":
        return kwargs
    return {k: v for k, v in kwargs.items() if k in _CLOUD_SAFE_PARAMS}


class _FallbackCompletions:
    """Mimics ``client.chat.completions`` across an ordered provider chain.

    ``create()`` keeps the exact signature the pipeline already uses
    (``model=…, messages=…, …``). For each provider in turn it substitutes that
    provider's own model id, sanitises the request, and returns the first
    success. The caller's ``model`` argument is used only for the local LM Studio
    provider (whose model is runtime-selected). If every provider errors, the
    last exception is re-raised so existing ``except Exception`` paths fall back
    to the offline parser unchanged.
    """

    def __init__(self, providers: list[dict]):
        self._providers = providers

    def create(self, **kwargs):
        requested_model = kwargs.pop("model", None)
        last_exc: Optional[Exception] = None
        attempted: list[str] = []
        for p in self._providers:
            model = p.get("model") or requested_model
            if not model:
                continue
            call_kwargs = _sanitize_create_kwargs(p["kind"], dict(kwargs))
            try:
                resp = p["client"].chat.completions.create(model=model, **call_kwargs)
                if attempted:
                    print(f"[llm] '{p['name']}' ({model}) succeeded after "
                          f"{', '.join(attempted)} failed")
                return resp
            except Exception as exc:  # noqa: BLE001 — fall through to the next provider
                last_exc = exc
                attempted.append(p["name"])
                print(f"[llm] provider '{p['name']}' ({model}) failed: "
                      f"{type(exc).__name__}: {exc}")
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No LLM providers configured")


class _FallbackChat:
    def __init__(self, providers: list[dict]):
        self.completions = _FallbackCompletions(providers)


class _FallbackClient:
    """Drop-in stand-in for an OpenAI client exposing only ``chat.completions``."""

    def __init__(self, providers: list[dict]):
        self.chat = _FallbackChat(providers)


def make_llm_client():
    """Build the extraction client.

    When any cloud provider is active, return the fallback chain
    (cloud… → LM Studio); otherwise return the plain local LM Studio client so a
    keyless install behaves byte-for-byte as before.
    """
    cloud = _active_cloud_providers()
    if not cloud:
        return _make_client()
    providers = [{
        "name": p["name"], "kind": "openai", "model": (p.get("model") or "").strip(),
        "client": OpenAI(base_url=p["base_url"], api_key=p["api_key"],
                         timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES),
    } for p in cloud]
    providers.append({"name": "lmstudio", "kind": "lmstudio", "model": None,
                      "client": _make_client()})
    print(f"[llm] extraction chain: {' → '.join(p['name'] for p in providers)}")
    return _FallbackClient(providers)


# ── Image encoding ─────────────────────────────────────────────────────────────
#
# CANONICAL PIPELINE ORDER:  greyscale → autocrop → OCR/text extraction → … → compress
#
#   0. greyscale — flatten the stored image to high-contrast grayscale BEFORE any
#                  OCR/LLM (grayscale_image_file), in place, so both the OCR engine
#                  and the vision model read the same cleaned-up file. No-op when
#                  GRAYSCALE_ENABLED is off.
#   1. autocrop  — trim uniform background borders (autocrop_image_file / the
#                  in-memory autocrop_receipt inside encode_image).
#   2. OCR/text  — run extraction (LM Studio vision/OCR or the RapidOCR fallback)
#                  against the autocropped, full-resolution image.
#   3. compress  — DEFERRED to spreadsheet-generation time (compress_result_images
#                  / generate_spreadsheet). Re-encoding/downscaling the stored
#                  file once, at export, keeps OCR reading the sharpest image and
#                  shrinks the output folder and the embedded workbook images in a
#                  single pass. Compression may REWRITE the file with a new suffix
#                  (.png/.jpeg → .jpg); compress_result_images updates the stored
#                  _image_path / _new_filename so later lookups still resolve.
#
# Keeping compression OUT of the per-receipt path means nothing downstream can be
# handed a stale pre-compress path (the old "[Errno 2] No such file or directory"
# bug); the file the worker stores is exactly the file OCR read.

AUTOCROP_ENABLED   = os.getenv("AUTOCROP_ENABLED", "1").lower() not in ("0", "false", "no")
# Single user-facing dial, 0 (gentle) … 100 (very aggressive). It drives every
# detection knob below so one slider moves the whole behaviour. The default leans
# aggressive on purpose: phone photos carry a lot of background, and a timid crop
# reads to the user as "it didn't do anything".
AUTOCROP_AGGRESSIVENESS = int(os.getenv("AUTOCROP_AGGRESSIVENESS", "85"))


def _autocrop_params(aggressiveness: float) -> dict:
    """Map the 0..100 aggressiveness dial onto the four detection knobs.

    Higher aggressiveness ⇒ trims closer (smaller re-added margin), accepts
    tighter crops (lower min-kept floor), fires on smaller borders (higher
    max-kept ceiling), and ignores fainter background gradients (higher content
    threshold).  At 0 it reproduces the old conservative behaviour; at 100 it
    will trim almost any detectable border.
    """
    a = max(0.0, min(1.0, aggressiveness / 100.0))
    return {
        "min_ratio": 0.50 - 0.47 * a,   # keep ≥50% … keep ≥3%
        "max_ratio": 0.92 + 0.079 * a,  # trim borders down to <0.1% of the frame
        "margin":    0.04 * (1.0 - a),  # 4% … 0% safety margin re-added
        "threshold": 16 + 30 * a,       # 16 … 46 min grayscale delta from bg
    }

# Stored-image compression — re-encode every saved receipt to an optimized JPEG
# so phone photos don't bloat the output folder or the embedded workbook images.
# All three are runtime-adjustable from the web UI (Settings → Image processing).
COMPRESS_ENABLED = os.getenv("COMPRESS_ENABLED", "1").lower() not in ("0", "false", "no")
JPEG_QUALITY     = int(os.getenv("JPEG_QUALITY", "85"))    # 40 (smaller) … 95 (sharper)
STORE_MAX_PX     = int(os.getenv("STORE_MAX_PX", "2000"))  # cap the longest side of stored images

# Greyscale (black-&-white) pre-pass — convert receipts to high-contrast grayscale
# BEFORE OCR/LLM. Phone photos of receipts carry colour tints, shadows and uneven
# lighting that hurt OCR; flattening to autocontrasted grayscale gives both the OCR
# engine and the vision model a cleaner image to read. Applied in place so every
# later step (OCR, distillation, rename, compression, the embedded workbook image,
# the web preview) finds the same converted file — no extra path to track.
GRAYSCALE_ENABLED = os.getenv("GRAYSCALE_ENABLED", "1").lower() not in ("0", "false", "no")

# Auto-rotate — get the receipt's text facing the right way up BEFORE OCR. Two
# tiers, both rules-based (no model):
#   • EXIF: bake a phone photo's stored Orientation tag into the pixels, so the OCR
#     engine (which reads raw pixels and ignores EXIF) and the browser agree on
#     which way is up — also keeps the field-markup boxes aligned with the preview.
#   • OCR-guided: when the upright read is weak, try the three 90° rotations and keep
#     whichever RapidOCR reads best — catches scans/photos with no orientation tag,
#     including upside-down (180°) shots. Bounded: only fires on a weak upright read.
AUTOROTATE_ENABLED   = os.getenv("AUTOROTATE_ENABLED", "1").lower() not in ("0", "false", "no")
ORIENT_BY_OCR        = os.getenv("ORIENT_BY_OCR", "1").lower() not in ("0", "false", "no")
ORIENT_MIN_SCORE     = float(os.getenv("ORIENT_MIN_SCORE", "30"))      # upright read this strong → skip the search
ORIENT_IMPROVE_RATIO = float(os.getenv("ORIENT_IMPROVE_RATIO", "1.2")) # a rotation must beat upright by 20% to win


def _content_bbox_by_edges(gray: Image.Image, frac: float):
    """Detect the receipt content box via an edge-energy projection (numpy).

    Far more robust than corner-background subtraction on real photos: it doesn't
    assume the corners are background, so it survives gradients, shadows, busy
    desktops and coloured surfaces. Edge magnitude is summed into per-row and
    per-column profiles; the content extent is where each profile rises a
    fraction ``frac`` of the way from its background (median) to its peak. Returns
    a raw content bbox ``(left, top, right, bottom)`` or ``None``. Raises
    ``ImportError`` when numpy is unavailable so the caller can fall back.
    """
    import numpy as np  # local import: only needed for crop, keeps startup light

    arr = np.asarray(gray, dtype=np.float32)
    h, w = arr.shape
    gmag = np.zeros((h, w), dtype=np.float32)
    gmag[:, 1:] += np.abs(arr[:, 1:] - arr[:, :-1])   # horizontal gradient (vertical edges)
    gmag[1:, :] += np.abs(arr[1:, :] - arr[:-1, :])   # vertical gradient (horizontal edges)

    def _smooth(p, k):
        if k <= 1:
            return p
        return np.convolve(p, np.ones(k, dtype=np.float32) / k, mode="same")

    col_prof = _smooth(gmag.sum(axis=0), max(1, w // 200))
    row_prof = _smooth(gmag.sum(axis=1), max(1, h // 200))

    def _bounds(prof):
        peak = float(prof.max())
        if peak <= 0:
            return None
        base = float(np.median(prof))
        thr = base + frac * (peak - base)
        idx = np.where(prof >= thr)[0]
        if idx.size == 0:
            return None
        return int(idx[0]), int(idx[-1] + 1)

    cb, rb = _bounds(col_prof), _bounds(row_prof)
    if not cb or not rb:
        return None
    return (cb[0], rb[0], cb[1], rb[1])


def _content_bbox_by_corner_bg(gray: Image.Image, threshold: float):
    """Legacy corner-background content detection (fallback when numpy is absent).

    Estimates the background from the four corner patches and returns the bbox of
    everything that differs from it by more than ``threshold``. Returns a raw
    content bbox or ``None``.
    """
    w, h = gray.size
    samples = []
    for box in ((0, 0, 8, 8), (w - 8, 0, w, 8),
                (0, h - 8, 8, h), (w - 8, h - 8, w, h)):
        samples.extend(gray.crop(box).tobytes())
    samples.sort()
    bg = samples[len(samples) // 2]
    diff = ImageChops.difference(gray, Image.new("L", gray.size, bg))
    mask = diff.point(lambda v: 255 if v > threshold else 0)
    return mask.getbbox()


def autocrop_analyze(img: Image.Image, aggressiveness: Optional[float] = None) -> dict:
    """Inspect what auto-crop would do to ``img`` without mutating it.

    Returns a diagnostics dict — ``{"bbox", "kept_ratio", "would_crop",
    "reason"}`` — that is the single source of truth for both the pipeline
    (``autocrop_receipt``) and the Settings → "Test image processing" preview.
    ``bbox`` is the detected content box *with* the safety margin (or None),
    ``kept_ratio`` is its area as a fraction of the original, and ``reason`` is a
    short, human-readable explanation of the decision.  ``aggressiveness``
    defaults to the module-level ``AUTOCROP_AGGRESSIVENESS`` dial.

    Detection uses an edge-energy projection (robust to non-uniform backgrounds);
    it falls back to legacy corner-background subtraction only if numpy is
    unavailable. The aggressiveness dial, margin and accept/reject gating are
    unchanged so the slider behaves predictably.
    """
    if aggressiveness is None:
        aggressiveness = AUTOCROP_AGGRESSIVENESS
    p = _autocrop_params(aggressiveness)
    min_ratio, max_ratio = p["min_ratio"], p["max_ratio"]
    try:
        gray = img.convert("L")
        w, h = gray.size
        if w < 64 or h < 64:
            return {"bbox": None, "kept_ratio": 1.0, "would_crop": False,
                    "reason": "image too small to crop (min 64×64 px)"}
        try:
            # `threshold` (16..46) reused as a 0.16..0.46 energy fraction: higher
            # aggressiveness ⇒ larger fraction ⇒ fainter edges ignored ⇒ tighter box.
            bbox = _content_bbox_by_edges(gray, p["threshold"] / 100.0)
        except ImportError:
            bbox = _content_bbox_by_corner_bg(gray, p["threshold"])
        if not bbox:
            return {"bbox": None, "kept_ratio": 1.0, "would_crop": False,
                    "reason": "no content edges stand out from the background"}

        mx = int(w * p["margin"])
        my = int(h * p["margin"])
        left   = max(0, bbox[0] - mx)
        top    = max(0, bbox[1] - my)
        right  = min(w, bbox[2] + mx)
        bottom = min(h, bbox[3] + my)
        kept = ((right - left) * (bottom - top)) / float(w * h)
        margined = (left, top, right, bottom)

        # Always crop if bbox is smaller than the original (any non-trivial border found).
        if kept >= 1.0 or margined == (0, 0, w, h):
            return {"bbox": margined, "kept_ratio": kept, "would_crop": False,
                    "reason": "no meaningful border detected — image fills the frame"}
        return {"bbox": margined, "kept_ratio": kept, "would_crop": True,
                "reason": f"trims background border to {kept:.0%} of the original"}
    except Exception as exc:
        return {"bbox": None, "kept_ratio": 1.0, "would_crop": False,
                "reason": f"detection error: {exc}"}


def autocrop_receipt(img: Image.Image) -> Image.Image:
    """Trim uniform background borders around a receipt photo.

    Conservative by design: returns the original image unchanged whenever the
    detected crop is suspiciously aggressive (<40% of the area kept), trims
    almost nothing, or detection fails for any reason.  All of that logic lives
    in ``autocrop_analyze``; this is the in-memory apply step.
    """
    if not AUTOCROP_ENABLED:
        return img
    info = autocrop_analyze(img)
    if info["would_crop"] and info["bbox"]:
        return img.crop(info["bbox"])
    return img


def autocrop_image_file(path: Path) -> bool:
    """Auto-crop a stored receipt image in place. Returns True if cropped."""
    try:
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            img = raw.convert("RGB")
            cropped = autocrop_receipt(img)
            if cropped.size == img.size:
                return False
            if path.suffix.lower() in (".jpg", ".jpeg"):
                cropped.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            else:
                cropped.save(path)
        return True
    except Exception:
        return False


def grayscale_image_file(path: Path) -> bool:
    """Convert a stored receipt image to high-contrast grayscale, in place.

    A pre-OCR pass: phone receipts are often tinted, shadowed, or low-contrast,
    which trips up both the OCR engine and the vision model.  Converting to an
    autocontrasted single channel sharpens the text without the harsh artefacts of
    a hard 1-bit threshold (which would also hurt the embedded receipt image).

    The file keeps its original path and suffix, so every later step — OCR,
    distillation, autocrop, rename, deferred compression, the workbook image, and
    the web preview — still finds it exactly where it was.  Returns True when the
    file was rewritten, False when disabled or on any error (best-effort: a failed
    conversion must never block extraction).
    """
    if not GRAYSCALE_ENABLED:
        return False
    try:
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            fmt  = (raw.format or "").upper()
            gray = ImageOps.autocontrast(raw.convert("L"), cutoff=1)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            gray.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        elif fmt:
            gray.save(path, format=fmt)
        else:
            gray.save(path)
        return True
    except Exception:
        return False


def _save_image_inplace(img: "Image.Image", path: Path, fmt: str = "") -> None:
    """Save an image over ``path``, honoring JPEG_QUALITY and keeping the original
    format where possible (mirrors the save behavior of grayscale/autocrop)."""
    if path.suffix.lower() in (".jpg", ".jpeg"):
        img.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    elif fmt:
        img.save(path, format=fmt)
    else:
        img.save(path)


def _exif_orientation(img: "Image.Image") -> int:
    """The EXIF Orientation tag (1 = normal) for an open image, 1 on absence/error."""
    try:
        return int(img.getexif().get(0x0112, 1) or 1)
    except Exception:
        return 1


def autorotate_image_file(path: Path) -> bool:
    """Bake the photo's EXIF orientation into the pixels, in place, BEFORE OCR.

    Phone cameras store a sideways/upside-down shot upright-on-screen via an EXIF
    Orientation tag. The OCR engine reads raw pixels and ignores that tag, so it
    would transcribe rotated text while the browser shows the receipt upright —
    hurting OCR and knocking the field-markup boxes out of alignment. Applying the
    rotation to the pixels (and dropping the tag) makes the stored file, the OCR
    engine, and the browser agree on which way is up. No-op when the orientation is
    already normal, when disabled, or on any error (best-effort)."""
    if not AUTOROTATE_ENABLED:
        return False
    try:
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            if _exif_orientation(raw) in (0, 1):
                return False                       # already upright — don't recompress
            fmt = (raw.format or "").upper()
            fixed = ImageOps.exif_transpose(raw)   # applies the rotation and strips the tag
        _save_image_inplace(fixed, path, fmt)
        return True
    except Exception:
        return False


def compress_image_file(path: Path) -> Path:
    """Re-encode a stored receipt image as an optimized JPEG to shrink its size.

    Honors the runtime JPEG_QUALITY / STORE_MAX_PX settings, downscales oversized
    photos, and converts non-JPEG formats (PNG, HEIC-as-PNG, etc.) to JPEG. Returns
    the path of the resulting file — which may carry a new ``.jpg`` suffix — or the
    original path unchanged when compression is disabled or anything goes wrong.
    """
    if not COMPRESS_ENABLED:
        return path
    try:
        target = path.with_suffix(".jpg")
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            img = raw.convert("RGB")
            if max(img.size) > STORE_MAX_PX:
                ratio = STORE_MAX_PX / max(img.size)
                img = img.resize(
                    (round(img.width * ratio), round(img.height * ratio)), Image.LANCZOS,
                )
            img.save(target, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        if target != path and path.exists():
            path.unlink()
        return target
    except Exception:
        return path


def encode_image(path: Path) -> tuple[str, str]:
    raw = Image.open(path)
    if getattr(raw, "format", None) == "MPO":
        raw.seek(0)
    img = raw.convert("RGB")
    if max(img.size) > IMAGE_MAX_PX:
        ratio = IMAGE_MAX_PX / max(img.size)
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def pdf_to_images(pdf_path: Path, dest_dir: Path) -> list[Path]:
    """Convert each PDF page to a JPEG in dest_dir. Returns list of image paths."""
    if not HAS_PYMUPDF:
        print(f"[pdf] PyMuPDF not installed — skipping {pdf_path.name}")
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
        # Safety cap: never expand a single PDF to more than PDF_MAX_PAGES JPEGs,
        # so a huge or crafted PDF can't exhaust disk.
        page_count = len(doc)
        n_pages = min(page_count, PDF_MAX_PAGES)
        multi = n_pages > 1
        for i, page in enumerate(doc):
            if i >= PDF_MAX_PAGES:
                print(f"[pdf] {pdf_path.name}: {page_count} pages exceeds cap "
                      f"PDF_MAX_PAGES={PDF_MAX_PAGES} — skipping the remaining "
                      f"{page_count - PDF_MAX_PAGES} page(s)")
                break
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            suffix = f"_p{i + 1}" if multi else ""
            img_path = dest_dir / f"{pdf_path.stem}{suffix}.jpg"
            pix.save(str(img_path))
            out.append(img_path)
        doc.close()
    except Exception as exc:
        print(f"[pdf] Failed to convert {pdf_path.name}: {exc}")
    return out


# Zip-bomb / abuse guards for archive extraction.
ARCHIVE_MAX_FILES = int(os.getenv("ARCHIVE_MAX_FILES", "1000"))             # member cap
ARCHIVE_MAX_BYTES = int(os.getenv("ARCHIVE_MAX_BYTES", str(1024 ** 3)))     # 1 GiB uncompressed cap


class _ArchiveTooLarge(Exception):
    """Internal sentinel — decompressed output exceeded ARCHIVE_MAX_BYTES."""


def extract_archive(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Extract the supported image/PDF members of a .zip into dest_dir (flattened).

    Returns the list of extracted file paths (images and PDFs), ready to flow
    through the same queueing path as a directly-uploaded file.  Designed to be
    safe against hostile archives:

      * Zip-slip — every member is written under its *basename* only, so entries
        like ``../../etc/x`` or absolute paths can't escape dest_dir.
      * Zip-bomb — extraction stops once ARCHIVE_MAX_FILES members or
        ARCHIVE_MAX_BYTES of actual decompressed bytes have been written.
      * Junk — non-image/PDF members, directories, and dotfiles are skipped, so a
        zip's incidental README/.DS_Store never reaches the queue.

    Best-effort: a corrupt archive or a single bad member is logged and skipped,
    never raised, so one bad upload can't take down the request.
    """
    import zipfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if len(extracted) >= ARCHIVE_MAX_FILES:
                    print(f"[zip] {archive_path.name}: stopped at {ARCHIVE_MAX_FILES}-file cap")
                    break
                if info.is_dir():
                    continue
                name = os.path.basename(info.filename)          # flatten → zip-slip safe
                if not name or name.startswith("."):
                    continue
                if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue

                # Collision-safe target name within dest_dir.
                target = dest_dir / name
                stem, ext = Path(name).stem, Path(name).suffix
                i = 1
                while target.exists():
                    target = dest_dir / f"{stem}_{i}{ext}"
                    i += 1

                try:
                    with zf.open(info) as src, open(target, "wb") as dst:
                        while True:
                            if total_bytes >= ARCHIVE_MAX_BYTES:
                                raise _ArchiveTooLarge()
                            chunk = src.read(64 * 1024)
                            if not chunk:
                                break
                            total_bytes += len(chunk)
                            if total_bytes > ARCHIVE_MAX_BYTES:
                                raise _ArchiveTooLarge()
                            dst.write(chunk)
                except _ArchiveTooLarge:
                    target.unlink(missing_ok=True)
                    print(f"[zip] {archive_path.name}: stopped at {ARCHIVE_MAX_BYTES}-byte cap")
                    break
                except Exception as exc:
                    target.unlink(missing_ok=True)
                    print(f"[zip] {archive_path.name}: skipped member {name}: {exc}")
                    continue
                extracted.append(target)
    except zipfile.BadZipFile:
        print(f"[zip] {archive_path.name}: not a valid zip archive")
    except Exception as exc:
        print(f"[zip] Failed to extract {archive_path.name}: {exc}")
    return extracted


# ── AI extraction ──────────────────────────────────────────────────────────────

def _normalize_flags(flags) -> list[dict]:
    """Coerce flags to a list of {"flag": str} dicts.

    LLMs occasionally return flags as bare strings instead of the expected
    {"flag": "..."} dicts.  Normalise here so every downstream consumer can
    safely call .get("flag") without an AttributeError.
    """
    if not flags:
        return []
    result = []
    for f in flags:
        if isinstance(f, dict):
            result.append(f)
        elif f:
            result.append({"flag": str(f)})
    return result


def _is_low_confidence(data: Optional[dict]) -> bool:
    if data is None:
        return True
    if not data.get("amount"):
        return True
    if not (data.get("vendor") or "").strip():
        return True
    return False


def _has_ocr_flag(data: Optional[dict]) -> bool:
    """True if the distillation model flagged an OCR error in this receipt."""
    if not data:
        return False
    flags = _normalize_flags(data.get("flags") or [])
    return any("ocr error" in (f.get("flag") or "").lower() for f in flags)


def _compute_confidence(data: Optional[dict]) -> tuple[int, str]:
    """Return (0–100 pct, comma-separated missing-field string)."""
    if not data:
        return 0, "no data extracted"
    score = 100
    missing: list[str] = []
    if not (data.get("vendor") or "").strip():
        score -= 35; missing.append("vendor")
    if not data.get("amount"):
        score -= 35; missing.append("amount")
    if not data.get("date"):
        score -= 15; missing.append("date")
    if not data.get("category"):
        score -= 5; missing.append("category")
    for _ in data.get("flags") or []:
        score -= 5
    return max(0, min(100, score)), ", ".join(missing)


def _get_fail_reason(data: Optional[dict]) -> str:
    if data is None:
        return "Model returned no data"
    _, issues = _compute_confidence(data)
    if issues:
        return f"Could not extract: {issues}"
    return "Low-confidence extraction"


def _strip_json(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    return match.group(0) if match else raw


def _normalize_llm_boxes(raw) -> dict:
    """Normalize a vision model's optional field-location boxes.

    The model may report where it sees each field on the image, as fractions of
    image width/height: ``{field: {x, y, w, h, confidence}}`` with x,y = top-left
    corner and confidence 0–100. We return ``{field: [x, y, w, h, confidence]}``
    for vendor/date/amount, clamping coords to 0..1 and confidence to 0..100 and
    dropping anything malformed or zero-sized. These are advisory UI markers
    (drawn alongside the rules-based OCR boxes) — never load-bearing — so a bad
    box is silently ignored rather than raised.
    """
    if not isinstance(raw, dict):
        return {}

    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    out: dict[str, list] = {}
    for field in ("vendor", "date", "amount"):
        b = raw.get(field)
        if not isinstance(b, dict):
            continue
        try:
            x, y = float(b.get("x")), float(b.get("y"))
            w, h = float(b.get("w")), float(b.get("h"))
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        try:
            conf = float(b.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        out[field] = [
            round(_clamp(x, 0.0, 1.0), 4), round(_clamp(y, 0.0, 1.0), 4),
            round(_clamp(w, 0.0, 1.0), 4), round(_clamp(h, 0.0, 1.0), 4),
            round(_clamp(conf, 0.0, 100.0), 1),
        ]
    return out


def _parse_llm_record(raw: str) -> Optional[dict]:
    """Parse a model's JSON reply into a record dict, or None if it isn't one.

    Hardened against two distinct LLM failure modes:
      * text that isn't JSON at all (``JSONDecodeError``), and
      * text that is *valid* JSON but not an object — e.g. a bare ``null``,
        ``[]``, ``"oops"`` or a number.

    Either way we return None so the caller can retry or fall back to the
    offline parser, rather than crashing on ``result["flags"]`` / ``.get`` when
    ``result`` turns out not to be a dict.
    """
    try:
        result = json.loads(_strip_json(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    result["flags"] = _normalize_flags(result.get("flags") or [])
    # normalise "summary" field name to "ai_summary" used downstream
    if "summary" in result and "ai_summary" not in result:
        result["ai_summary"] = result.pop("summary")
    # Canonicalise the date deterministically (US month/day, 2-digit years → 20xx)
    # rather than trusting the model to have picked the right format. Keep the raw
    # value if it isn't parseable so nothing is silently lost.
    if result.get("date"):
        result["date"] = normalize_date(result["date"]) or result["date"]
    # Optional LLM-placed field boxes (vision path only). Lifted onto a private
    # key the UI overlays; the raw "boxes" key is dropped from the record.
    boxes = _normalize_llm_boxes(result.pop("boxes", None))
    if boxes:
        result["_llm_field_boxes"] = boxes
    return result


def _extract_raw_ocr(client: OpenAI, image_path: Path, model_id: str) -> Optional[str]:
    """Transcribe a receipt to raw text with an LM Studio OCR/vision model.

    Retained for callers that want an LLM-based OCR pass, but no longer part of
    the default pipeline — local RapidOCR (_extract_local_ocr) is the primary
    text source now.
    """
    try:
        b64, mime = encode_image(image_path)
        thinking_body = _thinking_body(4096, enabled=False)  # OCR never reasons
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": OLMOCR_RAW_PROMPT},
            ]}],
            temperature=0.0, max_tokens=2048,
            frequency_penalty=0.1,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        text = response.choices[0].message.content.strip()
        return text if text else None
    except Exception as exc:
        print(f"[ocr] Extraction failed for {image_path.name}: {exc}")
        return None


# ── Local OCR fallback (RapidOCR) ───────────────────────────────────────────────
# Local CPU OCR used when the LM Studio OCR stage fails or is unreachable. The
# recognized text feeds the same distillation stage as LM Studio OCR.
#
# RapidOCR runs PaddleOCR's PP-OCR models on onnxruntime: the ONNX weights ship
# inside the wheel (no first-run model download) and there is no paddlepaddle /
# paddlex / langchain / setuptools chain to keep version-aligned — which is what
# made the previous PaddleOCR fallback so brittle inside the slim Docker image.

# Honour the legacy PADDLEOCR_ENABLED name so existing deploys / .env files keep
# toggling the fallback after the engine swap.
LOCAL_OCR_ENABLED = (
    os.getenv("LOCAL_OCR_ENABLED", os.getenv("PADDLEOCR_ENABLED", "1")).lower()
    not in ("0", "false", "no")
)

_ocr_engine = None          # RapidOCR instance, or False after an init failure
_ocr_init_error: str = ""   # last engine-init exception (exposed by /debug/ocr-status)
_ocr_lock = threading.Lock()


def _import_rapidocr():
    """Return the RapidOCR class from whichever package is installed.

    Prefer the mature, self-contained ``rapidocr-onnxruntime`` (PP-OCR models
    bundled in the wheel, onnxruntime backend, no runtime download); fall back to
    the newer unified ``rapidocr`` package when that's the one present.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        from rapidocr import RapidOCR  # newer unified package
    return RapidOCR


def _get_ocr_engine():
    """Lazy local-OCR (RapidOCR) singleton. Returns None when disabled/unavailable."""
    global _ocr_engine, _ocr_init_error
    if not LOCAL_OCR_ENABLED:
        return None
    if _ocr_engine is not None:
        return _ocr_engine or None
    with _ocr_lock:
        if _ocr_engine is not None:
            return _ocr_engine or None
        try:
            RapidOCR = _import_rapidocr()
            _ocr_engine = RapidOCR()
            _ocr_init_error = ""
            print("[ocr] RapidOCR fallback engine initialised")
        except Exception as exc:
            print(f"[ocr] RapidOCR unavailable: {exc}")
            _ocr_engine = False
            _ocr_init_error = str(exc)
    return _ocr_engine or None


def _reset_ocr_engine_failure() -> None:
    """Clear a cached engine-init failure so the next call retries.

    A failed init is cached (engine = False) to avoid re-paying a slow doomed
    init on every receipt. Diagnostics call this first so a fixed environment
    (package reinstalled) is picked up without a restart; a working engine is
    never discarded.
    """
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is False:
            _ocr_engine = None


def _rapidocr_lines(out) -> list[str]:
    """Pull recognized text out of a RapidOCR result, in the engine's reading
    order (top-to-bottom, left-to-right), one detected box per line.

    Handles both APIs: the mature rapidocr-onnxruntime returns ``(result,
    elapse)`` where ``result`` is a list of ``[box, text, score]``; the newer
    unified ``rapidocr`` package returns an object exposing a ``.txts`` sequence.
    """
    txts = getattr(out, "txts", None)
    if txts is not None:  # newer unified rapidocr package
        return [str(t) for t in txts if t]
    result = out[0] if isinstance(out, tuple) and out else out  # (result, elapse)
    lines: list[str] = []
    for entry in result or []:
        try:
            text = entry[1]
        except (IndexError, TypeError):
            continue
        if text:
            lines.append(str(text))
    return lines


def _as_xy_pairs(box) -> Optional[list]:
    """Coerce a RapidOCR polygon into a plain list of ``[x, y]`` float pairs.

    Accepts the nested 4-point form (``[[x, y], …]``, possibly numpy rows) or a
    flat ``[x1, y1, x2, y2, …]`` sequence. Returns None when the shape isn't
    usable so callers can simply skip markup for that line."""
    if box is None:
        return None
    try:
        pts = list(box)
        if not pts:
            return None
        first = pts[0]
        # Flat [x1, y1, x2, y2, …] (scalars, no per-point length) → pair it up.
        if not isinstance(first, (list, tuple)) and not hasattr(first, "__len__"):
            pts = [pts[i:i + 2] for i in range(0, len(pts) - 1, 2)]
        out = []
        for p in pts:
            xy = list(p)
            out.append([float(xy[0]), float(xy[1])])
        return out or None
    except (TypeError, ValueError, IndexError):
        return None


def _rapidocr_line_boxes(out) -> list:
    """Like :func:`_rapidocr_lines` but keeps each detected line's geometry.

    Returns ``[{"text": str, "box": [[x, y], …] | None, "score": float}, …]`` in
    the engine's reading order. The bounding box (RapidOCR's 4-point polygon) and
    per-line confidence are exactly what ``_rapidocr_lines`` discards; preserving
    them lets the UI mark up where each field was read — with no LLM involved.

    Degrades gracefully: the newer unified ``rapidocr`` package may expose only
    ``.txts`` (no boxes), in which case ``box`` is None and the overlay is simply
    skipped while the text still flows to distillation."""
    txts = getattr(out, "txts", None)
    if txts is not None:  # newer unified rapidocr package
        boxes  = getattr(out, "boxes", None)
        scores = getattr(out, "scores", None)
        rows: list = []
        for i, t in enumerate(txts):
            if not t:
                continue
            box = _as_xy_pairs(boxes[i]) if boxes is not None and i < len(boxes) else None
            score = 0.0
            if scores is not None and i < len(scores):
                try:
                    score = float(scores[i])
                except (TypeError, ValueError):
                    score = 0.0
            rows.append({"text": str(t), "box": box, "score": score})
        return rows
    result = out[0] if isinstance(out, tuple) and out else out  # (result, elapse)
    rows = []
    for entry in result or []:
        try:
            box, text, score = entry[0], entry[1], entry[2]
        except (IndexError, TypeError):
            continue
        if not text:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        rows.append({"text": str(text), "box": _as_xy_pairs(box), "score": score})
    return rows


def _extract_local_ocr_lines(image_path: Path) -> tuple:
    """Run the local OCR engine once and return per-line boxes plus the image's
    pixel size: ``(rows, width, height)``.

    The boxes are in the coordinate space of ``image_path`` as the engine read it
    (after the in-place grayscale/autocrop passes, before the deferred export
    compression), so normalizing them by ``(width, height)`` yields resolution-
    independent positions that still map onto the image shown in the UI. Returns
    ``([], 0, 0)`` when the engine is unavailable or errors out."""
    engine = _get_ocr_engine()
    if engine is None:
        return [], 0, 0
    try:
        out = engine(str(image_path))
        rows = _rapidocr_line_boxes(out)
    except Exception as exc:
        print(f"[ocr] local OCR failed for {image_path.name}: {exc}")
        return [], 0, 0
    w = h = 0
    try:
        with Image.open(image_path) as im:
            w, h = im.size
    except Exception:
        pass
    return rows, w, h


def _extract_local_ocr(image_path: Path) -> Optional[str]:
    """Run the local OCR engine (RapidOCR) on an image, returning recognized
    lines joined by newlines (None when the engine is unavailable or finds nothing)."""
    rows, _, _ = _extract_local_ocr_lines(image_path)
    text = "\n".join(r["text"] for r in rows).strip()
    return text or None


def _rotate_ops() -> list:
    """The three 90° transpose ops with labels, resolved across Pillow versions
    (``Image.Transpose.ROTATE_90`` on new Pillow, ``Image.ROTATE_90`` on old)."""
    base = getattr(Image, "Transpose", Image)
    ops = []
    for label, name in (("90°", "ROTATE_90"), ("180°", "ROTATE_180"), ("270°", "ROTATE_270")):
        op = getattr(base, name, None)
        if op is not None:
            ops.append((op, label))
    return ops


_ROTATE_OPS = _rotate_ops()


def _ocr_orientation_score(rows: list) -> float:
    """How well a page reads when OCR'd: recognized alnum chars weighted by mean
    line confidence. Upright receipts yield far more clear text than rotated ones,
    so this reliably ranks the four orientations — no model needed."""
    if not rows:
        return 0.0
    chars = 0
    confs: list = []
    for r in rows:
        chars += sum(c.isalnum() for c in (r.get("text") or ""))
        s = r.get("score")
        if s is not None:
            try:
                confs.append(float(s))
            except (TypeError, ValueError):
                pass
    mean_conf = (sum(confs) / len(confs)) if confs else 0.5
    return chars * (0.5 + 0.5 * mean_conf)


def _ocr_lines_best_orientation(image_path: Path) -> tuple:
    """Local OCR, but if the page reads poorly try the three 90° rotations and keep
    whichever RapidOCR reads best — rewriting the file in place at the winning angle
    so the stored image, the OCR boxes, and the UI preview all share one orientation.

    Returns ``(rows, w, h, note)`` where ``note`` is a human string when a rotation
    was applied (for the step log), else ``""``. Bounded: the rotation search only
    runs on a weak upright read, so well-oriented receipts pay nothing extra."""
    rows, w, h = _extract_local_ocr_lines(image_path)
    if not (AUTOROTATE_ENABLED and ORIENT_BY_OCR and w > 0 and h > 0):
        return rows, w, h, ""
    base_score = _ocr_orientation_score(rows)
    if base_score >= ORIENT_MIN_SCORE:
        return rows, w, h, ""

    best = (base_score, None, rows, w, h)  # (score, op, rows, w, h)
    for op, label in _ROTATE_OPS:
        tmp = None
        try:
            with Image.open(image_path) as im:
                fmt = (im.format or "").upper()
                cand = im.transpose(op)
            tmp = image_path.with_name(f".orient_{label}_{image_path.name}")
            _save_image_inplace(cand, tmp, fmt)
            crows, cw, ch = _extract_local_ocr_lines(tmp)
        except Exception:
            crows, cw, ch = [], 0, 0
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except Exception:
                    pass
        cscore = _ocr_orientation_score(crows)
        if cscore > best[0] * ORIENT_IMPROVE_RATIO:
            best = (cscore, op, crows, cw, ch)

    _, op, brows, bw, bh = best
    if op is None:
        return rows, w, h, ""
    try:
        with Image.open(image_path) as im:
            fmt = (im.format or "").upper()
            fixed = im.transpose(op)
        _save_image_inplace(fixed, image_path, fmt)
    except Exception:
        return rows, w, h, ""
    label = next((lbl for o, lbl in _ROTATE_OPS if o == op), "")
    return brows, bw, bh, f"auto-rotated {label} — OCR reads upright"


def _combine_ocr_sources(
    local_text: Optional[str], llm_text: Optional[str]
) -> Optional[str]:
    """Merge transcriptions from the built-in OCR engine and the LLM OCR model.

    When both are present they are concatenated under neutral labels so the
    distillation model can cross-reference the two readings of the same receipt
    (and the amount audit sees the union of every printed money value). When only
    one source produced text it is returned as-is, so single-source behaviour is
    unchanged.
    """
    local_text = (local_text or "").strip()
    llm_text   = (llm_text or "").strip()
    if local_text and llm_text:
        return (
            "=== OCR transcription A (built-in engine) ===\n"
            f"{local_text}\n\n"
            "=== OCR transcription B (vision model) ===\n"
            f"{llm_text}"
        )
    return local_text or llm_text or None


# ── Per-item step log ──────────────────────────────────────────────────────────

def _append_step(
    steps: Optional[list],
    step: str,
    label: str,
    detail: str = "",
    *,
    ok: bool = True,
    duration_s: float = 0.0,
) -> None:
    """Append one processing step to the per-item step log. No-op when steps is None."""
    if steps is None:
        return
    steps.append({
        "step":       step,
        "label":      label,
        "detail":     detail,
        "ok":         ok,
        "duration_s": round(duration_s, 2),
    })


# ── Offline rule-based distillation ────────────────────────────────────────────
# When LM Studio is disabled/unreachable the OCR text (from RapidOCR) still needs
# to be turned into structured fields. Sending it to the LM Studio distillation
# model would fail too, so receipts that successfully OCR'd would otherwise land in
# "failed". This pure-regex parser is the genuine offline fallback: no model
# required, so an imported image still produces a usable (if lower-confidence)
# result when the AI backend is down.

_DATE_PATTERNS = (
    # ISO  2026-05-01
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    # US   05/01/2026  or 5-1-26
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"), "mdy"),
    # Month name  May 1, 2026 / 1 May 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "mname"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b"), "dname"),
)


def _find_date_in_text(text: str) -> str:
    """Best-effort extraction of a transaction date as YYYY-MM-DD ('' if none)."""
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif kind == "mdy":
                    mo, d, y = int(m.group(1)), int(m.group(2)), _normalize_year(int(m.group(3)))
                elif kind == "mname":
                    mo = MONTH_MAP.get(m.group(1).lower())
                    d, y = int(m.group(2)), int(m.group(3))
                else:  # dname
                    mo = MONTH_MAP.get(m.group(2).lower())
                    d, y = int(m.group(1)), int(m.group(3))
                if not mo:
                    continue
                return date(y, mo, d).isoformat()
            except (ValueError, TypeError):
                continue
    return ""


def _normalize_year(y: int) -> int:
    """Two-digit year → the 2000s (24 → 2024); four-digit years pass through.

    Per the app's convention every ``YY`` is assumed to be 20xx — we do not try
    to guess 19xx — so "99" becomes 2099, not 1999.
    """
    return 2000 + y if 0 <= y < 100 else y


def _iso_or_blank(y: int, mo: int, d: int) -> str:
    try:
        return date(y, mo, d).isoformat()
    except (ValueError, TypeError):
        return ""


def normalize_date(raw) -> str:
    """Normalize a receipt date string to canonical ``YYYY-MM-DD`` — US-first.

    Receipts in this app are assumed to follow the US **MM/DD/YYYY** convention,
    so an ambiguous numeric date like ``08-15-24`` is read as month=08, day=15,
    year=2024 — we deliberately do **not** try to infer DD/MM order (that
    guessing is exactly what we want to take away from the LLM). Rules:

      * **US month/day order** for all numeric dates.
      * **Two-digit years → 2000s** (``24`` → ``2024``) via ``_normalize_year``.
      * **Separators**: dashes, slashes, and dots are all accepted
        (``08-15-24`` / ``08/15/24`` / ``08.15.24``).
      * An already-ISO ``YYYY-MM-DD`` (or ``YYYY/MM/DD``) is trusted year-first.
      * Common month-name forms (``May 1, 2024`` / ``1 May 2024``) are handled.

    Returns ``''`` when nothing date-like (or nothing *valid*, e.g. ``13/40/24``)
    can be found, so callers can fall back cleanly.
    """
    if not raw:
        return ""
    s = str(raw).strip()

    # Year-first ISO (4-digit year leads) — trust it as written.
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", s)
    if m:
        return _iso_or_blank(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # US numeric: MM[sep]DD[sep]YY or YYYY, with - / . separators.
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", s)
    if m:
        return _iso_or_blank(_normalize_year(int(m.group(3))),
                             int(m.group(1)), int(m.group(2)))

    # Month-name forms: "May 1, 2024" / "May. 1 24".
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{2,4})\b", s)
    if m and m.group(1).lower() in MONTH_MAP:
        return _iso_or_blank(_normalize_year(int(m.group(3))),
                             MONTH_MAP[m.group(1).lower()], int(m.group(2)))

    # Day-first month-name: "1 May 2024".
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{2,4})\b", s)
    if m and m.group(2).lower() in MONTH_MAP:
        return _iso_or_blank(_normalize_year(int(m.group(3))),
                             MONTH_MAP[m.group(2).lower()], int(m.group(1)))

    return ""


_ADDRESS_HINTS = re.compile(
    r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|hwy|"
    r"highway|ct|court|pkwy|parkway|ste|suite|unit|apt|fl|floor|"
    r"way|plaza|pike|terrace|trail)\b",
    re.IGNORECASE,
)
_STATE_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_PHONE = re.compile(r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")


def _looks_like_address(line: str) -> bool:
    """True when a line looks like a street address, phone, website or city/zip —
    i.e. something the vendor name should NOT be pulled from."""
    s = line.strip()
    low = s.lower()
    if low.startswith(("http", "www.")) or ".com" in low or "@" in low:
        return True
    if _PHONE.search(s) or _STATE_ZIP.search(s):
        return True
    # "123 Main St", "1700 W 7th Ave" — leading street number plus a street word.
    if re.match(r"^\s*\d{1,6}\s+\w", s) and _ADDRESS_HINTS.search(s):
        return True
    return False


def _guess_vendor_line(ocr_text: str) -> str:
    """Pick the most plausible business-name line from OCR text.

    Used only when no known brand matched. Scans the top of the receipt (where
    the name almost always sits), skips address/phone/website/total/number lines,
    and prefers short ALL-CAPS or Title-Case lines that read like a name.
    """
    best_line, best_score = "", -1.0
    for idx, raw in enumerate(ocr_text.splitlines()[:8]):
        s = raw.strip()
        if len(s) < 3 or not any(c.isalpha() for c in s):
            continue
        if _looks_like_address(s):
            continue
        letters = sum(c.isalpha() for c in s)
        digits = sum(c.isdigit() for c in s)
        if digits > letters:                       # mostly a number → not a name
            continue
        if re.search(r"\b(total|subtotal|tax|cash|change|visa|debit|credit|"
                     r"balance|amount|receipt|invoice|tel|phone)\b", s, re.IGNORECASE):
            continue
        score = 5.0 - idx                           # earlier lines score higher
        if s.isupper():
            score += 2.0                            # storefront names are often ALL CAPS
        elif s == s.title():
            score += 1.0                            # Title Case also reads like a name
        if 3 <= len(s) <= 40:
            score += 1.0
        if score > best_score:
            best_line, best_score = s[:60], score
    if best_line:
        return best_line
    # Nothing scored — fall back to the first line with letters (legacy behaviour).
    for raw in ocr_text.splitlines():
        s = raw.strip()
        if len(s) >= 3 and any(c.isalpha() for c in s):
            return s[:60]
    return ""


def _local_distill_from_ocr(ocr_text: str) -> Optional[dict]:
    """Rule-based field extraction from raw OCR text — no LLM involved.

    Returns the same schema the LM distillation produces (so the rest of the
    pipeline is unchanged), or None when there isn't enough to work with. Always
    flags the receipt for manual review since fields were parsed heuristically.
    """
    if not ocr_text or not ocr_text.strip():
        return None

    # Prefer the printed grand total; only when no total line exists fall back
    # to the largest money value (some receipts only print the bare number).
    amount = extract_best_total(ocr_text)
    if not amount:
        candidates = extract_candidate_totals(ocr_text)
        amount = max(candidates) if candidates else 0.0

    # Vendor: first try the known-vendor database (handles the common case where
    # the OCR'd name would otherwise lose out to the store address), then fall
    # back to the address-skipping line heuristic.
    matched = match_vendor(ocr_text)
    if matched:
        vendor, matched_category = matched
    else:
        vendor, matched_category = _guess_vendor_line(ocr_text), None

    if not amount or not vendor:
        return None

    if matched_category:
        category = matched_category
    else:
        low = ocr_text.lower()
        if (any(rx.search(low) for rx in _FUEL_VENDOR_PATTERNS.values())
                or any(rx.search(low) for rx in _FUEL_KEYWORD_PATTERNS.values())):
            category = "fuel"
        elif any(rx.search(low) for rx in _MATS_VENDOR_PATTERNS.values()):
            category = "mats"
        else:
            category = "misc"

    return {
        "date":                _find_date_in_text(ocr_text),
        "vendor":              vendor,
        "amount":              amount,
        "category":            category,
        "expense_description": None,
        "ai_summary":          vendor,
        "flags": [],
        "_local_parse": True,
    }


def _unified_distillation(
    client: OpenAI, ocr_text: str, *, _retry: bool = True,
) -> Optional[dict]:
    """Stage 2: distillation model extracts fields + summary + flags from OCR text."""
    today = date.today().isoformat()
    prompt = _UNIFIED_DISTILLATION_TEMPLATE.format(ocr_text=ocr_text, today=today)
    system_msg = {"role": "system", "content": "You are a receipt data extractor. Respond with valid JSON only."}
    user_msg   = {"role": "user", "content": prompt}

    _parse = _parse_llm_record

    thinking_body = _thinking_body(8192)
    try:
        resp = client.chat.completions.create(
            model=_active_distill_model,
            messages=[system_msg, user_msg],
            temperature=0.0, max_tokens=1024,
            frequency_penalty=0.15,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        result = _parse(resp.choices[0].message.content.strip())
        if result is not None:
            return result
        if _retry:
            print(f"[distill] JSON parse failed, retrying…")
            r2 = client.chat.completions.create(
                model=_active_distill_model,
                messages=[system_msg, user_msg,
                          {"role": "user", "content": "Return ONLY the JSON object — no extra text, no markdown."}],
                temperature=0.0, max_tokens=1024,
                frequency_penalty=0.15,
                extra_body={**thinking_body, "repeat_penalty": 1.1},
            )
            return _parse(r2.choices[0].message.content.strip())
    except Exception as exc:
        print(f"[distill] Exception: {exc}")
    return None


def _extract_with_model(
    client: OpenAI, image_path: Path, model_id: str, *, _retry: bool = True,
) -> Optional[dict]:
    """Direct-vision extraction — used as the sole path when no OCR model is set,
    and as fallback when OCR + distillation yields low-confidence results."""
    today = date.today().isoformat()
    prompt = _GEMMA_VISION_TEMPLATE.replace("{today}", today)

    _parse = _parse_llm_record

    thinking_body = _thinking_body(8192)
    try:
        b64, mime = encode_image(image_path)
        system_msg = {"role": "system", "content": "You are a receipt data extractor. Always respond with valid JSON only."}
        user_msg   = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}
        resp = client.chat.completions.create(
            model=model_id, messages=[system_msg, user_msg],
            temperature=0.0, max_tokens=1024,
            frequency_penalty=0.15,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        result = _parse(resp.choices[0].message.content.strip())
        if result is not None:
            return result
        if _retry:
            print(f"[extract] JSON parse failed for {image_path.name}, retrying…")
            r2 = client.chat.completions.create(
                model=model_id,
                messages=[system_msg, user_msg,
                          {"role": "user", "content": "Your response was not valid JSON. Return ONLY the JSON object."}],
                temperature=0.0, max_tokens=1024,
                frequency_penalty=0.15,
                extra_body={**thinking_body, "repeat_penalty": 1.1},
            )
            return _parse(r2.choices[0].message.content.strip())
    except Exception as exc:
        print(f"[extract] Exception for {image_path.name}: {exc}")
    return None


def _extract_receipt_with_status(
    client: OpenAI,
    image_path: Path,
    status_cb: Optional[Callable],  # (status, data, model) → None
    step_log: Optional[list] = None,
) -> Optional[dict]:
    """
    OCR-first pipeline with Kanban status callbacks and per-item step logging:

      1. PRIMARY  — built-in local RapidOCR transcribes the receipt (fast,
                    offline). When a dedicated OCR model is also selected, the
                    vision LLM transcribes it too and BOTH readings are
                    cross-referenced by the distillation model in one call.
      2. DISTILL  — the LM Studio model structures that text into fields. If the
                    LLM is unreachable, an offline rule-based parser fills the
                    fields and flags the receipt for manual review.
      3. RESCUE   — only when OCR produced no usable text (or distillation came
                    back low-confidence) does a vision-capable model read the
                    image directly.

    Each branch is recorded in step_log (if provided) for the per-item process log.
    """
    # Orientation pre-pass — bake the photo's EXIF rotation into the pixels FIRST,
    # so every later step (OCR, the vision model, the markup boxes, the preview)
    # sees text the right way up. A deeper OCR-guided rotation check runs inside the
    # OCR step below, where the engine's read tells us which way is actually upright.
    autorotate_image_file(image_path)
    # Black-&-white pre-pass — runs BEFORE any OCR/LLM call. Converts the stored
    # receipt to high-contrast grayscale in place so both the OCR engine (which
    # reads the file directly) and the vision model (via encode_image) get the
    # cleaner image. In-place, suffix preserved → no downstream path changes.
    grayscale_image_file(image_path)
    # Autocrop the uniform photo border next, still BEFORE OCR — this is the
    # canonical greyscale → autocrop → OCR order (autocrop_receipt is conservative:
    # it no-ops unless it can trim a clear border). OCR then reads the cropped
    # image, which is also the one shown in the UI, so the field-markup boxes stay
    # pixel-aligned with the preview. Gated by AUTOCROP_ENABLED.
    autocrop_image_file(image_path)

    def _cb(status: str, data: Optional[dict] = None, model: str = ""):
        if status_cb:
            status_cb(status, data, model)

    t_start = time.perf_counter()

    def _finish(data: Optional[dict], ocr_seconds: float = 0.0,
                distill_seconds: float = 0.0) -> Optional[dict]:
        if data is not None:
            data["_proc_seconds"] = round(time.perf_counter() - t_start, 1)
            if ocr_seconds:
                data["_ocr_seconds"] = round(ocr_seconds, 1)
            if distill_seconds:
                data["_distill_seconds"] = round(distill_seconds, 1)
            if step_log is not None:
                data["_steps"] = list(step_log)
        return data

    def _distill_text(ocr_text: str, ocr_seconds: float,
                      engine: str = "") -> Optional[dict]:
        _cb("distilling", model=_active_distill_model)
        t_distill = time.perf_counter()
        data = _unified_distillation(client, ocr_text)
        distill_dur = time.perf_counter() - t_distill
        local_used = False

        if data is None:
            # LM Studio distillation unreachable/failed → record failure, try offline parser
            _append_step(step_log, "distillation", "Distillation",
                         f"{_active_distill_model} – no response",
                         ok=False, duration_s=distill_dur)
            data = _local_distill_from_ocr(ocr_text)
            local_used = data is not None
            if local_used:
                _append_step(step_log, "local_parse", "Local parse",
                             "offline regex parser (LM Studio unavailable)", ok=True)

        # Ground the model's amount in the OCR text it was given: a value that
        # appears nowhere in the text is replaced by the printed grand total.
        if data is not None and not local_used:
            note = reconcile_amount(data, ocr_text)
            if note:
                data["flags"] = _normalize_flags(data.get("flags") or []) + [{"flag": note}]
                _append_step(step_log, "reconcile", "Amount check", note)

        distill_seconds = time.perf_counter() - t_distill
        if _is_low_confidence(data):
            if not local_used and data is not None:
                # LM Studio responded but result was too sparse to use
                _append_step(step_log, "distillation", "Distillation",
                             f"{_active_distill_model} – incomplete extraction",
                             ok=False, duration_s=distill_dur)
            return None

        # Result passes confidence check — record success if LM Studio handled it
        if not local_used:
            _append_step(step_log, "distillation", "Distillation",
                         _active_distill_model or "", ok=True, duration_s=distill_dur)
        data["_raw_ocr"] = ocr_text
        if engine:
            data["_ocr_engine"] = engine
        return _finish(data, ocr_seconds, distill_seconds)

    try:
        # PRIMARY: built-in local RapidOCR text extraction — fast, runs offline.
        # Capture the per-line boxes too (same single OCR pass) so the final
        # fields can later be marked up on the image without any LLM.
        _cb("ocr", model="rapidocr")
        t_ocr = time.perf_counter()
        local_rows, ocr_img_w, ocr_img_h, orient_note = _ocr_lines_best_orientation(image_path)
        local_text = "\n".join(r["text"] for r in local_rows).strip() or None
        # Fall back to the plain text reader only when the box-aware pass never ran
        # the engine (img size 0×0 → engine unavailable, e.g. RapidOCR not installed
        # or stubbed in tests). A real image that simply held no text returns a
        # non-zero size, so this never double-runs the engine in production.
        if local_text is None and ocr_img_w == 0 and ocr_img_h == 0:
            local_text = _extract_local_ocr(image_path)
        ocr_seconds = time.perf_counter() - t_ocr
        if orient_note:
            _append_step(step_log, "autorotate", "Auto-rotate", orient_note)
        if local_text:
            _append_step(step_log, "local_ocr", "OCR (built-in)",
                         "RapidOCR", ok=True, duration_s=ocr_seconds)
        else:
            _append_step(step_log, "local_ocr", "OCR (built-in)",
                         "no text extracted", ok=False, duration_s=ocr_seconds)

        # SECONDARY (optional): when a dedicated OCR model is selected, also
        # transcribe with the vision LLM so the distillation model can
        # cross-reference both readings of the same receipt. Reasoning is forced
        # off for this transcription pass.
        llm_text = None
        if _active_ocr_model:
            _cb("ocr", model=_active_ocr_model)
            t_llm = time.perf_counter()
            llm_text = _extract_raw_ocr(client, image_path, _active_ocr_model)
            llm_secs = time.perf_counter() - t_llm
            ocr_seconds += llm_secs
            _append_step(
                step_log, "llm_ocr", "OCR (LLM)",
                _active_ocr_model if llm_text else f"{_active_ocr_model} – no text",
                ok=bool(llm_text), duration_s=llm_secs,
            )

        combined_text = _combine_ocr_sources(local_text, llm_text)
        if combined_text:
            both = bool(local_text and llm_text)
            engine = ("rapidocr+llm" if both
                      else "rapidocr" if local_text else "llm-ocr")
            if both:
                _append_step(step_log, "cross_reference", "Cross-reference",
                             "distill model reconciles both OCR sources")
            # Hand the OCR text to the LLM to structure into fields. If LM Studio
            # is unavailable, _distill_text falls back to the offline rule-based
            # parser, which flags the receipt for manual review.
            data = _distill_text(combined_text, ocr_seconds, engine=engine)
            if data is not None:
                # Locate vendor/date/amount on the image from the RapidOCR boxes
                # (after reconcile_amount, so the amount box follows any
                # correction). Rules-based, cheap, no extra OCR or LLM call.
                field_boxes = locate_field_boxes(local_rows, ocr_img_w, ocr_img_h, data)
                if field_boxes:
                    data["_field_boxes"] = field_boxes
                return data
            print(f"[extract] OCR+distill low-confidence for {image_path.name}, "
                  "trying direct vision")

        # RESCUE: OCR found nothing usable (or distillation was low-confidence) —
        # let a vision-capable LLM read the image directly when one is available.
        _cb("distilling", model=_active_distill_model)
        t_distill = time.perf_counter()
        data = _extract_with_model(client, image_path, _active_distill_model)
        vision_dur = time.perf_counter() - t_distill
        if data is not None:
            _append_step(step_log, "vision", "Vision",
                         _active_distill_model or "", ok=True, duration_s=vision_dur)
            return _finish(data, ocr_seconds=ocr_seconds, distill_seconds=vision_dur)
        _append_step(step_log, "vision", "Vision",
                     f"{_active_distill_model} – no response", ok=False, duration_s=vision_dur)
        return None
    except Exception as exc:
        print(f"[extract] Unhandled exception for {image_path.name}: {exc}")
        return None


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Convenience wrapper without status callbacks."""
    return _extract_receipt_with_status(client, image_path, status_cb=None)


# ── Amount audit (rules-based OCR cross-check) ─────────────────────────────────
# LLMs occasionally hallucinate or mis-copy the total. When raw OCR text is
# available we cross-check the extracted amount against money values that
# appear on total-like lines — pure regex, no model involved.

_TOTAL_KEYWORD_RE = re.compile(
    r"\b(grand\s*total|sub[-\s]?total|subtotal|total\s*due|amount\s*due|"
    r"balance\s*due|total|amount|balance)\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})")

# Lines whose money value IS the receipt's final amount, strongest first.
_GRAND_TOTAL_RE = re.compile(
    r"\b(grand\s*total|total\s*due|amount\s*due|balance\s*due)\b", re.IGNORECASE)
_PLAIN_TOTAL_RE = re.compile(r"\btotal\b", re.IGNORECASE)
_SUBTOTAL_RE    = re.compile(r"\bsub[-\s]?total\b", re.IGNORECASE)
# A "total" line that is really something else (subtotal, tax, tender, change…)
_NON_GRAND_LINE_RE = re.compile(
    r"\b(sub[-\s]?total|subtotal|tax|savings|discount|tender(?:ed)?|tend|"
    r"cash|change|points|rewards?)\b", re.IGNORECASE)


def _money_values(s: str) -> list[float]:
    out = []
    for m in _MONEY_RE.finditer(s):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def extract_candidate_totals(text: str) -> list[float]:
    """Money values found on total-like lines of raw receipt text.

    Falls back to every money value in the text when no line mentions a
    total keyword (some receipts only print the bare number).
    """
    if not text:
        return []

    keyword_vals: list[float] = []
    for line in text.splitlines():
        if _TOTAL_KEYWORD_RE.search(line):
            keyword_vals.extend(_money_values(line))
    if keyword_vals:
        return sorted(set(keyword_vals))
    return sorted(set(_money_values(text)))


def extract_best_total(text: str) -> Optional[float]:
    """Best guess at the receipt's printed grand total, or None.

    Tier 1: lines naming the final amount explicitly (GRAND TOTAL, TOTAL DUE,
    AMOUNT DUE, BALANCE DUE).  Tier 2: plain TOTAL lines that aren't really a
    subtotal/tax/tender/change line.  Within a tier the largest value wins
    (e.g. FUEL TOTAL vs. the combined TOTAL on a fuel + car-wash receipt).
    """
    if not text:
        return None
    tier1: list[float] = []
    tier2: list[float] = []
    for line in text.splitlines():
        if _GRAND_TOTAL_RE.search(line):
            tier1.extend(v for v in _money_values(line) if v > 0)
        elif _PLAIN_TOTAL_RE.search(line) and not _NON_GRAND_LINE_RE.search(line):
            tier2.extend(v for v in _money_values(line) if v > 0)
    for vals in (tier1, tier2):
        if vals:
            return max(vals)
    return None


def reconcile_amount(data: Optional[dict], raw_text: str) -> Optional[str]:
    """Replace a hallucinated amount with the receipt's printed grand total.

    The distillation model only ever sees the OCR text, so an extracted amount
    that appears nowhere in that text cannot be a faithful copy of the receipt.
    When the text prints an explicit total line, adopt that value instead.
    Returns a human-readable note (used as a review flag) when the amount was
    changed, or None when it was left alone.
    """
    if not data or not raw_text:
        return None
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0

    best = extract_best_total(raw_text)

    if amount > 0:
        # Model copied the pre-tax SUBTOTAL? The printed grand total wins.
        if best and best > amount + 0.005:
            sub_vals = [v for line in raw_text.splitlines()
                        if _SUBTOTAL_RE.search(line)
                        for v in _money_values(line)]
            if any(abs(amount - v) < 0.005 for v in sub_vals):
                data["amount"] = best
                return (f"Amount corrected: ${amount:.2f} is the pre-tax subtotal; "
                        f"receipt total is ${best:.2f} — verify")
        candidates = extract_candidate_totals(raw_text)
        if any(abs(amount - c) < 0.005 for c in candidates):
            return None        # amount is printed on the receipt — keep it

        # Last guard before overwriting: only replace an amount that looks like a
        # hallucination. If the model's value appears anywhere among the money
        # values in the raw OCR text, it is a faithful copy — leave it alone.
        # (The subtotal-mismatch case above is the one sanctioned exception.)
        if any(abs(amount - v) < 0.005 for v in _money_values(raw_text)):
            return None

    if not best or abs(best - amount) < 0.005:
        return None

    data["amount"] = best
    if amount > 0:
        return (f"Amount corrected: model said ${amount:.2f} but receipt prints "
                f"total ${best:.2f} — verify")
    return f"Amount taken from printed total ${best:.2f} — verify"


def audit_amount(data: Optional[dict], raw_text: str) -> Optional[str]:
    """Cross-check the model's amount against OCR text.

    Sets data["_amount_verified"] (True/False) and returns a human-readable
    flag string when the amount cannot be found in the OCR text, or None
    when it verifies (or there is nothing to check against).
    """
    if not data or not raw_text:
        return None
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    candidates = extract_candidate_totals(raw_text)
    if not candidates:
        return None

    if any(abs(amount - c) < 0.005 for c in candidates):
        data["_amount_verified"] = True
        return None

    data["_amount_verified"] = False
    closest = min(candidates, key=lambda c: abs(c - amount))
    return (f"Amount ${amount:.2f} not found in receipt text "
            f"(closest printed total: ${closest:.2f}) — verify manually")


# ── On-image field localization (rules-based, no LLM) ──────────────────────────
# Map the final vendor / date / amount values back to the OCR line that produced
# each, so the UI can draw a highlight box on the receipt image showing exactly
# where the app read the field. This reuses the same matchers the pipeline already
# trusts — no model call, no extra OCR pass — and intentionally OMITS any field it
# cannot confidently locate so the UI can flag it instead of drawing a wrong box.

def _poly_to_norm_rect(box, img_w: int, img_h: int) -> Optional[list]:
    """Axis-aligned ``[x, y, w, h]`` normalized to 0..1 from a 4-point polygon.

    Normalizing by the OCR image size makes the box resolution-independent, so it
    survives the deferred export compression/downscale and still lands correctly
    on whatever size the image is rendered at in the browser."""
    if not box or img_w <= 0 or img_h <= 0:
        return None
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    nx = max(0.0, min(1.0, x0 / img_w))
    ny = max(0.0, min(1.0, y0 / img_h))
    nw = max(0.0, min(1.0 - nx, (x1 - x0) / img_w))
    nh = max(0.0, min(1.0 - ny, (y1 - y0) / img_h))
    if nw <= 0 or nh <= 0:
        return None
    return [round(nx, 5), round(ny, 5), round(nw, 5), round(nh, 5)]


def locate_field_boxes(line_boxes, img_w: int, img_h: int,
                       data: Optional[dict]) -> dict:
    """Locate the vendor/date/amount values on the receipt image, rules-based.

    Given RapidOCR's per-line boxes (from :func:`_extract_local_ocr_lines`) and
    the final extracted ``data``, return ``{"vendor": [x,y,w,h], "date": …,
    "amount": …}`` with each box normalized to 0..1. Fields that can't be matched
    to a line are omitted entirely (the UI shows a "location not detected" note).

    Matching reuses the pipeline's own helpers — the money regex for the amount,
    the date regex for the date, vendor-name containment for the vendor — so the
    markup agrees with how the fields were grounded (e.g. the amount box follows a
    ``reconcile_amount`` correction onto the printed grand-total line)."""
    out: dict = {}
    if not data or not line_boxes or img_w <= 0 or img_h <= 0:
        return out
    rows = [r for r in line_boxes if r.get("box")]  # only lines with geometry
    if not rows:
        return out

    # ── Amount: the line whose printed money value equals the FINAL amount ──────
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0
    if amount > 0:
        candidates = []  # (priority, row) — lower priority number wins
        for r in rows:
            text = r["text"]
            if not any(abs(amount - v) < 0.005 for v in _money_values(text)):
                continue
            if _GRAND_TOTAL_RE.search(text):
                prio = 0                       # GRAND TOTAL / TOTAL DUE …
            elif _PLAIN_TOTAL_RE.search(text) and not _NON_GRAND_LINE_RE.search(text):
                prio = 1                       # a plain TOTAL line
            elif _NON_GRAND_LINE_RE.search(text):
                prio = 3                       # subtotal/tax/tender share the value
            else:
                prio = 2                       # a bare number that happens to match
            candidates.append((prio, r))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            rect = _poly_to_norm_rect(candidates[0][1]["box"], img_w, img_h)
            if rect:
                out["amount"] = rect

    # ── Date: the first line that parses to the same ISO date ───────────────────
    want_date = (data.get("date") or "").strip()
    if want_date:
        for r in rows:
            if _find_date_in_text(r["text"]) == want_date:
                rect = _poly_to_norm_rect(r["box"], img_w, img_h)
                if rect:
                    out["date"] = rect
                break

    # ── Vendor: the line that best contains the vendor name ─────────────────────
    vendor = (data.get("vendor") or "").strip().lower()
    if vendor:
        best = None  # (score, row)
        for idx, r in enumerate(rows):
            tlow = r["text"].strip().lower()
            if not tlow:
                continue
            if tlow == vendor:
                score = 3.0
            elif vendor in tlow or tlow in vendor:
                score = 2.0
            else:
                vtok = set(re.findall(r"[a-z0-9]+", vendor))
                ttok = set(re.findall(r"[a-z0-9]+", tlow))
                shared = vtok & ttok
                if not shared:
                    continue
                score = 1.0 + len(shared) / max(len(vtok), 1)
            score -= idx * 0.01  # earlier lines win ties — the name sits up top
            if best is None or score > best[0]:
                best = (score, r)
        if best is not None:
            rect = _poly_to_norm_rect(best[1]["box"], img_w, img_h)
            if rect:
                out["vendor"] = rect

    return out


# ── Category classification ────────────────────────────────────────────────────

def classify_category(data: dict) -> str:
    model_cat = (data.get("category") or "misc").lower().strip()
    if model_cat == "materials":
        model_cat = "mats"
    if model_cat not in ("fuel", "mats", "misc"):
        model_cat = "misc"

    vendor   = (data.get("vendor") or "").lower()
    summary  = (data.get("ai_summary") or data.get("summary") or "").lower()
    expense  = (data.get("expense_description") or "").lower()
    raw_ocr  = (data.get("_raw_ocr") or "").lower()
    combined = f"{vendor} {summary} {expense} {raw_ocr}"

    # Fuel: AI-extracted vendor name is strongest signal (+3); fuel vendor found in
    # raw OCR text but not in extracted vendor adds a secondary signal (+2); fuel
    # keywords anywhere in the combined text add +1 each.  All matches are
    # word-bounded (see _kw_pattern) so generic receipt text can't fake a signal.
    # Purely-numeric brands ("76") are too generic in raw OCR — a space-delimited
    # "76" (PUMP 76, 76 MAIN ST, LANE 76) reads as fuel — so they are honoured ONLY
    # when they match the extracted VENDOR field, never the raw OCR text.
    fuel_score = sum(3 for rx in _FUEL_VENDOR_PATTERNS.values() if rx.search(vendor))
    fuel_score += sum(2 for kw, rx in _FUEL_VENDOR_PATTERNS.items()
                      if not kw.isdigit() and rx.search(raw_ocr) and not rx.search(vendor))
    fuel_score += sum(1 for rx in _FUEL_KEYWORD_PATTERNS.values() if rx.search(combined))

    if fuel_score >= 3:
        return "fuel"
    if model_cat == "fuel" and fuel_score >= 1:
        return "fuel"

    if any(rx.search(vendor) for rx in _MATS_VENDOR_PATTERNS.values()):
        return "mats"

    return model_cat


def audit_warning_flags(data: dict, category: str) -> list[str]:
    """User-configured spending/date warnings, applied deterministically.

    Returns a list of human-readable warning strings for this receipt based on
    the current ``AMOUNT_LIMITS`` (per-category $ caps) and ``MAX_RECEIPT_AGE_DAYS``
    settings. Returns ``[]`` when nothing is configured (the default), so a
    receipt gets **no** warnings unless the user has opted in — replacing the old
    hard-coded fuel>$200 / mats>$500 / misc>$300 and 6-month-window prompt rules.
    """
    out: list[str] = []
    limit = AMOUNT_LIMITS.get((category or "").lower())
    if limit is not None:
        try:
            amt = float(data.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt > limit:
            out.append(f"Amount ${amt:,.2f} exceeds the ${limit:,.0f} {category} limit")

    if MAX_RECEIPT_AGE_DAYS is not None:
        iso = normalize_date(data.get("date") or "") or (data.get("date") or "")
        try:
            age = (date.today() - date.fromisoformat(iso)).days
            if age > MAX_RECEIPT_AGE_DAYS:
                out.append(f"Receipt is {age} days old (dated {iso}; over the "
                           f"{MAX_RECEIPT_AGE_DAYS}-day limit)")
        except (ValueError, TypeError):
            pass
    return out


# ── Duplicate detection ────────────────────────────────────────────────────────

def _detect_duplicates(results: list[dict]) -> None:
    seen: dict[tuple, int] = {}
    for i, r in enumerate(results):
        key = (
            (r.get("vendor") or "").lower().strip(),
            r.get("date") or "",
            round(float(r.get("amount") or 0), 2),
        )
        if key[2] == 0:
            continue
        if key in seen:
            first = seen[key]
            if not results[first].get("_flag"):
                results[first]["_flag"] = "Potential duplicate entry"
            if not r.get("_flag"):
                r["_flag"] = f"Duplicate of receipt #{first + 1} (same vendor/date/amount)"
        else:
            seen[key] = i


# ── Photo renaming ─────────────────────────────────────────────────────────────

def sanitize_filename_part(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40]


def _format_date_mmddyy(raw_date: str) -> str:
    """Convert YYYY-MM-DD (or YYYY-M-D) to MM-DD-YY for filenames."""
    if not raw_date:
        return "unknown"
    # Handle both zero-padded and non-zero-padded dates
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw_date.strip())
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%m-%d-%y")
        except ValueError:
            pass
    return sanitize_filename_part(raw_date) or "unknown"


def rename_receipt_image(
    img_path: Path,
    data: dict,
    category: str,
    dest_dir: Optional[Path] = None,
) -> Path:
    """Rename to {category}_{MM-DD-YY}_{Vendor}.ext and optionally move to dest_dir.

    Example: fuel_12-30-24_chevron.jpg
    """
    raw_date   = (data.get("date") or "unknown").strip()
    date_str   = _format_date_mmddyy(raw_date)
    vendor_str = sanitize_filename_part(
        data.get("vendor") or data.get("expense_description") or "receipt"
    )
    cat_str  = category.lower()
    stem     = f"{cat_str}_{date_str}_{vendor_str}" if vendor_str else f"{cat_str}_{date_str}"
    ext      = img_path.suffix.lower()
    out_dir  = dest_dir if dest_dir is not None else img_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    new_path = out_dir / f"{stem}{ext}"

    if new_path.exists() and new_path.resolve() != img_path.resolve():
        # Walk numbered suffixes, but cap the scan so a folder already holding
        # thousands of same-named receipts can't spin here indefinitely — past
        # the cap, fall back to a short random suffix that's guaranteed unique.
        new_path = None
        for counter in range(2, 10000):
            candidate = out_dir / f"{stem}_{counter}{ext}"
            if not candidate.exists():
                new_path = candidate
                break
        if new_path is None:
            new_path = out_dir / f"{stem}_{uuid.uuid4().hex[:8]}{ext}"

    if new_path.resolve() != img_path.resolve():
        shutil.move(str(img_path), str(new_path))
    return new_path


# ── Date helpers ───────────────────────────────────────────────────────────────

def sort_key_for_receipt(data: dict) -> date:
    raw = (data.get("date") or "").strip()
    if not raw:
        return date.max
    # Flexible YYYY-M-D parsing (handles missing zero-padding)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Month-name fallback
    month_num = MONTH_MAP.get(raw.lower())
    if month_num:
        today = date.today()
        year  = today.year if month_num <= today.month else today.year - 1
        return date(year, month_num, 1)
    return date.max


def compute_expense_period(results: list[dict]) -> str:
    dates = []
    for r in results:
        k = sort_key_for_receipt(r)
        if k != date.max:
            dates.append(k)
    if not dates:
        return ""
    fmt = lambda d: d.strftime("%m/%d/%y")
    return f"{fmt(min(dates))} - {fmt(max(dates))}"


# ── Spreadsheet generation ─────────────────────────────────────────────────────

def compress_result_images(results: list[dict], log: Optional[Callable] = None) -> int:
    """Compress each processed receipt image in place.

    This is the deferred *compress* step: instead of re-encoding every receipt
    the moment it is processed, the optimisation now runs once, at spreadsheet
    generation time, so the on-disk output folder and the images embedded in the
    workbook are both shrunk together.

    Idempotent — records are marked with ``_compressed`` so repeat calls (e.g. a
    second export of the same batch) are no-ops.  When compression changes the
    file suffix (``.png`` → ``.jpg``) the ``_image_path`` / ``_new_filename`` /
    ``_compressed_file`` fields are updated so later lookups still resolve.

    Returns the total number of bytes saved.
    """
    if not COMPRESS_ENABLED:
        return 0   # respect the runtime toggle; leave records unmarked so a later
                   # export (with compression re-enabled) still optimises them
    saved = 0
    for r in results:
        if r.get("_compressed"):
            continue
        p_str = r.get("_image_path")
        if not p_str:
            continue
        path = Path(p_str)
        if not path.exists():
            r["_compressed"] = True   # nothing on disk to shrink — don't retry forever
            continue
        try:
            before = path.stat().st_size
        except OSError:
            before = 0
        new_path = compress_image_file(path)
        try:
            after = new_path.stat().st_size
        except OSError:
            after = before
        if new_path != path:
            r["_image_path"] = str(new_path)
            if r.get("_new_filename"):
                r["_new_filename"] = new_path.name
            r["_compressed_file"] = new_path.name
        r["_compressed"] = True
        if before and after and after < before:
            saved += before - after
            if log:
                pct = round((1 - after / before) * 100)
                log(f"[image] {new_path.name}: {before // 1024} KB → {after // 1024} KB (−{pct}%)")
    return saved


def generate_spreadsheet(
    results: list[dict],
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
) -> Optional[Path]:
    if not results:
        return None

    # Use _image_path as-is — images live in the temp/staged folder where they
    # were written during processing. Host path remapping belongs in UI only.
    resolved = list(results)

    # Deferred compression: shrink every stored receipt image now, right before
    # the workbook is built, so the output folder and the embedded images are
    # optimised in a single pass. Idempotent — already-compressed records skip.
    compress_result_images(resolved)

    by_category: dict[str, list] = defaultdict(list)
    for r in resolved:
        by_category[r.get("_category", "misc")].append(r)
    for cat_list in by_category.values():
        cat_list.sort(key=sort_key_for_receipt)

    expense_period = compute_expense_period(resolved)

    wb = build_themed_workbook(
        sections=dict(by_category),
        expense_period=expense_period,
        employee_name=employee_name,
        build_tag=APP_VERSION,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name   = re.sub(r'[^\w\s-]', '', employee_name or '').strip().replace(' ', '_') or 'Employee'
    datestamp   = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"Reimbursements_{safe_name}_{datestamp}.xlsx"
    wb.save(output_path)
    return output_path


# ── Main pipeline ──────────────────────────────────────────────────────────────

def process_receipts_batch(
    receipts_folder: Path,
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
    job_name_default: str = "",
    job_number_default: str = "",
    dry_run: bool = False,
    auto_generate: bool = True,
    use_folder_structure: bool = False,
    progress_callback:       Optional[Callable] = None,
    log_callback:            Optional[Callable] = None,
    cancel_event:            Optional[threading.Event] = None,
    receipt_status_callback: Optional[Callable] = None,
    openai_client=None,
    output_images_dir:       Optional[Path] = None,
) -> dict:
    """
    2-stage pipeline: OCR (optional) → Unified Distillation.

    receipt_status_callback(idx, total, filename, status, data, model):
      Emits per-receipt stage updates for the Kanban board.
      status: queued | ocr | distilling | done | failed
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def progress(cur: int, tot: int, fname: str):
        if progress_callback:
            progress_callback(cur, tot, fname)

    def receipt_status(idx, tot, fname, status, data, model=""):
        if receipt_status_callback:
            receipt_status_callback(idx, tot, fname, status, data, model)

    if use_folder_structure:
        intake_dir    = receipts_folder / "Intake"
        proc_dir      = receipts_folder / "Processing"
        retry_dir     = receipts_folder / "Failed" / "Retry"
        completed_dir = output_dir / "Completed"
        for d in (intake_dir, proc_dir, retry_dir, completed_dir):
            d.mkdir(parents=True, exist_ok=True)
        scan_dir = intake_dir
    else:
        scan_dir = receipts_folder

    # Expand any PDFs in the scan directory to JPEG images before processing
    _pdf_tmp_dirs: list[Path] = []
    for pdf_path in sorted(scan_dir.glob("*.pdf")):
        tmp = scan_dir / f"_pdf_{pdf_path.stem}"
        pages = pdf_to_images(pdf_path, tmp)
        if pages:
            log(f"[pdf] Expanded {pdf_path.name} → {len(pages)} page(s)")
            _pdf_tmp_dirs.append(tmp)
            # Move original PDF to output so it won't be picked up again
            pdf_dest = (output_images_dir or output_dir) / pdf_path.name
            (output_images_dir or output_dir).mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf_path), str(pdf_dest))

    images = sorted(
        [p for p in scan_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    total = len(images)
    if total == 0:
        log("No receipt images found.")
        return {"processed": 0, "skipped": [], "total": 0,
                "output_path": None, "expense_period": "", "results": []}

    log(f"Found {total} receipt image(s).")
    log(f"  OCR model:    {_active_ocr_model or '(none — direct vision)'}")
    log(f"  Distill model: {_active_distill_model}")
    client = openai_client if openai_client is not None else OpenAI(
        base_url=LMSTUDIO_BASE_URL, api_key="lmstudio",
        timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES,
    )

    results: list[dict] = []
    skipped: list[str]  = []

    for i, img_path in enumerate(images, start=1):
        receipt_status(i, total, img_path.name, "queued", None)

    futures_map: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS or None) as executor:
        for i, img_path in enumerate(images, start=1):
            if cancel_event and cancel_event.is_set():
                log("Processing stopped by user.")
                break

            if use_folder_structure:
                proc_path = proc_dir / img_path.name
                img_path.rename(proc_path)
                img_path = proc_path

            idx = i
            img = img_path

            def make_status_cb(ridx, rname):
                def cb(status, data=None, model=""):
                    receipt_status(ridx, total, rname, status, data, model)
                    progress(ridx, total, rname)
                return cb

            future = executor.submit(
                _extract_receipt_with_status, client, img, make_status_cb(idx, img_path.name)
            )
            futures_map[future] = (idx, img_path)

        raw_results: list[tuple] = []
        for future in concurrent.futures.as_completed(futures_map):
            ridx, img_path = futures_map[future]
            try:
                data = future.result()
            except Exception as exc:
                log(f"  [{ridx}/{total}] ERROR — {img_path.name}: {exc}")
                data = None
            raw_results.append((ridx, img_path, data))

    raw_results.sort(key=lambda t: t[0])
    for ridx, img_path, data in raw_results:
        if cancel_event and cancel_event.is_set():
            log("Processing stopped by user.")
            break

        log(f"  [{ridx}/{total}] {img_path.name}")

        if data is None or _is_low_confidence(data):
            reason = "extraction failed" if data is None else "low confidence"
            log(f"    SKIPPED — {reason}")
            skipped.append(img_path.name)
            receipt_status(ridx, total, img_path.name, "failed", None)
            if use_folder_structure:
                try:
                    img_path.rename(retry_dir / img_path.name)
                except Exception:
                    pass
            continue

        category = classify_category(data)
        data["_category"] = category
        data["_original_index"] = ridx

        # Always use user-supplied job fields — never trust LLM extraction for these
        if category == "fuel":
            data["expense_description"] = None
        data["job_name"]   = job_name_default or DEFAULT_JOB_NAME
        data["job_number"] = job_number_default or DEFAULT_JOB_NUMBER

        flags_list = _normalize_flags(data.get("flags") or [])
        if flags_list and not data.get("_flag"):
            data["_flag"] = flags_list[0].get("flag", "")

        # Autocrop now; compression is deferred to generate_spreadsheet so the
        # stored image keeps full resolution until the report is built.
        autocrop_image_file(img_path)
        if use_folder_structure:
            dest_dir = completed_dir
            renamed    = rename_receipt_image(img_path, data, category)
            final_path = dest_dir / renamed.name
            try:
                shutil.move(str(renamed), str(final_path))
            except Exception:
                final_path = renamed
        else:
            dest = output_images_dir if output_images_dir is not None else None
            final_path = rename_receipt_image(img_path, data, category, dest)

        data["_new_filename"] = final_path.name
        data["_file"]         = final_path.name
        data["_image_path"]   = str(final_path)

        log(f"    [{category.upper():5}] {data.get('vendor','?')} — "
            f"${data.get('amount',0):.2f}  →  {final_path.name}")
        if data.get("_flag"):
            log(f"    FLAG: {data['_flag']}")

        receipt_status(ridx, total, img_path.name, "done", data)
        results.append(data)

    if not results:
        log("No receipts were successfully processed.")
        return {"processed": 0, "skipped": skipped, "total": total,
                "output_path": None, "expense_period": "", "results": []}

    _detect_duplicates(results)
    expense_period = compute_expense_period(results)
    log(f"\nExpense period: {expense_period or '(no parseable dates)'}")

    output_path: Optional[Path] = None
    if auto_generate and not dry_run:
        output_path = generate_spreadsheet(results, output_dir, employee_name)
        if output_path:
            log(f"Saved: {output_path}")
    elif dry_run:
        log("Dry run — workbook not saved.")

    processed = len(results)
    log(f"Done. {processed}/{total} receipts processed"
        + (f", {len(skipped)} skipped." if skipped else "."))

    return {
        "processed":      processed,
        "skipped":        skipped,
        "total":          total,
        "output_path":    output_path,
        "expense_period": expense_period,
        "results":        results,
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts",         default=RECEIPTS_FOLDER)
    parser.add_argument("--output-dir",       default=OUTPUT_FOLDER)
    parser.add_argument("--employee",         default="Duane Hamilton")
    parser.add_argument("--job-name",         default="")
    parser.add_argument("--job-number",       default="")
    parser.add_argument("--dry-run",          action="store_true")
    parser.add_argument("--folder-structure", action="store_true")
    args = parser.parse_args()

    receipts = Path(args.receipts)
    out_dir  = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not receipts.exists():
        print(f"ERROR: Receipts folder not found: {receipts}"); sys.exit(1)

    initialize_models()
    process_receipts_batch(
        receipts_folder=receipts,
        output_dir=out_dir,
        employee_name=args.employee,
        job_name_default=args.job_name,
        job_number_default=args.job_number,
        dry_run=args.dry_run,
        auto_generate=True,
        use_folder_structure=args.folder_structure,
    )


if __name__ == "__main__":
    main()
