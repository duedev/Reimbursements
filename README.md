# Receipt Processor

[![CI](https://github.com/duedev/Reimbursements/actions/workflows/ci.yml/badge.svg)](https://github.com/duedev/Reimbursements/actions/workflows/ci.yml)

> AI-powered receipt scanning, extraction, and reimbursement report generation — runs entirely on your hardware with [LM Studio](https://lmstudio.ai).

Drop in photos or PDFs of receipts. The app extracts the vendor, date, amount, and category using a local vision model, renames and organizes the files, tracks everything on a live Kanban board, and produces a formatted, print-ready Excel reimbursement form. No cloud APIs, no subscription fees — 100% local AI.

**New here or not technical?** Start with the [Tutorial](TUTORIAL.md) — it walks through Docker, LM Studio, first launch, and daily use in plain language.

For architecture notes, model selection guidance, and roadmap considerations, see the [Technical Advisory](ADVISORY.md).

---

## Features

- **Local AI only** — Uses LM Studio for all vision and language inference; nothing leaves your machine
- **Web UI with live Kanban board** — Real-time receipt status tracking (Queued → OCR → Distilling → Done/Failed) via Server-Sent Events
- **Batch & continuous modes** — Process a folder all at once, or let the watcher auto-queue new files as they appear
- **PDF support** — PDFs are automatically expanded to per-page images before processing
- **Zip support** — Drop (or drop into intake) a `.zip` of receipts and the app extracts the images/PDFs inside, queues them, and deletes the archive — handy for bulk uploads from a phone. Zip-slip / zip-bomb safe (caps on member count and decompressed size)
- **Black & white for OCR** — Optional pre-pass converts each receipt to high-contrast grayscale *before* OCR/AI for crisper text recognition; toggle in **Settings → Image processing**
- **Smart categorization** — Receipts classified as Fuel, Materials, or Miscellaneous based on vendor and content
- **Professional Excel output** — Themed, print-ready workbook with embedded receipt images, per-category subtotals, a grand total, and accounting-format amounts. Verified compatible with both **Microsoft Excel** and **macOS Numbers** (native charts, conditional formatting, internal hyperlinks, frozen headers)
- **Insights *in the workbook*** — A dedicated **Insights** sheet mirrors the web dashboard: KPI figures, a spend-by-category pie, top-vendor bars, and a detailed spend-over-time chart (daily columns + a cumulative trend line), all as native charts that open in Excel and Numbers
- **Review & approval gate** — Optional setting that blocks spreadsheet generation until every completed receipt has been reviewed and approved on the board (enforced client- and server-side)
- **Deferred image compression** — Receipts are stored at full resolution through OCR; JPEG compression/downscaling runs once, at spreadsheet-generation time, so the OCR stage always reads the sharpest image and the output folder + embedded images are optimized together
- **Persistent autocomplete** — Employee name, job name, and job number fields remember your last 20 entries
- **Category-prefixed filenames** — Processed images renamed to `fuel_12-30-24_shell.jpg` for instant sorting
- **Duplicate detection** — Same vendor/date/amount flagged automatically, with an exclude-from-report dialog
- **Amount verification** — In two-stage OCR mode, extracted amounts are cross-checked against money values printed on total-like lines of the raw OCR text; verified receipts get a ✓ badge, mismatches are flagged for review (pure regex, catches LLM hallucinations)
- **Insights dashboard** — Live spend analytics: total/average/flagged tiles, a category donut, an annotated spend-over-time chart (daily bars, cumulative line, average marker, peak callout), and top-vendor rankings (dependency-free SVG charts)
- **CSV export** — One click exports all completed receipts as a spreadsheet-ready CSV
- **Report history** — Browse and re-download every previously generated workbook
- **Maintenance tools** — Scan the working folders for orphaned (unreferenced) files — each reported with its full on-disk location — and one-click delete emptied job/temp folders. Empty orphaned folders are also swept automatically at the start of every session
- **Unsupported-file handling** — Anything copied into the intake folder that isn't an image, PDF, or `.zip` is moved into an `unsupported` quarantine folder and surfaced as a notification (with the reason, size, and location) and a one-click delete button — nothing is processed or silently dropped
- **Dated receipt folders** — Completed receipt images are grouped into short, dated subfolders (`output/receipts/Processed_<YYYY-MM-DD>/`) so each day's processed receipts stay together
- **Board search** — Filter receipt cards by vendor, filename, or category (press `/` to focus)
- **Inline editing** — Click any field on a completed card (vendor, date, amount, category, summary) to fix it in place; duplicate flags recompute automatically
- **Crash-safe persistence** — Completed and failed receipts are snapshotted to disk and restored on startup, so a server restart never loses a processed batch
- **Optional email delivery & scheduling** — Watch-mode daemon and a built-in weekly scheduler can email the report over SMTP or drop it into a synced cloud folder
- **Desktop GUI** — Standalone `customtkinter` app for users who prefer not to run a server
- **Installable PWA** — Add to home screen on mobile for a native-like experience

---

## Quick Start

### Option 1 — Docker (recommended)

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/), [LM Studio](https://lmstudio.ai) running locally with at least one vision-capable model loaded.

```bash
# 1. Clone the repo
git clone https://github.com/duedev/Reimbursements.git
cd Reimbursements

# 2. (Optional) Create a .env file to point at your actual receipt folders
cp .env.example .env
# Edit .env — uncomment and set INTAKE_PATH and OUTPUT_PATH

# 3. Start
./launch.sh          # macOS / Linux
# launch.bat         # Windows

# The browser opens automatically at http://localhost:8000
```

Without a `.env` file the app creates `./intake` and `./output` folders next to the project.

### Option 2 — Native Python

```bash
pip install -r requirements.txt
export LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1   # default
python -m uvicorn server:app --reload
# Visit http://localhost:8000
```

---

## LM Studio Setup

1. Download and launch [LM Studio](https://lmstudio.ai)
2. Load any **multimodal / vision model** (the app auto-detects whatever you load)
3. Start the **Local Server** in LM Studio (default port **1234**)
4. The app **auto-selects and loads** a model at startup and **warms it up** with a tiny dummy receipt so your first batch is fast

**One model, both stages:** OCR and extraction share a single model (pick it under Settings → AI Model). The built-in RapidOCR always runs locally; flip **"Also use this model for OCR"** to additionally have the AI model transcribe each receipt and cross-reference both readings — more accurate on hard receipts, but slower (two model calls each).

---

## Bundled LLM (no external LM Studio)

Prefer to run everything in one `docker compose up`? You can bundle a local model
**inside** the stack instead of installing LM Studio. The `model-server` service
(profile `bundled-llm`) bakes the weights into its image and serves an
OpenAI-compatible API the app talks to.

```bash
# In .env, set the model download URLs (a small Qwen3-VL-2B GGUF + its mmproj is
# recommended — find current links on Hugging Face), then:
LMSTUDIO_BASE_URL=http://model-server:1234/v1 \
  docker compose --profile bundled-llm up --build
```

- **Weights are baked into the image** → the container runs fully offline, but the
  image is large (~2–3 GB for a 2B vision model). Swap the model with the
  `MODEL_URL` / `MMPROJ_URL` build args (see `Dockerfile.model`).
- A **vision** model (GGUF + mmproj) is expected so the direct-image path and the
  on-image field-location boxes work.
- CPU-only by default; enable the commented GPU block in `docker-compose.yml`
  (needs the NVIDIA container toolkit) to offload layers.

---

## Usage

### Web Interface

Open `http://localhost:8000`.

#### Adding receipts

| Method | How |
|---|---|
| **Drag & drop** | Drag image files, PDFs, or a `.zip` onto the upload zone |
| **Browse** | Click the upload zone and select files |
| **Intake folder** | Drop files (images, PDFs, or `.zip` archives) into the configured intake folder; they appear in the queue automatically within 5 seconds |
| **Queue Intake Files** | Click the button to manually enqueue everything currently in the intake folder |

Click **Add to Queue** to start processing. You can keep adding files at any time — the queue drains continuously.

#### Kanban board

| Column | Meaning |
|---|---|
| **Queued** | Waiting for a worker slot |
| **Processing** | OCR and/or distillation actively running (model name shown) |
| **Completed** | Extraction succeeded; vendor, amount, date, and summary visible |
| **Failed** | Low-confidence extraction — click **↺ Retry** to re-queue |

Every card has a **×** dismiss button. **Clear Board** (appears once any cards exist) wipes everything and resets the queue.

#### Generating the spreadsheet

Once receipts reach the Completed column, a **Generate Spreadsheet** card appears. Click it to build and download the Excel workbook. The file is named `Reimbursements_EmployeeName_YYYY-MM-DD.xlsx`.

The card also has a **Require review & approval** checkbox. While it's on, generation is blocked (button disabled, and the server rejects the request) until every completed receipt has been approved via the **✎ Review & Approve** button on its card — the status line shows how many receipts still need review and updates live as you approve them. The Review & Approve dialog opens with a **large, zoomed view of the receipt right beside the editable fields and Approve button**, so you can verify and approve in one step (click the image to go full-screen).

---

### Watch-Mode Daemon

The watch-mode service runs as a background daemon that continuously monitors an inbox folder, processes any new receipts it finds, and can email the accumulated report on a schedule.

**Start alongside the web server:**

```bash
docker compose --profile watch up -d
```

**Or run standalone:**

```bash
python watch_mode.py
```

The daemon polls `WATCH_INBOX` every `WATCH_INTERVAL` seconds, moves processed images to `WATCH_STAGED`, and accumulates results in a JSON state file. Call `POST /watch/send-email` from the web UI to trigger an immediate email, or configure a cron job to do it on a schedule.

---

### Desktop GUI

For a fully offline, no-server experience:

```bash
pip install -r requirements.txt customtkinter
python receipt_gui.py
```

The GUI provides folder pickers, a live receipt thumbnail preview, extracted data display, and a progress log. It calls the same extraction pipeline as the server.

---

### CLI Batch Processing

```bash
python process_receipts.py \
  --receipts /path/to/receipt/images \
  --output-dir /path/to/output \
  --employee "Jane Smith" \
  --job-name "HQ Renovation" \
  --job-number "JB-2025-04"
```

Pass an optional Excel template as the first positional argument to base the output on your company's existing format.

---

## Configuration

### Docker volume paths (`.env`)

```bash
# Folder on your host machine that maps to the app's intake folder
INTAKE_PATH=/Users/yourname/Desktop/receipts

# Folder on your host machine for output files (Excel reports + processed images)
OUTPUT_PATH=/Users/yourname/Documents/reimbursements
```

### Environment variables

All variables have sensible defaults for local development.

#### Core processing

| Variable | Default | Description |
|---|---|---|
| `LMSTUDIO_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio server URL (use `host.docker.internal` inside Docker) |
| `RECEIPTS_FOLDER` | `receipts` | Intake folder path |
| `OUTPUT_FOLDER` | `output` | Output folder for Excel files and processed images |
| `MAX_PARALLEL_REQUESTS` | `4` | Concurrent receipt processing threads |
| `APP_AUTH_TOKEN` | *(unset)* | Shared-secret token required on every request when set. **Set this before exposing the app beyond `127.0.0.1`** (LAN, a phone, or a Cloudflare tunnel). Open the page once with `?token=<value>` — the token is then remembered and attached to image/stream/API requests, so the UI works across reloads and PWA relaunches on remote devices. |

#### AI models

| Variable | Default | Description |
|---|---|---|
| `GEMMA_SMALL_MODEL_ID` | _(empty → auto-detect)_ | Pin the distillation model; if unset the app auto-selects a loaded chat/vision model |
| `GEMMA_LARGE_MODEL_ID` | _(empty → auto-detect)_ | Pin a larger distillation model (optional) |
| `OLMOCR_MODEL_ID` | _(empty)_ | Pin an optional dedicated OCR model (e.g. a document-OCR model like `allenai/olmOCR-2-7B`) |

Model IDs are defaults only — use the in-app model selectors to switch without restarting.

#### Watch mode

| Variable | Default | Description |
|---|---|---|
| `WATCH_INBOX` | `/data/watch_inbox` | Folder to poll for new receipts |
| `WATCH_STAGED` | `/data/watch_staged` | Destination for processed receipt images |
| `WATCH_STATE` | `/data/watch_state` | JSON state persistence folder |
| `WATCH_INTERVAL` | `60` | Poll interval in seconds |
| `WATCH_EMPLOYEE_NAME` | `Duane Hamilton` | Employee name for watch-mode reports |

#### Email (optional)

> **You can now configure email entirely in the web UI** — open **Settings → Email
> delivery**, fill in your SMTP details, and click **Send test email** to verify.
> UI-saved settings take precedence over the environment variables below and need
> no restart. The variables remain as defaults / for the standalone watcher.

Leave `SMTP_HOST` empty (and the UI fields blank) to disable email entirely.

| Variable | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP port (default 587, TLS) |
| `SMTP_USER` / `SMTP_PASS` | SMTP credentials |
| `SMTP_FROM` | Sender address |
| `EMAIL_TO` | Recipient address(es), comma-separated |
| `EMAIL_SUBJECT` | Subject line (default: "Weekly Reimbursement Report") |

#### Image processing

Black-&-white conversion, JPEG compression, and the RapidOCR fallback are
toggleable in **Settings → Image processing** (no restart needed). The defaults
below apply until changed in the UI.

| Variable | Default | Description |
|---|---|---|
| `AUTOCROP_ENABLED` | `1` | Trim uniform background borders around each receipt |
| `GRAYSCALE_ENABLED` | `1` | Convert each receipt to high-contrast grayscale before OCR/AI (also applies to the stored image) |
| `COMPRESS_ENABLED` | `1` | Re-encode stored receipts to optimized JPEG |
| `JPEG_QUALITY` | `85` | Stored-image JPEG quality (40–95) |
| `STORE_MAX_PX` | `2000` | Cap the longest side of stored receipt images |
| `LOCAL_OCR_ENABLED` | `1` | Local CPU OCR fallback (RapidOCR) when LM Studio's OCR stage is down. The legacy `PADDLEOCR_ENABLED` name is still honored. |

#### UI folder shortcuts

| Variable | Description |
|---|---|
| `HOST_INTAKE_PATH` | Host-side path displayed in the UI for the intake folder |
| `HOST_OUTPUT_PATH` | Host-side path displayed in the UI for the output folder |

These are purely display values — they let the folder-shortcut buttons show your real host path and copy it to the clipboard when running inside Docker.

---

## How It Works

### Processing pipeline

```
Receipt image / PDF
        │
        ▼
  ┌─────────────┐
  │  Ingest     │  PDF → per-page JPEGs (PyMuPDF, 2× zoom); .zip → member images/PDFs
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Greyscale  │  Pipeline order: greyscale → autocrop → OCR/extraction → … → compress.
  │  + Autocrop │  Flatten to high-contrast grayscale (optional) so OCR/AI read
  │             │  crisper text, then trim uniform borders so the receipt fills the
  │             │  frame. The image is kept at full resolution here; compression is
  │             │  deferred (see the Spreadsheet step) so OCR always reads the
  │             │  sharpest image.
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Stage 1    │  PRIMARY — local RapidOCR transcribes all visible text on-device
  │  OCR        │  (fast, offline, no LLM). This is the default path for every
  │             │  receipt.
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Stage 2    │  OCR text → structured JSON via the LM Studio distillation model.
  │  Distill    │  Extracts: date, vendor, amount, category, summary, flags.
  │             │  If LM Studio is unreachable, a local regex parser turns the
  │             │  RapidOCR text into fields — cross-referencing a known-vendor
  │             │  database for the vendor name — and flags it for manual review.
  └──────┬──────┘
         │
         ├── No OCR text / low confidence? ──► Vision rescue: a vision-capable
         │      model reads the image directly; still failing ──► Failed
         │
         │
         ▼
  ┌─────────────┐
  │  Classify   │  Vendor-name lookup → fuel / mats / misc
  │  & Validate │  Apply amount thresholds; check date within 6-month window
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Rename &   │  fuel_12-30-24_shell.jpg  (category_MM-DD-YY_vendor.ext)
  │  Move       │  Saved to output/receipts/Processed_<YYYY-MM-DD>/
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Accumulate │  Added to _results list; duplicate detection runs across batch
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Compress   │  Now (at export) each stored image is re-encoded/downscaled to
  │  → Spreadsheet  an optimized JPEG, then the themed Excel workbook is built:
  │             │  Summary form + Insights charts + per-category image sheets.
  └─────────────┘
```

### Categorization

| Category | Matched vendors |
|---|---|
| **Fuel** | Shell, Chevron, Arco, Mobil, Exxon, BP, 76, Circle K, Pilot, Love's, Wawa, Casey's, and 30+ more |
| **Materials** | Home Depot, Lowe's, Menards, Ace Hardware, Harbor Freight, Fastenal, Grainger, and more |
| **Miscellaneous** | Everything else |

### Threshold flags

| Category | Flag above |
|---|---|
| Fuel | $200 |
| Materials | $500 |
| Miscellaneous | $300 |

Receipts dated more than 6 months ago are also flagged. Flagged receipts still appear in the spreadsheet — the Notes column turns red and the flag reason is shown.

### Spreadsheet layout

Each generated workbook contains five sheets. Every feature used — native charts,
conditional formatting, internal hyperlinks, frozen panes, accounting number
formats, and embedded images — renders in both **Microsoft Excel** and **macOS
Numbers**.

| Sheet | Contents |
|---|---|
| **Summary** | Formatted reimbursement form — employee name and expense period (label + value sit side-by-side), all receipts grouped by category with subtotals and a grand total. Columns are auto-fit to content width and rows grow to fit wrapped text. The `#` cell of each receipt links straight to its image on the category sheet |
| **Insights** | Mirrors the web dashboard: KPI figures (total, count, avg, flagged, verified, avg processing), a spend-by-category pie, top-vendor bars, and a detailed spend-over-time chart (daily columns + cumulative trend line) with a backing data table |
| **Fuel** | Embedded receipt images for fuel receipts |
| **Materials** | Embedded receipt images for materials receipts |
| **Miscellaneous** | Embedded receipt images for miscellaneous receipts |

**Summary sheet columns:**

| Col | Header | Notes |
|---|---|---|
| A | Receipt No. | Sequential within category; hyperlinks to the receipt image |
| B | Date | `m/d/yy` format |
| C | Store | Vendor name |
| D | Job Name | Centered |
| E | Job Number / Expense Desc | Job # for fuel/mats; expense description for misc |
| F | Amount | Accounting currency format |
| G | Summary | AI-generated one-liner, centered |
| H | Notes | Flag text, red background if flagged |

---

## API Reference

### Queue

| Method | Path | Body / Params | Response |
|---|---|---|---|
| `POST` | `/queue/add` | multipart: `files`, `employee`, `job_name`, `job_number` | `{queued: [], pending: n}` |
| `POST` | `/queue/add-intake` | form: `employee`, `job_name`, `job_number` | `{queued: [], pending: n}` |
| `POST` | `/queue/cancel` | — | `{ok, cleared: n}` |
| `POST` | `/queue/clear-all` | — | `{ok, cleared: n}` |
| `POST` | `/queue/nudge` | — | `{ok, requeued: [], count, worker_restarted}` — manual push for a stalled queue |
| `GET` | `/queue/status` | — | `{pending, completed, kanban}` |

### Events & Results

| Method | Path | Notes |
|---|---|---|
| `GET` | `/events` | SSE stream — connect once, receive all updates |
| `POST` | `/retry-receipt` | `{"filename": "..."}` — re-queues at front |
| `POST` | `/results/update` | `{"filename", "field", "value"}` — inline-edit one field of a completed receipt |
| `POST` | `/kanban/remove` | `{"filename": "..."}` — dismiss a card |
| `POST` | `/generate-spreadsheet` | Streams `.xlsx` binary |
| `POST` | `/results/clear` | Clears completed results, hides generate card |
| `GET` | `/stats` | Spend analytics: totals, by-category, top vendors, and a timeline where each day carries its `total`, receipt `count`, and a running `cumulative` |
| `GET` | `/export/csv` | Streams all completed results as CSV |
| `GET` | `/reports` | Lists previously generated workbooks (name, size, date) |
| `GET` | `/reports/download?filename=` | Download a past report |

### Models

| Method | Path | Notes |
|---|---|---|
| `GET` | `/models/available` | Active model IDs + full list from LM Studio |
| `POST` | `/models/distill` | `{"model": "model-id"}` |
| `POST` | `/models/ocr` | `{"model": ""}` to disable dedicated OCR |
| `GET` | `/models/lmstudio` | Raw list of models loaded in LM Studio |

### Settings & Autocomplete

| Method | Path | Notes |
|---|---|---|
| `GET/POST` | `/settings` | `host_intake_path`, `host_output_path`; GET also returns `version` |
| `GET/POST` | `/settings/processing` | `autocrop`, `grayscale`, `compress`, `local_ocr`, `jpeg_quality` |
| `GET/POST` | `/settings/review` | `require_approval` — block spreadsheet generation until every receipt is approved |
| `GET/POST` | `/settings/email` | SMTP host/port/user/pass/from, recipients, subject (GET never echoes the password) |
| `POST` | `/settings/email/test` | Send a test email with the current settings |
| `GET/POST` | `/saved-fields` | `employees`, `job_names`, `job_numbers` lists |
| `GET` | `/intake/files` | Files waiting in the intake folder |
| `GET` | `/version` | Running build tag |

### Maintenance

| Method | Path | Notes |
|---|---|---|
| `GET` | `/maintenance/orphans` | Report unreferenced files in the working folders — each with `folder`, `name`, full `path`, `size`, `modified` — plus a list of empty temp dirs. Report-only |
| `POST` | `/maintenance/cleanup-empty-dirs` | Delete emptied job/temp folders (`_upload_*`, `_pdf_*`, and any empty subfolder) from the working directories. Returns the removed locations. Also runs automatically at session start |
| `GET` | `/intake/rejected` | List unsupported files quarantined from the intake folder — each with `name`, `reason`, `ext`, `size`, `modified`, full `path` |
| `POST` | `/intake/rejected/delete` | Delete one quarantined file by `name` (path-traversal guarded) |
| `POST` | `/intake/rejected/delete-all` | Delete every quarantined file |
| `POST` | `/admin/restart` | Restart the server process (Docker relaunches it) |

### Watch Mode

| Method | Path | Notes |
|---|---|---|
| `GET` | `/watch/status` | Receipt count, last email date, SMTP status |
| `POST` | `/watch/send-email` | Trigger immediate report email |

### SSE Event Types

All events are JSON, delivered as `data: {...}\n\n` on the `/events` stream.

| Type | Fields | Meaning |
|---|---|---|
| `full_state` | `kanban`, `pending`, `completed` | Sent on connect; full board snapshot |
| `kanban_update` | `filename`, `status`, `data`, `model` | Single card status change |
| `batch_done` | `completed`, `pending` | A batch finished processing |
| `results_cleared` | — | Completed/failed cards cleared |
| `kanban_cleared` | — | Full board reset |
| `log` | `message` | Text log line |
| `progress` | `current`, `total`, `filename` | Progress bar update |

---

## Directory Structure

```
.
├── server.py               # FastAPI server (queue, SSE, endpoints)
├── process_receipts.py     # Extraction pipeline, categorization, spreadsheet
├── spreadsheet_theme.py    # Excel workbook builder (openpyxl)
├── watch_mode.py           # Continuous folder-monitoring daemon
├── receipt_gui.py          # Desktop GUI (customtkinter)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── launch.sh               # One-click start (macOS / Linux)
├── launch.bat              # One-click start (Windows)
├── .env.example            # Volume path configuration template
├── tests/                  # Pytest suite (classification, duplicates, persistence, …)
├── .github/workflows/ci.yml# CI — runs the test suite on every push and PR
└── templates/
    ├── index.html          # SPA frontend (vanilla JS + SSE)
    ├── manifest.json       # PWA manifest
    └── icon.svg
```

At runtime, the following folders are created automatically:

```
output/
├── receipts/               # Renamed receipt images (category_date_vendor.ext)
│   └── _upload_XXXXX/      # Temp staging for web uploads (cleaned up after rename)
├── processing/             # In-flight and failed images live here
├── .app_state.json         # Crash-safe snapshot of completed/failed receipts
├── .app_config.json        # UI-saved settings (paths, schedule, processing, review)
└── Reimbursements_Name_YYYY-MM-DD.xlsx
intake/                     # Drop receipts here for auto-processing
```

> Emptied temp/job folders left behind by clears or interrupted runs can be swept
> up from **Settings → Maintenance → Check Orphaned Files → Delete empty folders**.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.12+ |
| Docker + Compose | Any recent version |
| LM Studio | Latest (with Local Server enabled) |
| Vision LLM | Any multimodal model loaded in LM Studio |

Python package dependencies (installed automatically by Docker or `pip install -r requirements.txt`):

```
fastapi          >= 0.115.0
uvicorn[standard]>= 0.32.0
python-multipart >= 0.0.12
openai           >= 1.57.0     # LM Studio uses the OpenAI-compatible API
openpyxl         >= 3.1.5
Pillow           >= 11.0.0
PyMuPDF          >= 1.24.0
```

`customtkinter` is only required for the desktop GUI and is **not** in `requirements.txt` — install it separately if needed.

---

## Development

### Running the tests

The test suite covers the pure pipeline logic — categorization, duplicate detection, confidence scoring, filename/date handling, spreadsheet generation, and state persistence — and requires no LM Studio connection.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

CI runs the same suite on Python 3.11 and 3.12 for every push and pull request (see `.github/workflows/ci.yml`).

---

## Frequently Asked Questions

**Q: Does this send my receipts anywhere?**  
No. All processing happens through LM Studio on your own machine. The only outbound network call the app makes is to the LM Studio local server (default `localhost:1234`).

**Q: What models work best?**  
Any multimodal model that can see images works. A vision-capable instruction model in the 7–12B range gives strong accuracy on a typical laptop. For 2-stage OCR mode, a dedicated document-OCR model (such as `allenai/olmOCR-2-7B`) followed by any instruction model to distill the structure produces very clean output on handwritten or low-resolution receipts.

**Q: Why are some receipts ending up in Failed?**  
The extractor flags a receipt as low-confidence when it cannot identify a vendor name or a dollar amount. This happens with blurry images, heavily stylized receipts, or models that struggle with a particular format. Click **↺ Retry** to re-queue with the same or a different model, or try enabling the optional OCR model.

**Q: The app says "LM Studio unreachable" — what do I do?**  
Make sure the LM Studio Local Server is running and a model is loaded. The app will **auto-detect** a working endpoint on its own: at startup, and whenever the status reads unreachable, it probes the well-known addresses (LM Studio on `:1234`, the bundled Docker server on `:11434`, and the `host.docker.internal` variants) and connects to whichever answers. You can also force a re-scan with the **🔎 Auto-detect** button in Settings → AI Models → *LLM Server Controls* — handy if a previously-saved "Docker bundled server" choice left the app pointed at the wrong port. If you're running the app inside Docker, `LMSTUDIO_BASE_URL` defaults to `http://host.docker.internal:1234/v1` (not `localhost`) so the container can reach the host network.

> Note: the in-app LLM Server setting is saved and **overrides the `LMSTUDIO_BASE_URL` env var** on subsequent startups. To change the server permanently from the command line you may need to re-run `docker compose up -d` (a bare `restart` does not reload env vars) — or just use the Auto-detect button / LLM Server card in the UI, which apply immediately without a restart.

**Q: Can I process receipts while the previous batch is still running?**  
Yes. "Add to Queue" and "Queue Intake Files" can be clicked at any time. Files are added to a persistent queue that the background worker drains continuously.

**Q: I see ghost cards on the Kanban board after reloading — how do I clean up?**  
Click **Clear Board** to wipe everything and start fresh. Individual cards can also be dismissed with the × button in the card's top-right corner.

---

## License

MIT — see [LICENSE](LICENSE).
