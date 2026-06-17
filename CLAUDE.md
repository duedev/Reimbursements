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
  only drivers were *the end result + ease of use + low cost* (privacy, local-only,
  and even using an LLM all optional). Outcome-first and tech-agnostic — mandates
  no language, runtime, container, or model. Not the current architecture.

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
| `server.py` (~4k lines) | FastAPI app: all HTTP/SSE endpoints (82 routes), the background **worker** that drains the queue, kanban/board state, results store, persistence, folder watching, model-management endpoints, settings endpoints. Imports the pipeline from `process_receipts`. |
| `process_receipts.py` (~2.7k lines) | The extraction **pipeline** and all model/OCR logic: OCR (RapidOCR + optional LLM OCR), distillation, vision rescue, offline regex parser, amount audit/reconcile, category classification, confidence scoring, dedup, image autocrop/grayscale/compress, file renaming, and `generate_spreadsheet`. Pure-ish module reused by server, watch_mode, scheduler. |
| `spreadsheet_theme.py` (~1k lines) | All openpyxl workbook building: Summary form, Insights charts, per-category image sheets, conditional formatting, autosize/fit, internal hyperlinks. |
| `templates/index.html` (~5.4k lines) | The entire web UI (workspace + settings tabs, kanban board, review modal, dialogs, charts, SSE client). |
| `vendor_db.py` | Curated vendor → category lookup data/helpers. |
| `watch_mode.py` | Standalone watch-mode daemon (monitor inbox, process, email on schedule). `main()` entry. |
| `scheduler.py` | Weekly scheduled export/delivery. |
| `app_secrets.py` | Secrets store (SMTP password etc.) kept out of the main config. |
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

### Cloud LLM fallback chain (Gemini → Mistral → LM Studio)

- Extraction can fall back across multiple OpenAI-compatible endpoints, tried in
  order: **Gemini Flash-Lite → Mistral → local LM Studio**. A cloud provider is
  only tried when its API key is set; LM Studio is always the final fallback, so a
  keyless install behaves exactly as before. If every provider errors, callers fall
  through to the offline regex parser unchanged (the chain only changes *where* the
  model call goes).
- `process_receipts.make_llm_client()` builds the chain: returns the plain local
  client when no cloud provider is active, else a `_FallbackClient` whose
  `.chat.completions.create(...)` iterates providers, substituting each provider's
  own model and stripping LM-Studio-only params (`extra_body`/thinking,
  `frequency_penalty`) for cloud (`_sanitize_create_kwargs`). **The three extraction
  functions (`_unified_distillation`, `_extract_with_model`, `_extract_raw_ocr`) are
  unchanged** — the wrapper mimics the OpenAI client. The worker (`server._drain_once`)
  and `/watch/send-email` call `make_llm_client()`; warm-up still targets LM Studio
  only (no cloud quota burned).
- Config: cloud providers are module globals (`_CLOUD_PROVIDERS`, seeded from
  `GEMINI_API_KEY`/`GEMINI_MODEL`/`MISTRAL_API_KEY`/`MISTRAL_MODEL` env). Runtime:
  `configure_providers(specs)`, `provider_status()`, `active_provider_names()`.
  Endpoints `GET/POST /settings/llm-providers`; non-secret settings (model, enabled)
  persist in `cfg["llm_providers"]`, **API keys go to the secrets store**
  (`app_secrets`, never the cloud-syncable config). `_apply_provider_config()` restores
  on startup (in lifespan, before `initialize_models`). UI: "Cloud LLM Fallback"
  sub-card in the AI Models card (`loadProviders()` / `#providers-save-btn`).
- 429-aware backoff is the OpenAI SDK's built-in retry (`LLM_MAX_RETRIES`, honours
  Retry-After); once a provider is exhausted the chain moves to the next.

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

- Run: `python -m pytest -q` (from repo root). Currently **434 tests, all green**.
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

- **2026-06-17 (cloud LLM fallback chain — Gemini → Mistral → LM Studio):** Extraction
  can now fall back across multiple OpenAI-compatible providers instead of only the
  local LM Studio endpoint. `process_receipts.make_llm_client()` returns a
  `_FallbackClient` that mimics the OpenAI client (`.chat.completions.create`) and
  tries each active provider in order — substituting that provider's own model and
  stripping LM-Studio-only params for cloud (`_sanitize_create_kwargs`) — so the three
  extraction functions are **unchanged**. Cloud providers (`_CLOUD_PROVIDERS`, seeded
  from `GEMINI_*`/`MISTRAL_*` env) are only tried when their API key is set; LM Studio
  is always last, and an all-fail still drops to the offline parser. New
  `GET/POST /settings/llm-providers` (`configure_providers`/`provider_status`,
  `_apply_provider_config`/`_persist_provider_config`, restored in lifespan); **API
  keys persist in the secrets store**, model/enabled in `cfg["llm_providers"]`. The
  worker and `/watch/send-email` call `make_llm_client()`; warm-up stays LM-Studio-only.
  UI: "Cloud LLM Fallback" sub-card in the AI Models card (`loadProviders()`).
  429-aware backoff = the OpenAI SDK's built-in retry. `tests/test_llm_fallback.py`
  (+17). `.env.example` documents `GEMINI_API_KEY`/`MISTRAL_API_KEY`. Suite now **451**.

- **2026-06-16 (docs sync — no code changes):** Brought the Markdown docs back in
  line with the code (no behavior changed):
  * **CLAUDE.md** — refreshed the Key-files map (server.py ~4k lines / **82 routes**,
    process_receipts.py ~2.7k, index.html ~5.4k) and corrected the Testing line to
    **434 tests** (matched the changelog, which the Testing section still listed as 422).
  * **README.md** — removed the stale **Desktop GUI** (`receipt_gui.py` no longer
    exists in the repo); corrected `MAX_PARALLEL_REQUESTS` default 4→**3**; replaced
    the hard-coded **Threshold flags** section (fuel>$200/mats>$500/misc>$300 + 6-month)
    with the current **opt-in, off-by-default** Spending & Date Warnings; fixed the
    pipeline diagram's Validate box; updated the Models API (`/models/ocr` now
    `{enabled}`, added `/models/thinking`), the `/settings/processing` keys
    (autorotate, autocrop_aggressiveness, max_parallel), and added LLM-Server /
    Benchmarks / Audit / finish endpoint rows; Python requirement 3.12+→**3.11+**
    (CI tests 3.11 & 3.12).
  * **BLUEPRINT.md** — §5/§7 updated for the opt-in warnings (the baked-in
    thresholds/stale-date flags are gone).
  * **TUTORIAL.md** — Step 2 now describes the single **AI Model** + *"Also use this
    model for OCR"* toggle (no separate "OCR Model" dropdown post-consolidation).
  * **ADVISORY.md** — §6 note updated: `receipt_gui.py` was removed from the repo
    (not just moved to `extras/`).
  * **DESIGN_FROM_SCRATCH.md** — added the per-field zoomed review callouts to the
    "port straight over" review-UX list.

- **2026-06-16 (review/export/benchmark UX batch — 7 changes):**
  * **Confetti gated on a finished workload** — `batch_done` only fires `celebrate()`
  * **Confetti gated on a finished workload** — `batch_done` only fires `celebrate()`
    when nothing is left (`pending === 0` **and** no card is `queued`/`ocr`/`distilling`),
    so a batch that completes mid-run with more queued no longer triggers it early.
  * **Per-field magnified callouts in review** — the review modal now shows a zoomed
    slice of the receipt under each of vendor/date/amount (`.mr-callout` +
    `_renderFieldCallouts()`), built from `_field_boxes` (rules-based) and falling
    back to `_llm_field_boxes` (tagged `AI NN%`). The crop is uniformly scaled
    (no distortion) so the extracted value can be checked against the printed text
    at a glance. (LLM spatial boxes still draw dashed on the image when the vision
    path runs; the callout is the always-available aid since `_field_boxes` is set
    on every successful distill.)
  * **Benchmark insights** — new `_benchmark_insights(entries)` (server.py) rolls the
    per-batch log into totals, weighted avg/receipt, throughput (receipts/min), a
    recent-vs-overall trend, fastest/slowest batch, and a per-distill-model
    comparison; returned under `insights` by `GET /benchmarks` and rendered as stat
    tiles + bars above the table (`_renderBenchInsights`).
  * **Generate ⇄ Download swap** — the green "Generate Spreadsheet" button is replaced
    in-place by a "Download Spreadsheet" link once the workbook is built
    (`_swapToDownload`/`_swapToGenerate`; the old separate `#download-row` is gone,
    `#download-btn` now lives in `.gen-actions`). Any board change reverts to Generate
    (the prepared download is stale).
  * **Finish-batch tidy-up** — after a download, a dialog (`#finish-modal`) offers
    **Clear files** (delete) or **Keep in archive**. New `POST /results/finish`
    `{mode}` moves the completed receipt images into `ARCHIVE_FOLDER`
    (`output/archive`, **outside** the scanned working folders → never reported as
    orphaned) or deletes them, then clears the board. `_collect_orphans` also skips
    the archive defensively. `tests/test_finish_batch.py` (+5).
  * **Live concurrency slider** — the "process N at a time" slider now applies
    mid-batch. New `_ConcurrencyGate` (server.py) re-reads `MAX_PARALLEL_REQUESTS`
    on every acquire; the worker pool is sized to a fixed `CONCURRENCY_CEILING` (8)
    and each task is gated. `_apply_processing_config` calls `gate.bump()` so a raised
    cap wakes blocked workers immediately. `tests/test_concurrency_gate.py` (+3).
  * **Cards show old → new filename** — `makeCard` renders `original → renamed`
    (`.k-renamed`/`.k-fn-old`/`.k-fn-new`) when the pipeline renamed the file.
  * Tests: `tests/test_benchmark.py` (+4 insights). Suite now **434** green.

- **2026-06-16 (LLM connection — auto-detect / self-healing endpoint):** The
  durable fix for the recurring "app won't connect to LM Studio" report. Even
  after the docker-hostname fix, a stale saved choice (e.g. the **"Docker bundled
  server"** radio pinning the URL to `:11434` while LM Studio runs on `:1234`)
  was re-applied on every startup and could never self-recover. New seam in
  `server.py`:
  * `_probe_llm_url(url)` (urllib GET `{url}/models` → `(reachable, model_count)`),
    `_candidate_llm_urls()` (ordered/deduped: current URL first, then `127.0.0.1:1234`,
    `localhost:1234`, `host.docker.internal:1234`, the runtime-aware bundled
    `:11434`, and `host.docker.internal:11434`), `_autodetect_llm_url()` (first
    reachable, preferring one with a model loaded).
  * `_ensure_llm_reachable()` — startup safety net: if the configured endpoint is
    dead, adopt a working candidate **for the session only** (non-destructive; the
    persisted preference is left intact). Runs in a new `_startup_models()` wrapper
    that the lifespan thread calls before `initialize_models`.
  * `POST /llm-server/autodetect` — explicit recovery: probes, adopts, **and
    persists** the found URL as `llm_server={server_type:"custom",base_url:…}`,
    overwriting a bad saved choice so the fix sticks. UI: new **🔎 Auto-detect**
    button in the LLM Server card; `loadLMStudioModels()` also calls it silently
    (15s-throttled) whenever the configured URL reads unreachable, so the board
    reconnects on its own once LM Studio comes online.
  * **Bug fix:** `POST /llm-server/load` (and the new autodetect) wrapped
    `loop.run_in_executor(...)` (a Future) in `asyncio.create_task(...)`, which
    raises `TypeError` and 500s the call — the "Load Model" button never worked.
    Now scheduled fire-and-forget without `create_task`.
  * `tests/test_llm_autodetect.py` (+10). Suite now **422** green.

- **2026-06-16 (LLM connection fix — "docker" server-type stranding):** Root-caused
  the persistent "LM Studio won't connect" report. Selecting **"Docker bundled
  server"** in the LLM Server card or Configure Model dialog persisted
  `server_type: "docker"`, and `_apply_llm_server_config()` then forced
  `LMSTUDIO_BASE_URL = http://model-server:11434/v1` on **every startup**. The
  `model-server` hostname only resolves *inside* the docker-compose network, so a
  host-run app was permanently stranded (unreachable) even with LM Studio live on
  `127.0.0.1:1234` — and a restart re-applied the bad config. Fixes:
  * New `_in_docker()` seam + `_docker_llm_url()` helper (server.py): the "docker"
    server-type now resolves to `model-server:11434` only when actually inside
    Docker, else `127.0.0.1:11434` (the bundled server's published host port).
    Used in `_apply_llm_server_config` (both legacy `llm_model_config` and
    canonical `llm_server` keys) and `set_llm_server`. `/llm-server/status` reuses
    `_in_docker()`.
  * `set_llm_model_config` (Configure Model dialog) no longer calls
    `_apply_llm_server_config` — it only applies the model_id for the session, so
    the dialog can't silently overwrite a working URL (URL/server-type still defer
    to next startup, matching the dialog's wording).
  * `initialize_models` now logs `[models] LLM endpoint: <url>` so the tried URL is
    visible in the console.
  * UI: `loadLMStudioModels` shows the tried URL in the "unreachable" message + chip;
    `checkLLMStatus()` runs at page load (not just when Settings opens).
  * `tests/test_llm_server_url.py` (+10). Suite now 412 green.

- **2026-06-16 (polish batch — 6 changes):**
  * **Blue accent restored** — dark theme `:root` reverts to vivid `--accent: #3b82f6`
    (blue) + `--accent-2: #a855f7` (purple); added `--teal: #14b8a6` and `--rose:
    #fb7185`; `--ring` updated to `rgba(59,130,246,0.28)`; `body::before` gradient
    now uses blue/purple wash; logo-mark shadow, drop-zone drag-over bg, and
    `.btn-primary` box-shadow all updated from the old steel `rgba(111,143,166,…)`
    to the new blue `rgba(59,130,246,…)`.
  * **LLM URL normalization** — new `_normalize_llm_url(url)` helper (defined before
    `_apply_llm_server_config` in `server.py`) appends `/v1` if the user omits it.
    Used in `_apply_llm_server_config` when restoring `llm_model_config.base_url`
    and `llm_server.base_url`, and in `set_llm_server` for the `elif base_url` path.
  * **Audit card grid layout** — replaced the vertical flex stack with a
    `display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr))` 2-col
    grid; labels now show a small UPPERCASE category name + inline `$`/`max`/`days`
    adornments.
  * **Retry moves to next** — success path of the retry button handler now calls
    `_loadNextAny(fn)` before `_closeReviewModal()`, so the reviewer lands on the
    next receipt rather than the empty board.
  * **Spreadsheet link anchor** — in `_build_image_sheet`, `anchors.append` now
    points to a new 4pt-tall thin row inserted AFTER the header (between the header
    label and the receipt image), so Summary hyperlinks scroll directly to the image.
  * **Progress card collapsed by default** — `#progress-body` starts with
    `style="display:none"` and `#progress-toggle` starts with `class="section-toggle
    collapsed"` so the Processing & Errors section is hidden until the user opens it.

- **2026-06-16 (batch of 12 features):**
  * **Autocrop (Feature 1):** Default `AUTOCROP_AGGRESSIVENESS` raised from 70 to 85.
    Removed the accept/reject gate that blocked crops as "too aggressive" or "borders
    negligible" — crop now fires whenever the detected bbox is strictly smaller than
    the original. `tests/test_autocrop.py` updated (4 tests adjusted).
  * **LLM model config dialog (Feature 2):** New `POST /settings/llm-model` endpoint
    saves `{model_id, server_type, base_url}` to `cfg["llm_model_config"]`; applied
    by `_apply_llm_server_config()` at startup. Settings UI: "Configure Model" button
    opens a modal with model-ID input, docker/other radio, and base-URL input.
  * **Theme correction (Feature 3):** Confetti colors updated to vivid party colors
    (red, gold, green, blue, purple, orange). "LM Studio" labels → "LLM Server".
  * **Developer mode — card fields (Feature 4):** `makeCard()` wraps confidence
    badge, proc-time + OCR engine, step-log toggle, and step-log div in
    `class="dev-only"` — hidden when developer mode is off.
  * **Info tab (Features 5+6):** New "Info" nav tab (`#tab-info`) with About, Getting
    Started, Docker Commands (copy buttons), Keyboard Shortcuts, Pipeline Overview,
    and Tips.
  * **Remove review/approve buttons from cards; add Review All (Feature 7):**
    Done cards no longer show "Review & Approve" button (card body opens modal).
    Export card gained `#review-all-btn` + `#pending-review-count`.
  * **Review modal: Retry and Next buttons (Feature 8):** Modal footer has
    `#mr-retry-btn` (posts to `/retry-receipt`) and `#mr-next-btn` (`_loadNextAny()`).
  * **Notes field (Feature 9):** `notes` added to `_safe_receipt_data`, `_EDITABLE_FIELDS`,
    `ManualReceiptRequest`, `add_manual_result`. Review modal has `#mr-notes` textarea.
    Cards show truncated note indicator. `spreadsheet_theme.py` col H combines
    `data["notes"]` + `data["_flag"]`.
  * **Docker port 1234 → 11434 (Feature 10):** `model-server` service in
    `docker-compose.yml` moved to :11434. `Dockerfile.model` updated. `.env.example`
    comments updated. Non-docker LM Studio default unchanged.
  * **LLM server control buttons (Feature 11):** New endpoints `GET /llm-server/status`,
    `POST /llm-server/start/stop/restart/load`. UI: status dot + Start/Stop/Restart/
    Load/Refresh buttons in AI Models card.
  * **Server selection (Feature 12):** New `GET/POST /settings/llm-server` updates
    `_pr.LMSTUDIO_BASE_URL` immediately, persists under `cfg["llm_server"]`.
    `_apply_llm_server_config()` restores at startup. Docker/Custom radio + URL
    input in AI Models card. User-facing "LM Studio" → "LLM Server".

- **2026-06-16 (Docker: bundled LLM):** New `Dockerfile.model` (multi-stage:
  curl-fetch the GGUF + mmproj, bake into `ghcr.io/ggml-org/llama.cpp:server`) +
  a `model-server` compose service under profile `bundled-llm` serving an
  OpenAI-compatible API on :1234. App's `LMSTUDIO_BASE_URL` is now env-overridable
  (`${LMSTUDIO_BASE_URL:-http://host.docker.internal:1234/v1}`) so it can point at
  `http://model-server:1234/v1`. Weights are baked into the image (offline, but
  ~2-3 GB); model is swappable via `MODEL_URL`/`MMPROJ_URL` build args (default
  alias `qwen3-vl-2b-instruct`). `.env.example` + README "Bundled LLM" documented;
  README OCR note updated for the single-model consolidation. No code/tests changed.

- **2026-06-16 (synthetic receipt test-bench):** New `receipt_testkit.py` — a
  fixed suite of 9 challenge receipts (clean, rotated_90, faint_thermal,
  multi_total, us_date_ambiguous, noisy_scan, long_itemized, missing_vendor,
  big_amount), each a PIL-rendered image with known ground truth. `build_test_receipts`
  renders them; `score_extraction(truth, got)` scores vendor/amount/date/category
  (vendor=containment, amount=±0.01, weighted 0.3/0.4/0.2/0.1; blank-vendor rewards
  NOT fabricating); `run_benchmark(manifest, extract_fn)` aggregates and
  `format_scorecard` prints a table. CLI: `python receipt_testkit.py --out DIR [--run]`
  (`--run` drives the real pipeline to score the active LLM). Pure-PIL generator +
  scorer are LLM-free and unit-tested. `tests/test_receipt_testkit.py` (+7).

- **2026-06-16 (LLM spatial awareness — model-placed field boxes):** The vision
  path now also asks the model WHERE vendor/date/amount sit on the image, with a
  confidence. `_GEMMA_VISION_TEMPLATE` gained a `"boxes"` schema (fractional
  x,y,w,h 0..1 + confidence 0–100); `_normalize_llm_boxes` validates/clamps it and
  `_parse_llm_record` lifts it onto `data["_llm_field_boxes"]` (`{field:[x,y,w,h,conf]}`),
  whitelisted in `_safe_receipt_data`. UI `drawFieldBoxes(boxes, img, overlay, llmBoxes)`
  now draws the LLM boxes **dashed** with a `Label NN%` tag alongside the solid
  rules-based OCR boxes; legend notes AI-placed fields + confidence.
  `tests/test_llm_field_boxes.py` (+6). Note: only the vision/rescue path sees the
  image, so these boxes appear when the vision model runs (not on pure OCR-text
  distillation, which can't place coordinates).

- **2026-06-16 (auto-crop rewrite — edge-energy projection):** Replaced the
  corner-background content detection (which failed on gradients/shadows/busy
  desks — the "crop never fires no matter how aggressive" bug) with an
  **edge-energy projection** (`_content_bbox_by_edges`, numpy): per-row/col edge
  magnitude, content extent where each profile rises `frac` of the way from its
  median to its peak (`frac = threshold/100`, so the aggressiveness dial still
  controls tightness). `autocrop_analyze` keeps the same margin + accept/reject
  gating + reasons, and falls back to legacy `_content_bbox_by_corner_bg` only if
  numpy is unavailable. `tests/test_autocrop_robust.py` (+3); existing
  `tests/test_autocrop*.py` unchanged and still green.

- **2026-06-16 (spreadsheet: image above data):** In `_build_image_sheet`, the
  receipt picture is now embedded **above** its metadata row (was below), and the
  Summary→image hyperlink anchor points at the receipt's header row, so clicking a
  link lands with the image in view. Per-receipt order is now header → image →
  data → spacer. `tests/test_image_above_data.py` (+1).

- **2026-06-16 (Developer mode + gunmetal theme + review colour-coding):**
  * **Developer mode** — the old "Advanced settings" toggle is now "Developer mode"
    (same `#advanced-toggle` / localStorage `advancedMode` / `body.hide-advanced`
    mechanism). The CSS gate now also hides `.dev-only` elements, used for **enhanced
    workspace stats**: two dev-only insight tiles (Verified, Total Proc Time) + a
    `#dev-engine-line` technical summary (amount-verified ratio, dated-days, span,
    avg/total proc seconds), all driven from `/stats` in `updateStats`.
  * **Gunmetal dark theme** — retoned the default (`:root`) palette off the blue/
    purple hue to neutral graphite surfaces + a muted steel accent (`--accent
    #6f8fa6`). Swapped the accent-tinted `rgba(79,142,247…)`/`rgba(59,130,246…)`
    backgrounds to steel `rgba(111,143,166…)`, re-washed `body::before`, and moved
    the misc category / confetti colours off purple. Light theme untouched.
  * **Review-window colour coding** — the Vendor/Date/Amount inputs in the review
    modal are tinted to match their on-image `FIELD_MARKERS` boxes (vendor=blue,
    date=green, amount=amber): left-border + focus ring + a leading `.mr-fdot`
    swatch per label.

- **2026-06-16 (single AI model + auto-load + warm-up):**
  * **Consolidated to one model** — OCR and distillation now share a SINGLE active
    model. `process_receipts.set_active_model(id)` sets `_active_distill_model` and
    keeps `_active_ocr_model` in lock-step (= active model when LLM-OCR is on, else
    `""`). `set_llm_ocr(bool)` toggles the optional LLM-OCR cross-reference (reuses
    the one model — no separate OCR model). `_llm_ocr_enabled` global, default off.
  * **Auto-load + warm-up** — `initialize_models(warm=True)` now also `_try_load_model`s
    the chosen model into LM Studio memory, then `warm_up_model()` fires a tiny dummy
    receipt (`_WARMUP_OCR_TEXT`) through `_unified_distillation` so the first real
    batch isn't cold. Best-effort; skipped when LM Studio is unreachable.
  * **Persistence** — selection + OCR toggle persist under `cfg["models"]`
    (`_persist_model_config` / `_apply_model_config`, restored in lifespan BEFORE
    `initialize_models` so a saved choice survives restart).
  * **Endpoints** — `POST /models/distill` now sets the single model (persists);
    `POST /models/ocr` now takes `{enabled: bool}` (was `{model}`) to toggle LLM-OCR;
    `GET /models/available` adds `llm_ocr`. UI: one "AI Model" selector + an "Also use
    this model for OCR" checkbox (replaces the two dropdowns). `tests/test_model_consolidation.py` (+8).

- **2026-06-16 (bug fixes — date span + vendor default):**
  * **Spend-over-time duration** — the dashboard caption reported
    `timeline.length` (count of distinct *dated days*) as the duration, so a
    multi-year range read as "over 173 days". `_compute_stats` now also returns
    `timeline_span_days` = inclusive calendar distance between the first/last ISO
    date (full Y/M/D). `renderTimeline` uses it (with a local `_daySpan(isoA,isoB)`
    UTC fallback). `tests/test_timeline_span.py` (+5).
  * **Vendor no longer defaults to "Butchs Grinders"** — that string was a concrete
    example vendor in the distillation/vision `summary` examples; the model echoed
    it as the vendor when OCR couldn't read one. Both prompt templates now use
    generic category-level examples and an explicit rule: copy the printed vendor,
    else return `""` — never guess/invent/copy an example.
    `tests/test_vendor_prompt_hygiene.py` (+2).

- **2026-06-16 (advanced-mode toggle + LLM benchmark):**
  * **Advanced mode** — Settings has an "Advanced settings" toggle
    (`#advanced-toggle`, localStorage `advancedMode`, default OFF). When off,
    `body.hide-advanced .adv-only { display:none }` hides the deep-technical bits:
    the **AI Models** card, the image-processing internals (aggressiveness/JPEG
    sliders + Test OCR/Test image-processing buttons), the **Maintenance** card,
    and the **Benchmark** card. Folders/Scheduler/Email stay visible.
  * **Benchmark** — `_drain_once` times each batch and `_record_benchmark` logs
    `{ts,count,total_seconds,avg_seconds,distill_model,ocr_model}` (newest-first,
    capped `BENCH_MAX_ENTRIES=100`, persisted in `.app_state.json`). `GET
    /benchmarks` + `POST /benchmarks/clear`; a Benchmark settings card shows the
    table, refreshes on `batch_done`, and has Copy-as-CSV / Clear.
    `tests/test_benchmark.py` (+5).
- **2026-06-16 (customizable spending/date warnings, default off):** The old
  hard-coded fuel>$200 / mats>$500 / misc>$300 and "6-month window" flags were
  baked into the LLM prompts. Removed them from both templates and replaced with
  **opt-in, deterministic** rules: `AMOUNT_LIMITS` (per-category $ caps) +
  `MAX_RECEIPT_AGE_DAYS` in `process_receipts.py`, applied by
  `audit_warning_flags(data, category)` in the worker (prepended so a warning is
  the headline `_flag`). All **off by default**. New `GET/POST /settings/audit`
  (+ `_apply_audit_config` restored on startup) and a "Spending & Date Warnings"
  settings card (`#audit-card`, blank = off). `tests/test_audit_warnings.py` (+9).
- **2026-06-16 (concurrency slider + OCR labels + saved agent):**
  * **Batch concurrency** is now user-controllable: `max_parallel` added to
    `/settings/processing` (clamped 1..8 → `_pr.MAX_PARALLEL_REQUESTS`, applied on
    the next batch) with a compact slider at the top of the **Add Receipts** card
    (`#conc-slider`). Test in `tests/test_settings_endpoints.py`.
  * **OCR engine, in plain English** — `_ocrEngineInfo(engine)` maps the raw
    `_ocr_engine` (`rapidocr` / `rapidocr+llm` / `llm-ocr`) to "Built-in OCR" /
    "Built-in + LLM OCR" / "LLM OCR" with hover tooltips on the card and in the
    review modal.
  * **Persona persisted** — saved the Senior Developer agent to
    `.claude/agents/senior-developer.md` so it travels with the repo.
- **2026-06-16 (date normalization + cleanup):** `tests/test_date_normalize.py` (+~24).
  * **`normalize_date(raw)`** — dedicated, deterministic, **US-first** date
    normalizer (`process_receipts.py`): MM/DD/YYYY convention, two-digit years →
    2000s (`24`→2024, `99`→2099), accepts `-` `/` `.` separators, ISO passthrough,
    month-name forms; returns `''` when unparseable. Shared `_normalize_year` /
    `_iso_or_blank`; `_find_date_in_text` reuses `_normalize_year`. Wired into
    `_parse_llm_record` so every model date is canonicalised (raw kept if it can't
    parse). Both prompt templates now state the US month/day rule outright so the
    model stops guessing day/month order.
  * **Cleanup** — dropped the "JIT" wording from the `/models/*` docstrings;
    genericised the stale `google/gemma-4-12b-qat` default in README/TUTORIAL/
    ADVISORY (the code default is empty → auto-detect). `GEMMA_*` env-var names and
    the model-selection heuristic are unchanged.
- **2026-06-16 (aggressive auto-crop + series test):** Auto-crop is now a single
  `AUTOCROP_AGGRESSIVENESS` dial (0..100, default **70**) that `_autocrop_params`
  maps onto the four detection knobs (min-kept floor, max-kept ceiling, re-added
  margin, content threshold) — one slider moves the whole behaviour; the old
  fixed `AUTOCROP_MIN_RATIO`/`MAX_RATIO`/`MARGIN`/`_AUTOCROP_THRESHOLD` constants
  are gone. `autocrop_analyze(img, aggressiveness=None)` takes the dial.
  * Settings → Image Processing **reordered to app-flow order** (1 auto-rotate →
    2 b&w → 3 auto-crop + **Aggressiveness slider** → 4 OCR → 5 compress) and the
    per-step "Test Auto-crop" replaced by one **"Test image processing →"** button
    → `POST /debug/process-test`, which runs auto-rotate→b&w→auto-crop→compress in
    series and shows original vs final + a per-step before/after (proves crop and
    rotate compose). `autocrop_aggressiveness` added to `/settings/processing`.
  * Tests: `tests/test_autocrop.py` (+4) and `tests/test_autocrop_endpoint.py` (+6).
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
