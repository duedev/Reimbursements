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

1. **Grayscale pre-pass** then **autocrop** (autocrop is applied later in the batch path; compression is deferred to export time).
2. **OCR (built-in, primary):** `_extract_local_ocr` (RapidOCR), always runs.
3. **OCR (LLM, optional):** when `_active_ocr_model` is set, `_extract_raw_ocr`
   also transcribes via the vision LLM. `_combine_ocr_sources` then merges both
   transcriptions (labelled A/B) so the distillation model **cross-references**
   them. `_ocr_engine` becomes `rapidocr+llm` (vs `rapidocr` / `llm-ocr`).
4. **Distillation:** `_unified_distillation` → structured fields; `reconcile_amount`
   grounds the amount in the printed total.
5. **Vision rescue:** `_extract_with_model` only if OCR text is missing/low-confidence.
6. **Offline fallback:** `_local_distill_from_ocr` regex parser when the LLM is down.
7. Back in `server.py` worker: `classify_category`, `audit_amount`, confidence,
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

- Run: `python -m pytest -q` (from repo root). Currently **264 tests, all green**.
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
  `_raw_ocr`, `_steps`, `_proc_seconds`, …). User-facing fields are unprefixed.
- Compression is **deferred to export time** (`generate_spreadsheet`), never per
  receipt — keep OCR reading full-res images.
- Don't send receipt content to any cloud service. Only outbound call is the local
  model endpoint.
- Module-level model globals persist across tests; monkeypatch them, don't set
  raw (some tests rely on `_active_ocr_model == ""`).

---

## Recent changes (append newest at top)

- **2026-06-14:** Per-stage reasoning (OCR always off, distillation default on);
  dual built-in + LLM OCR cross-referenced by the distill model
  (`_combine_ocr_sources`, `_ocr_engine == "rapidocr+llm"`); approve-and-next
  review sweep with remaining counter; `POST /reports/clear` + Clear History UI;
  model-dropdown auto-refresh; job name/number placeholder defaults
  (`DEFAULT_JOB_NAME` / `DEFAULT_JOB_NUMBER`). Docs (BLUEPRINT/TUTORIAL) updated;
  added `tests/test_new_features.py`. Created this `CLAUDE.md`.
