# CLAUDE.md — Repo Map & Working Notes

> **Purpose.** Claude Code automatically reads this file at the start of every
> session. It exists so an assistant does **not** have to re-scan the whole
> codebase each time — read this first, then open only the files you need.
>
> **Maintenance rule.** At the **end of each session**, update this file with any
> structural changes you made (new modules, endpoints, settings, pipeline
> stages, conventions). Keep it accurate and concise — a stale map is worse than
> none. Treat the "Recent changes" log at the bottom as an append-only changelog.

---

## What this project is

A **local, private receipt → reimbursement-report** app. A user drops in receipt
photos/PDFs; the app reads each one with a **local** vision LLM (LM Studio, an
OpenAI-compatible endpoint) + a built-in OCR engine, organizes/renames the files,
lets the user review/correct/approve, and produces a polished multi-sheet Excel
workbook. **No receipt data ever leaves the machine** except to the local model.

- `BLUEPRINT.md` — the authoritative *what & why* spec (stack-agnostic). Update it
  when behavior changes.
- `TUTORIAL.md` — end-user, non-technical setup/usage guide.
- `README.md` — fuller project README.
- `ADVISORY.md` — security/operational advisory.
- `DESIGN_FROM_SCRATCH.md` — a design note: how the app would be rebuilt if the
  drivers were *low cost + ease of use* instead of *privacy + local-only*
  (hosted vision model, scale-to-zero web app). Not the current architecture.

## Stack

- **Backend:** Python 3.11+, **FastAPI** + Uvicorn (`server.py`). Server-Sent
  Events for live board/log updates.
- **Frontend:** a single self-contained SPA, `templates/index.html` (~4k lines,
  inline CSS + JS — no build step, no framework). Served by FastAPI.
- **AI:** local LM Studio via the `openai` client (`LMSTUDIO_BASE_URL`, default
  `http://127.0.0.1:1234/v1`). Built-in OCR via **RapidOCR** (onnxruntime).
- **Spreadsheet:** `openpyxl` (`spreadsheet_theme.py`).
- **Packaging:** `Dockerfile` + `docker-compose.yml` + `docker-entrypoint.py`;
  `launch.sh` / `launch.bat` are the user-facing launchers.

## Key files (responsibilities)

| File | What lives here |
|---|---|
| `server.py` (~3k lines) | FastAPI app: all HTTP/SSE endpoints (66 routes), the background **worker** that drains the queue, kanban/board state, results store, persistence, folder watching, model-management endpoints, settings endpoints. Imports the pipeline from `process_receipts`. |
| `process_receipts.py` (~1.9k lines) | The extraction **pipeline** and all model/OCR logic: OCR (RapidOCR + optional LLM OCR), distillation, vision rescue, offline regex parser, amount audit/reconcile, category classification, confidence scoring, dedup, image autocrop/grayscale/compress, file renaming, and `generate_spreadsheet`. Pure-ish module reused by server, watch_mode, scheduler. |
| `spreadsheet_theme.py` (~1k lines) | All openpyxl workbook building: Summary form, Insights charts, per-category image sheets, conditional formatting, autosize/fit, internal hyperlinks. |
| `templates/index.html` | The entire web UI (workspace + settings tabs, kanban board, review modal, dialogs, charts, SSE client). |
| `vendor_db.py` | Curated vendor → category lookup data/helpers. |
| `watch_mode.py` | Standalone watch-mode daemon (monitor inbox, process, email on schedule). `main()` entry. |
| `scheduler.py` | Weekly scheduled export/delivery. |
| `app_secrets.py` | Secrets store (SMTP password etc.) kept out of the main config. |
| `extras/receipt_gui.py` | A separate/legacy desktop GUI experiment — not the main app. |
| `tests/` | pytest suite (see Testing). |

## Processing pipeline (per receipt) — `process_receipts._extract_receipt_with_status`

Order matters (see `BLUEPRINT.md` §5). Current flow:

1. **Auto-rotate** (`autorotate_image_file`, EXIF → upright pixels) then **grayscale**
   then **autocrop** — all in-place and **BEFORE OCR** (canonical
   autorotate→greyscale→autocrop→OCR order, applied in the web-worker path too, not
   just the CLI batch path). A deeper **OCR-guided** rotation check runs inside the OCR
   step (below). Compression is deferred to export time.
2. **OCR (built-in, primary):** `_ocr_lines_best_orientation` → `_extract_local_ocr_lines`
   (RapidOCR), always runs — returns per-line **boxes + dims** (text via
   `_extract_local_ocr`, kept as a fallback for the engine-unavailable/test path).
   `_rapidocr_line_boxes` preserves the geometry `_rapidocr_lines` discards. On a weak
   upright read, `_ocr_lines_best_orientation` tries the three 90° rotations
   (`_ocr_orientation_score`) and rewrites the file to whichever reads best (logged as
   an `autorotate` step) — rules-based, no LLM.
3. **OCR (LLM, optional):** when `_active_ocr_model` is set, `_extract_raw_ocr`
   also transcribes via the vision LLM. `_combine_ocr_sources` then merges both
   transcriptions (labelled A/B) so the distillation model **cross-references**
   them. `_ocr_engine` becomes `rapidocr+llm` (vs `rapidocr` / `llm-ocr`).
4. **Distillation:** `_unified_distillation` → structured fields; `reconcile_amount`
   grounds the amount in the printed total.
5. **Vision rescue:** `_extract_with_model` only if OCR text is missing/low-confidence.
6. **Offline fallback:** `_local_distill_from_ocr` regex parser when the LLM is down.
7. **Field markup (rules-based, no LLM):** after a successful distill,
   `locate_field_boxes` maps the final vendor/date/amount back to the RapidOCR line
   that produced each (reusing the money/date/vendor matchers), stored normalized
   0..1 on `data["_field_boxes"]`. The amount box is computed **after**
   `reconcile_amount`, so it follows any correction onto the printed grand-total line.
8. Back in `server.py` worker: `classify_category`, `audit_amount`, confidence,
   review/approval defaults, job-field defaults, rename, dedup.

**Reasoning is per stage** (`_thinking_body(budget, enabled=...)`):
- OCR pass → **always** `enabled=False`.
- Distillation/vision → follow the global toggle `_thinking_enabled` (default **True**),
  set via `POST /models/thinking`, persisted as `thinking_enabled` in config.

## Models & settings

- Active models are module globals in `process_receipts`: `_active_ocr_model`
  (empty = no LLM OCR), `_active_distill_model` (auto-selected at startup by
  `initialize_models`).
- Endpoints: `GET /models/available`, `GET /models/lmstudio`, `POST /models/distill`,
  `POST /models/ocr`, `POST /models/thinking`.
- UI selectors live in the Settings tab; `loadModels()` populates them and they
  **auto-refresh** (on opening Settings + every 30s while Settings is visible,
  unless a dropdown is focused).

## Job-field placeholders

When a batch has no job name/number, receipts are stamped with the literal
constants `DEFAULT_JOB_NAME` = `"Default Job Name"` and `DEFAULT_JOB_NUMBER` =
`"Default Job Number"` (defined in `process_receipts.py`, imported by `server.py`).
Applied at: `process_receipts.py` batch path (`job_name_default or DEFAULT_JOB_NAME`),
`server.py` worker (`item.get("job_name") or DEFAULT_JOB_NAME`), and the
manual-receipt endpoint. The form's autocomplete only saves *real* (non-blank)
user input, never the placeholder.

## Review & approval

- Review modal: `openManualReview(filename)` in `index.html`. Saves/approves via
  `POST /results/add-manual` (or `POST /results/set-approval`).
- **Approve-and-next sweep:** the modal shows a remaining counter; the approve
  button reads "Approve & Next" while more completed receipts need approval and
  auto-opens the next one (`_unapprovedCompleted()` / `_loadNextForApproval()`).
- Approval gate (when `require_approval` on) blocks `POST /generate` both in UI
  and server-side until all completed receipts are approved.

## Report history

- `GET /reports` lists `Reimbursements_*.xlsx` in `OUT_FOLDER`, `GET /reports/download`
  serves one, **`POST /reports/clear`** deletes all `Reimbursements_*.{xlsx,csv}`
  (scoped glob only; never images). UI: Report History card "Clear History" button
  → `loadReports()` refresh.

## Config / state / paths

- `OUTPUT_FOLDER` (default `output/`), `RECEIPTS_FOLDER` (default `receipts/`).
- Config: `output/.app_config.json` (`CONFIG_FILE`, `_load_config`/`_save_config`).
- Crash-safe state: `output/.app_state.json` (`STATE_FILE`, `_persist_state` /
  restore on startup — completed/failed results + board survive restarts).
- Secrets: `.app_secrets.json` via `app_secrets.py`.
- `APP_VERSION` from `BUILD_TAG` env (fallback date string in `process_receipts.py`).

## Testing

- Run: `python -m pytest -q` (from repo root). Currently **323 tests, all green**.
- Install deps once: `pip install -r requirements-test.txt` (lightweight — the
  RapidOCR/onnxruntime stack is **mocked** in tests, not installed).
- `tests/conftest.py` autouse fixture redirects config/state/secrets to a temp dir
  per test (mark `no_path_isolation` to opt out).
- Pipeline tests mock `_extract_local_ocr` / `_unified_distillation` /
  `_extract_with_model` and assert on the per-step log (`step` keys like
  `local_ocr`, `llm_ocr`, `cross_reference`, `distillation`, `vision`).
- `tests/test_new_features.py` covers per-stage reasoning, dual-OCR cross-ref,
  job defaults, and clear-reports.

## Conventions / gotchas

- The frontend is one big file with **no build**; edit `templates/index.html`
  directly. Watch for duplicate element IDs (there's a UI-layout test).
- Receipt record dicts use `_`-prefixed internal fields (`_file`, `_new_filename`,
  `_category`, `_approved`, `_review_required`, `_confidence`, `_ocr_engine`,
  `_raw_ocr`, `_steps`, `_proc_seconds`, `_field_boxes`, …). User-facing fields are
  unprefixed. `_field_boxes` = `{vendor|date|amount: [x,y,w,h]}` normalized 0..1 to
  the OCR image; must be added to `_safe_receipt_data`'s whitelist to reach the UI.
- Compression is **deferred to export time** (`generate_spreadsheet`), never per
  receipt — keep OCR reading full-res images.
- **Batch concurrency:** `MAX_PARALLEL_REQUESTS` (default **3**, env-overridable)
  caps the worker's `ThreadPoolExecutor`. The local LM Studio model is the
  bottleneck — an unbounded pool times out and silently falls back to the offline
  parser. Raise only with a parallel-capable LM Studio + VRAM headroom.
- Don't send receipt content to any cloud service. Only outbound call is the local
  model endpoint.
- Module-level model globals persist across tests; monkeypatch them, don't set
  raw (some tests rely on `_active_ocr_model == ""`).

---

## Recent changes (append newest at top)

- **2026-06-15 (auto-crop control + preview):** Surfaced and made auto-crop
  testable — `tests/test_autocrop_endpoint.py` (+5) and analyze tests in
  `tests/test_autocrop.py` (+5).
  * **Refactor** — detection logic extracted into `autocrop_analyze(img)` (single
    source of truth returning `{bbox, kept_ratio, would_crop, reason}`);
    `autocrop_receipt` is now a thin apply step over it. Behavior unchanged.
  * **`POST /debug/autocrop-test`** — uploads an image, returns before/after dims,
    the crop decision + human-readable reason, and a JPEG preview data URL
    (mirrors `/debug/ocr-test`).
  * **UI** — the **auto-crop toggle** is now exposed in Settings → Image
    Processing (`proc-autocrop`; the `/settings/processing` backend already
    supported it but the SPA never sent it), plus a **"Test Auto-crop"** button
    that shows the original vs. cropped side-by-side with the decision. Honors the
    enabled flag (shows a "preview only" note when off).
- **2026-06-15 (usability & SSE efficiency):** `tests/test_sse_stream.py` (+2 tests).
  * **Snappier, leaner live board** — the `/events` SSE loop decoupled its poll
    cadence from its keep-alive: `SSE_POLL_SECS` (0.25s) delivers real board/log
    events ~4× faster while `SSE_HEARTBEAT_SECS` (15s) cuts idle keep-alive frames
    ~15×. Previously both were a single 1s `asyncio.sleep`, so a queued event
    could wait up to a full second. Both env-overridable.
  * **Keyboard-driven review sweep** — in the review modal, `Ctrl/⌘+Enter` runs
    the primary action (Approve & Next on a completed receipt, else Save) and
    `Ctrl/⌘+S` saves, reusing the existing button handlers; a `.mr-kbd-hint`
    line under the buttons makes them discoverable. Lets a reviewer clear a whole
    batch without the mouse.
  * **Step-log stays open across live ticks** — `moveCard` now carries the
    `.k-step-log.open` state into the rebuilt card (`makeCard`'s new
    `stepLogOpen` arg), so a card opened to watch progress no longer snaps shut
    on every `ocr`→`distilling`→`done` status update.
- **2026-06-15 (edge-case hardening):** Defensive safeguards so one malformed
  input can't crash the pipeline, poison totals, or leak a file —
  `tests/test_edge_hardening.py` (+30 tests). Changes:
  * **LLM JSON parsing** — extracted one hardened `_parse_llm_record` (replaces
    the two duplicate `_parse` closures in `_unified_distillation` /
    `_extract_with_model`). Now returns `None` for valid-but-non-object replies
    (`null`, `[]`, a bare number/string) instead of raising on `result["flags"]`,
    so the retry / offline fallback takes over cleanly.
  * **Config load** — `_load_config` only returns `dict`; a hand-corrupted
    config (`null` / list / number) no longer crashes every `.get()` caller.
  * **Non-finite amounts** — `/results/update` rejects `inf`/`nan` (400) and
    `/results/add-manual` coerces them to `0.0`; a `NaN` would otherwise serialise
    to invalid JSON and break the SSE feed + persisted state the browser reads.
  * **Symlink-safe previews** — `GET /receipt-image` now serves only real files
    that resolve inside the working folders (`_serveable`), blocking a planted
    symlink from turning the preview into an arbitrary-file read.
  * **Bounded rename collisions** — `rename_receipt_image` caps the numbered-suffix
    scan at 9999 then falls back to a random suffix (no more unbounded `while True`).
  * **Upload guards** — `/queue/add` skips empty (0-byte) files and ones over
    `MAX_UPLOAD_BYTES` (env, default 100 MiB) before staging them to disk.
- **2026-06-14 (autorotate):** **Auto-rotate to upright** (rules-based, no model) —
  `autorotate_image_file` bakes a photo's EXIF Orientation into the pixels before OCR
  (also fixes OCR-vs-browser orientation disagreement that would misalign the markup
  boxes); when the upright OCR read is weak, `_ocr_lines_best_orientation` tries the
  three 90° rotations and rewrites the file to whichever RapidOCR reads best
  (`_ocr_orientation_score`, logged as an `autorotate` step). Settings: `autorotate`
  toggle (`AUTOROTATE_ENABLED`; also `ORIENT_BY_OCR`/`ORIENT_MIN_SCORE`/
  `ORIENT_IMPROVE_RATIO` env knobs) wired through `/settings/processing` + the Image
  Processing card. Added `tests/test_autorotate.py`.
- **2026-06-14 (later):** **On-image field markup** — RapidOCR per-line boxes are
  now preserved (`_rapidocr_line_boxes`, `_extract_local_ocr_lines`) and the final
  vendor/date/amount are mapped back to the line that produced them by a rules-based,
  **LLM-free** `locate_field_boxes` (normalized `_field_boxes`, whitelisted in
  `_safe_receipt_data`). The review modal and full-screen lightbox draw colour-coded
  overlay boxes (`drawFieldBoxes`, `#mr-box-overlay`/`#lb-box-overlay`) with a legend
  + "Show field markers" toggle; fields that can't be located show a "location not
  detected" note. **Flow/concurrency tuning:** `MAX_PARALLEL_REQUESTS` default 0→**3**
  (avoids LLM timeouts → offline-parser fallback); autocrop now runs **before OCR in
  the web-worker path** (canonical order; keeps boxes pixel-aligned with the preview).
  Added `tests/test_field_markup.py` + box tests in `tests/test_local_ocr.py`.
- **2026-06-14:** Per-stage reasoning (OCR always off, distillation default on);
  dual built-in + LLM OCR cross-referenced by the distill model
  (`_combine_ocr_sources`, `_ocr_engine == "rapidocr+llm"`); approve-and-next
  review sweep with remaining counter; `POST /reports/clear` + Clear History UI;
  model-dropdown auto-refresh; job name/number placeholder defaults
  (`DEFAULT_JOB_NAME` / `DEFAULT_JOB_NUMBER`). Docs (BLUEPRINT/TUTORIAL) updated;
  added `tests/test_new_features.py`. Created this `CLAUDE.md`.
