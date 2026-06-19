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
| `server.py` (~4k lines) | FastAPI app: all HTTP/SSE endpoints (87 routes), the background **worker** that drains the queue, kanban/board state, results store, persistence, folder watching, model-management endpoints, settings endpoints, and the **run-log** capture (`_begin_run`/`_record_run_receipt`/`_finalize_run`, `_emit_log`). Imports the pipeline from `process_receipts`. |
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

**Reasoning is OFF** (`_thinking_body(budget, enabled=...)`): `_thinking_enabled`
defaults **False** and there is **no UI toggle** any more — the OCR pass never
reasons and distillation/vision run faster (and usually just as accurately)
without it. The `POST /models/thinking` endpoint still exists (programmatic/test
use) but nothing in the app turns reasoning on.

## Models & settings

- Active models are module globals in `process_receipts`: `_active_ocr_model`
  (empty = no LLM OCR), `_active_distill_model` (auto-selected at startup by
  `initialize_models`).
- Endpoints: `GET /models/available`, `GET /models/lmstudio`, `POST /models/distill`,
  `POST /models/ocr`, `POST /models/thinking`.
- UI selectors live in the Settings tab; `loadModels()` populates them and they
  **auto-refresh** (on opening Settings + every 30s while Settings is visible,
  unless a dropdown is focused).

## LLM provider (local server vs. OpenRouter cloud)

- **One canonical key `provider`** in config (`"local"` default, or `"openrouter"`).
  `_apply_llm_server_config(cfg)` dispatches: `_apply_local_llm_config` (LM Studio /
  custom URL / bundled docker, via `llm_server` + legacy `llm_model_config`) or
  `_apply_openrouter_config` (cloud). Run BEFORE `initialize_models` at startup.
- **Client seam:** `process_receipts.make_client()` is the SINGLE OpenAI-client
  factory — reads `LMSTUDIO_BASE_URL` + `LLM_API_KEY` (+ `LLM_EXTRA_HEADERS`). No
  call site hard-codes `api_key="lmstudio"` any more. For OpenRouter the base URL is
  `OPENROUTER_BASE_URL` and the key is the user's (secret `openrouter_api_key`).
- **OpenRouter auto-pick:** `_openrouter_free_vision_models()` filters the catalogue
  to free (zero prompt+completion price) + image-capable, ranks by family → quick
  (small/fast variants) → context; `_openrouter_autopick()` returns the best id.
  Endpoints: `GET/POST /settings/llm-provider`, `GET /models/openrouter`.
- **Free router default `openrouter/free`** (`OPENROUTER_FREE_ROUTER`): the default
  OpenRouter model is the free router meta-model (OpenRouter auto-selects among free
  models per request). It's STEERED via `process_receipts.LLM_EXTRA_BODY` — merged
  into every completion call — to `{"provider": {"sort": "throughput",
  "allow_fallbacks": True}, "models": [<quick-first free vision fallbacks>]}` so it
  prefers quick, reliable, image-capable models. `model="auto"` instead uses our own
  single best pick; an explicit id pins one model.
- **Privacy gate `LLM_ALLOW_IMAGE`** (process_receipts): when False the LLM-OCR pass
  and the vision rescue are skipped so the receipt IMAGE is never transmitted —
  OpenRouter's "send OCR text only" mode. "send receipt image" keeps full accuracy.
- **The "stuck on Docker URL" fix:** the frontend no longer silently calls
  `/llm-server/autodetect` (that used to persist the docker URL over a custom one);
  an explicit `server_type:"custom"` is honoured even with a blank URL (→ localhost,
  never docker); `GET /settings/llm-server` returns the *configured* URL + a separate
  `effective_base_url` so the UI shows the user's own choice.
- **Advanced processing tunables** (previously env-only) are now in `/settings/processing`
  and Settings → Image Processing → *Advanced tuning*: `llm_timeout`,
  `llm_max_retries`, `store_max_px`, `pdf_max_pages`, `max_upload_mb`.

> **Single cloud path = OpenRouter.** The old multi-provider Gemini → Mistral →
> LM Studio fallback chain was removed (it duplicated the no-cost goal that the
> OpenRouter free router already meets autonomously). There is now exactly one
> cloud option — OpenRouter — selected via the `provider` key above; everything
> goes through `make_client()`. There is no `make_llm_client`, `_CLOUD_PROVIDERS`,
> `_FallbackClient`, `/settings/llm-providers`, or per-provider keys any more.

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

## Transparency & run log ("what gets sent" + per-run detail)

Goal: surface **all** processing detail and **exactly what instructions are sent**
to the model — nothing hidden or clipped.

- **`GET /settings/llm-instructions`** → `_llm_instructions_payload()`: a live,
  self-documenting snapshot of what the app sends to the LLM for the active
  provider — provider/endpoint/model, the privacy gate (`send_image`), reasoning
  toggle, OpenRouter `extra_headers` + routing `extra_body`, and the **full system
  + user prompt for each pipeline stage** (OCR transcription `OLMOCR_RAW_PROMPT`,
  distillation `_UNIFIED_DISTILLATION_TEMPLATE`, vision rescue `_GEMMA_VISION_TEMPLATE`).
  UI: the OpenRouter card's collapsible **"Instructions sent to the model"** panel
  (`toggleInstr()` / `_renderInstructions()`) renders it in scrollable `.instr-pre`
  blocks (never truncated) — the fix for "the text gets cut off".
- **Run log = one record per batch ("run").** `_begin_run(batch)` opens it (embeds
  the instructions snapshot); **every `type:"log"` broadcast is auto-captured** into
  the active run by a hook inside `_broadcast` (so all ~20 log call sites feed it
  with no per-site change), capped at `RUN_MAX_LINES`. `_record_run_receipt()` adds
  each finished receipt (filename→renamed, status, fields, confidence, **full step
  list incl. image-processing**) AND streams the per-step breakdown into the live
  log via `_emit_log(msg, level=…)`. `_finalize_run()` pushes it onto `_runs`
  (newest-first, capped `RUNS_MAX_ENTRIES=25`, persisted in `.app_state.json`);
  `_abort_current_run()` salvages a partial run on worker crash.
- **Endpoints:** `GET /runs` (summaries), `GET /runs/{id}` (full detail),
  `GET /runs/{id}/download` (plain-text report via `_format_run_text`),
  `POST /runs/clear`. `batch_done` now carries `run_id`.
- **UI:** the **Run Log** sub-section lives inside the **Processing & Errors** card
  (`#runlog-section`) — a run picker + detail view (`loadRuns()`/`_showRun()`/
  `_renderRunDetail()`) showing the run header, a collapsible instructions panel,
  the full streamed log, and a per-receipt step breakdown (reuses `renderSteps`),
  with Download/Refresh/Clear. Refreshes on `batch_done` and on page load.
- **Image-processing steps are logged.** `_extract_receipt_with_status` now records
  `exif_rotate` / `grayscale` / `autocrop` steps (when each actually changes the
  file; autocrop shows before→after dims) so the card step-log, the run log, and the
  live Processing & Errors stream all show what was done to the picture before OCR.
- **The same stream feeds both places** — `#log` (Processing & Errors) and the run
  record are the *same* `type:"log"` events, so "route the log into Processing &
  Errors" is satisfied by construction. The curated **Errors** panel still filters
  to genuine error *reasons* (so the verbose per-step dump doesn't flood it).
- Tests: `tests/test_run_log.py` (+17).

## Config / state / paths

- `OUTPUT_FOLDER` (default `output/`), `RECEIPTS_FOLDER` (default `receipts/`).
- Config: `output/.app_config.json` (`CONFIG_FILE`, `_load_config`/`_save_config`).
- Crash-safe state: `output/.app_state.json` (`STATE_FILE`, `_persist_state` /
  restore on startup — completed/failed results + board survive restarts).
- Secrets: `.app_secrets.json` via `app_secrets.py`.
- `APP_VERSION` from `BUILD_TAG` env (fallback date string in `process_receipts.py`).

## Testing

- Run: `python -m pytest -q` (from repo root). Currently **496 tests, all green**.
- Install deps once: `pip install -r requirements-test.txt` (lightweight — the
  RapidOCR/onnxruntime stack is **mocked** in tests, not installed).
- `tests/conftest.py` autouse fixture redirects config/state/secrets to a temp dir
  per test (mark `no_path_isolation` to opt out).
- Pipeline tests mock `_extract_local_ocr` / `_unified_distillation` /
  `_extract_with_model` and assert on the per-step log (`step` keys like
  `local_ocr`, `llm_ocr`, `cross_reference`, `distillation`, `vision`).
- `tests/test_new_features.py` covers per-stage reasoning, dual-OCR cross-ref,
  job defaults, and clear-reports.
- `tests/test_llm_provider.py` covers the provider rework: the "stuck on Docker URL"
  regression, OpenRouter free/vision filtering + auto-pick, provider dispatch/apply,
  the `/settings/llm-provider` + `/models/openrouter` endpoints, and the
  `LLM_ALLOW_IMAGE` privacy gate.

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

- **2026-06-19 (AI Model section rework + benchmark steps + scan-app import):** Suite
  **483 → 496** green.
  * **Unified mode selector** — the AI Model card replaced the two separate radio
    groups (Provider local/openrouter **and** LLM Server custom/docker) with ONE 3-way
    **"Where the model runs"** selector: **On-host LLM** / **Docker bundled LLM** /
    **OpenRouter**. The shared **Server URL** field auto-populates from the choice —
    editable on-host (LM Studio default), read-only + auto-filled for docker
    (`_docker_llm_url()`) and OpenRouter (`openrouter.ai/api/v1`). Frontend-only:
    `_currentMode()` / `_applyModeUI(mode)` drive section visibility + URL state and map
    the 3 modes onto the existing `/settings/llm-provider` + `/settings/llm-server`
    endpoints (no backend change). `loadLLMProvider` derives the mode from
    `provider` + `local.server_type`.
  * **"OpenRouter shows no calls" root cause + guard** — the run log had `provider=local`
    but `endpoint=openrouter.ai`: a cloud URL pasted into the local custom field, so
    `make_client()` authed with the dummy `"lmstudio"` key (no attribution headers / no
    routing body) → every request 401s before it counts as a call → silent offline-parser
    fallback. The mode rework prevents it (URL read-only + key wired in OpenRouter mode);
    `_updateHostUrlHint()` also warns when a cloud URL is detected in On-host mode.
  * **Docker controls hidden unless docker** — Start/Stop/Restart/Load (`#llm-docker-controls`,
    which shell out to `docker compose` and fail elsewhere) now only show in docker mode.
    Status + Auto-detect + Refresh split into `#llm-conn-row` (on-host & docker).
  * **"None" model option** — the local model dropdown always offers **None** (value
    `""`) = built-in OCR + offline parser, no LLM. `_unified_distillation` /
    `_extract_with_model` short-circuit (return None, no API call) when no model is set;
    `_distill_text` logs "no AI model selected — built-in OCR + offline parser"; vision
    rescue is skipped. The dropdown change handler now allows the empty value.
  * **Reasoning removed** — `_thinking_enabled` default **True → False**; the Reasoning
    checkbox + listener are gone from the UI (endpoint kept for tests). See the
    "Reasoning is OFF" note above.
  * **Loaded-models list scrollable** — `.model-strip` capped at `max-height:168px` +
    `overflow-y:auto` (design must: a long loaded list can't blow out the card/page).
  * **Benchmark: all steps + CSV download** — `_record_benchmark(count, seconds,
    receipts)` now stores a per-step time breakdown via `_aggregate_step_durations`;
    `_benchmark_insights` adds `step_totals` (time-by-step across all batches, slowest
    first). New `GET /benchmarks/download` (`_benchmarks_csv`) streams a long-format CSV
    (one row per batch-step, incl. failures) — UI **⬇ Download CSV** button + a per-batch
    step sub-row + a "Time by step" insights chart.
  * **Scan-app (CamScanner) guided import** — `POST /settings/processing/preset {preset}`
    (`_PROCESSING_PRESETS`: `scanned`/`camscanner` = auto-crop **off** since scan apps
    already crop/de-skew/enhance, auto-rotate + B&W on; `photo` = full chain @ aggr 85).
    Add-Receipts card gains an **"Import from a scan app"** button → `#camscanner-modal`
    (best-export guidance + "apply scanned-document settings" checkbox + file picker that
    applies the preset then queues via the normal path). Also fixed `addFiles` to accept
    `.zip` (UI/server already did; the client filter dropped them).
  * Tests: `tests/test_ai_model_modes.py` (+11), benchmark steps/download in
    `tests/test_benchmark.py` (+5), `test_proc_time_stats` vision test now sets a model.

- **2026-06-19 (transparency: "what gets sent" + full per-run log + image-prep steps):**
  Suite **466 → 483** green. Answers "are you passing instructions?" (yes) and "I want
  all details" with end-to-end transparency.
  * **What gets sent** — new `GET /settings/llm-instructions` (`_llm_instructions_payload()`)
    returns the live system+user prompt for every stage (OCR / distillation / vision),
    the privacy gate, reasoning toggle, and OpenRouter routing headers/body. The
    OpenRouter card gained a collapsible **"Instructions sent to the model"** panel
    (`toggleInstr` / `_renderInstructions`) rendering it in scrollable `.instr-pre`
    blocks — **the fix for the cut-off text** (removed `white-space:nowrap` on the key
    status too).
  * **Run log** — one reviewable record per batch. `_begin_run` embeds the instructions
    snapshot; a hook in `_broadcast` auto-captures **every** `type:"log"` line into the
    active run (capped `RUN_MAX_LINES`); `_record_run_receipt` adds each receipt's full
    detail (incl. steps) and **streams the per-step breakdown into the live log** via
    `_emit_log(msg, level)`; `_finalize_run` pushes onto `_runs` (newest-first, capped
    `RUNS_MAX_ENTRIES`, persisted); `_abort_current_run` salvages on crash. Endpoints
    `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/download` (`_format_run_text`),
    `POST /runs/clear`; `batch_done` carries `run_id`.
  * **UI** — **Run Log** sub-section inside the **Processing & Errors** card
    (`#runlog-section`: picker + header + collapsible instructions + full log +
    per-receipt step breakdown, with Download/Refresh/Clear). Refreshes on `batch_done`
    and page load.
  * **Image-processing steps logged** — `_extract_receipt_with_status` now records
    `exif_rotate`/`grayscale`/`autocrop` steps (autocrop shows before→after dims) so
    image prep shows on the card, in the run log, and in the live stream.
  * **Same stream, both places** — `#log` and the run record are the identical
    `type:"log"` events ("route the log into Processing & Errors" is by construction).
    The curated Errors panel still filters to genuine error reasons.
  * **Theme** — restored blue where the gunmetal pass had swapped it to steel (besides
    the page background): pie/donut **misc** category `#8a93a0`→`#3b82f6`, the
    `.k-cat-misc` chip, and all `rgba(111,143,166,…)` element accent-tints →
    `rgba(59,130,246,…)` (timeline/vendor bars already used `--accent`/`--accent-2`).
  * Tests: `tests/test_run_log.py` (+17, incl. an end-to-end `_drain_once` capture).

- **2026-06-19 (merge main into dev + drop the Gemini/Mistral fallback chain):** Merged
  `origin/main` (which had independently added a Gemini → Mistral → LM Studio cloud
  fallback chain) into `dev`, then **removed that chain entirely** — the OpenRouter free
  router already meets the no-cost goal autonomously, so the multi-provider chain was
  redundant. There is now **one** cloud option: OpenRouter, via the `provider` key, with
  everything routed through `process_receipts.make_client()`.
  * **process_receipts.py** — deleted `make_llm_client`, `_CLOUD_PROVIDERS`,
    `_CLOUD_SAFE_PARAMS`, `_active_cloud_providers`, `configure_providers`,
    `provider_status`, `active_provider_names`, `_sanitize_create_kwargs`,
    `_FallbackCompletions`/`_FallbackChat`/`_FallbackClient`, and the
    `GEMINI_*`/`MISTRAL_*` globals.
  * **server.py** — removed `_PROVIDER_ENV`, `_apply_provider_config`,
    `_persist_provider_config`, the `GET/POST /settings/llm-providers` endpoints, and
    the lifespan restore call. The worker (`_drain_once`) and `/watch/send-email` now
    call `make_client()` directly.
  * **UI** — removed the "Cloud LLM Fallback" sub-card, `loadProviders()`, and the
    `#providers-save-btn`/`#provider-chain`/`#gemini-*`/`#mistral-*` elements. The
    OpenRouter provider panel (`loadLLMProvider`) is unchanged.
  * **Docs/deploy** — `.env.example` and `CLAUDE.md` drop the chain; the Oracle free
    deploy (`DEPLOY_ORACLE.md` / `docker-compose.prod.yml`) now wires
    `OPENROUTER_API_KEY` instead of `GEMINI/MISTRAL` keys.
  * **Tests** — deleted `tests/test_llm_fallback.py` (the chain's 17 tests). Suite
    **483 → 466** green (merge union was 483; −17 chain tests).

- **2026-06-19 (LLM provider rework + OpenRouter + settings completeness + multi-user plan):**
  Suite **434 → 455** green. Branch consolidated to `dev` (one persistent dev branch
  instead of a new per-session branch; existing branches left untouched).
  * **Provider redesign + "stuck on Docker URL" fix** — one canonical config key
    `provider` (`local`/`openrouter`) dispatches in `_apply_llm_server_config` →
    `_apply_local_llm_config` / `_apply_openrouter_config`. The local path now honours
    an explicit `server_type:"custom"` even with a blank URL (→ `127.0.0.1:1234`, never
    the legacy docker fall-through that stranded users on `:11434`). The **frontend no
    longer silently POSTs `/llm-server/autodetect`** (the real culprit — it persisted the
    bundled docker URL over the user's custom one); recovery is the explicit button.
    `GET /settings/llm-server` now returns the *configured* URL + a separate
    `effective_base_url` so the UI shows the user's own choice. `set_llm_server` /
    autodetect also set `provider:"local"`.
  * **Client seam** — `process_receipts.make_client()` is now the single OpenAI-client
    factory (base_url + `LLM_API_KEY` + `LLM_EXTRA_HEADERS`); the hard-coded
    `api_key="lmstudio"` is gone from all 5 call sites (3 in server.py, 2 in
    process_receipts.py).
  * **OpenRouter cloud provider (opt-in, off by default)** — `OPENROUTER_BASE_URL`,
    secret `openrouter_api_key` (via `app_secrets`), `_openrouter_free_vision_models()`
    (free = zero prompt+completion price, image-capable; ranked by family/context) +
    `_openrouter_autopick()`. New endpoints `GET/POST /settings/llm-provider`,
    `GET /models/openrouter`. UI: AI Model card gains a **Provider** toggle
    (Local / OpenRouter) with an OpenRouter panel (key, model dropdown + Auto, send-mode
    radios, privacy note). **Privacy gate `LLM_ALLOW_IMAGE`** — "send OCR text only"
    suppresses the LLM-OCR + vision-rescue image passes so the receipt image never
    leaves the machine; "send receipt image" keeps full accuracy.
  * **Free router default (`openrouter/free`)** — the default OpenRouter model is the
    free router meta-model, STEERED via `LLM_EXTRA_BODY` (merged into every completion
    call) toward quick + reliable providers (`provider.sort:"throughput"`,
    `allow_fallbacks`) with a pinned quick-first free **vision** fallback `models` list
    so image requests never land on a text-only model. `_openrouter_score` now ranks
    family → quick (small/fast) → context. Suite **455 → 460**.
  * **Zero-click first-run OpenRouter** — `_first_run_provider_default()` (lifespan,
    before `_apply_llm_server_config`): when `OPENROUTER_API_KEY` is set in the env AND
    the config is fresh (no provider/llm_server/llm_model_config/openrouter keys), it
    persists `provider:"openrouter"` + the free-router default — never overriding an
    explicit choice. `_startup_models()` now **skips `initialize_models()` for the
    openrouter provider** (the local auto-select would otherwise clobber the
    `openrouter/free` slug) and best-effort pins the vision fallback list off-thread.
    Suite **460 → 466**.
  * **Settings completeness** — previously env-only tunables surfaced in
    `/settings/processing` + Settings → Image Processing → *Advanced tuning*:
    `llm_timeout`, `llm_max_retries`, `store_max_px`, `pdf_max_pages`, `max_upload_mb`
    (clamped + persisted). Remaining internal knobs (orientation thresholds, SSE
    intervals, stall timeouts, archive caps) intentionally stay env-only — noted in
    `ROADMAP.md`.
  * **Docs** — new `MULTIUSER.md` (plan-only multi-tenant design + phased migration)
    and `ROADMAP.md` (forward view; notes GitHub Projects/Milestones/Issues as native
    tracking options; past changelog stays here).
  * Tests: `tests/test_llm_provider.py` (+20), advanced-settings round-trip in
    `tests/test_settings_endpoints.py` (+1).

- **2026-06-17 (free cloud deploy — Oracle Always Free + Caddy):** Added a
  production deploy path for hosting the Docker image free, 24/7. `docker-compose.prod.yml`
  is an overlay (`-f docker-compose.yml -f docker-compose.prod.yml`) that adds a
  **Caddy** reverse-proxy service for automatic Let's Encrypt HTTPS in front of the
  app (only Caddy's 80/443 are public; the app stays on the internal compose network
  as `receipt-processor:8000`), forces `APP_AUTH_TOKEN` (`:?` guard), and wires the
  cloud LLM keys. `Caddyfile` proxies with `flush_interval -1` so SSE streams
  unbuffered. `DEPLOY_ORACLE.md` is the step-by-step for an Oracle Cloud Always-Free
  Ampere A1 (ARM) VM — build happens on the VM so aarch64 wheels are pulled natively;
  the LM Studio tier is inert in cloud (chain = Gemini → Mistral → offline parser).
  Docs/compose only — no app code or tests changed.

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
