# Receipt Processor

> AI-powered receipt scanning, extraction, and reimbursement report generation — runs entirely on your hardware with [LM Studio](https://lmstudio.ai).

Drop in photos or PDFs of receipts. The app extracts the vendor, date, amount, and category using a local vision model, renames and organizes the files, tracks everything on a live Kanban board, and produces a formatted, print-ready Excel reimbursement form. No cloud APIs, no subscription fees — 100% local AI.

---

## Features

- **Local AI only** — Uses LM Studio for all vision and language inference; nothing leaves your machine
- **Web UI with live Kanban board** — Real-time receipt status tracking (Queued → OCR → Distilling → Done/Failed) via Server-Sent Events
- **Batch & continuous modes** — Process a folder all at once, or let the watcher auto-queue new files as they appear
- **PDF support** — PDFs are automatically expanded to per-page images before processing
- **Smart categorization** — Receipts classified as Fuel, Materials, or Miscellaneous based on vendor and content
- **Professional Excel output** — Themed workbook with embedded receipt images, subtotals per category, grand total, and accounting-format amounts
- **Persistent autocomplete** — Employee name, job name, and job number fields remember your last 20 entries
- **Category-prefixed filenames** — Processed images renamed to `fuel_12-30-24_shell.jpg` for instant sorting
- **Duplicate detection** — Same vendor/date/amount flagged automatically
- **Optional email delivery** — Watch-mode daemon can email the weekly report over SMTP
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
2. Load a **multimodal / vision model** — Gemma 4, LLaVA, or similar
3. Start the **Local Server** in LM Studio (default port **1234**)
4. The app auto-detects loaded models and populates the model selectors

**Two-stage OCR mode (optional):** Load a second, dedicated OCR model (e.g. `allenai/olmOCR-2-7B`). Select it in the "OCR Model" dropdown. The first model transcribes text; the second distills structured data — useful for boosting accuracy on difficult receipts.

---

## Usage

### Web Interface

Open `http://localhost:8000`.

#### Adding receipts

| Method | How |
|---|---|
| **Drag & drop** | Drag image files or PDFs onto the upload zone |
| **Browse** | Click the upload zone and select files |
| **Intake folder** | Drop files into the configured intake folder; they appear in the queue automatically within 5 seconds |
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

#### AI models

| Variable | Default | Description |
|---|---|---|
| `GEMMA_SMALL_MODEL_ID` | `google/gemma-4-12b-qat` | Default distillation model (small/fast) |
| `GEMMA_LARGE_MODEL_ID` | `google/gemma-4-26b-a4b-qat` | Large distillation model |
| `OLMOCR_MODEL_ID` | `allenai/olmOCR-2-7B` | Optional dedicated OCR model |

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

Leave `SMTP_HOST` empty to disable email entirely.

| Variable | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP port (default 587, TLS) |
| `SMTP_USER` / `SMTP_PASS` | SMTP credentials |
| `SMTP_FROM` | Sender address |
| `EMAIL_TO` | Recipient address(es) |
| `EMAIL_SUBJECT` | Subject line (default: "Weekly Reimbursement Report") |

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
  │  Ingest     │  PDF → per-page JPEGs (PyMuPDF, 2× zoom)
  │             │  Image resize to max 1568px, JPEG encode, base64
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Stage 1    │  (Optional — only when a separate OCR model is configured)
  │  OCR        │  Dedicated model transcribes all visible text verbatim
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Stage 2    │  Vision or OCR-text → structured JSON via distillation model
  │  Distill    │  Extracts: date, vendor, amount, category, summary, flags
  └──────┬──────┘
         │
         ├── Low confidence? (missing vendor or amount) ──► Failed
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
  │  Move       │  Saved to output/receipts/
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Accumulate │  Added to _results list; duplicate detection runs across batch
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Spreadsheet│  Themed Excel workbook with embedded images, subtotals, total
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

Each generated workbook contains four sheets:

| Sheet | Contents |
|---|---|
| **Summary** | Formatted reimbursement form — employee name, expense period, all receipts grouped by category with subtotals and a grand total |
| **Fuel** | Embedded receipt images for fuel receipts |
| **Materials** | Embedded receipt images for materials receipts |
| **Miscellaneous** | Embedded receipt images for miscellaneous receipts |

**Summary sheet columns:**

| Col | Header | Notes |
|---|---|---|
| A | Receipt No. | Sequential within category |
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
| `GET` | `/queue/status` | — | `{pending, completed, kanban}` |

### Events & Results

| Method | Path | Notes |
|---|---|---|
| `GET` | `/events` | SSE stream — connect once, receive all updates |
| `POST` | `/retry-receipt` | `{"filename": "..."}` — re-queues at front |
| `POST` | `/kanban/remove` | `{"filename": "..."}` — dismiss a card |
| `POST` | `/generate-spreadsheet` | Streams `.xlsx` binary |
| `POST` | `/results/clear` | Clears completed results, hides generate card |

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
| `GET/POST` | `/settings` | `host_intake_path`, `host_output_path` |
| `GET/POST` | `/saved-fields` | `employees`, `job_names`, `job_numbers` lists |
| `GET` | `/intake/files` | Files waiting in the intake folder |

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
└── Reimbursements_Name_YYYY-MM-DD.xlsx
intake/                     # Drop receipts here for auto-processing
```

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.12+ |
| Docker + Compose | Any recent version |
| LM Studio | Latest (with Local Server enabled) |
| Vision LLM | Any multimodal model loaded in LM Studio (Gemma 4, LLaVA, etc.) |

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

## Frequently Asked Questions

**Q: Does this send my receipts anywhere?**  
No. All processing happens through LM Studio on your own machine. The only outbound network call the app makes is to the LM Studio local server (default `localhost:1234`).

**Q: What models work best?**  
Any multimodal model that can see images works. Gemma 4 12B and 27B (QAT variants) give excellent accuracy. For 2-stage OCR mode, `allenai/olmOCR-2-7B` as the OCR model followed by a Gemma distillation model produces very clean output on handwritten or low-resolution receipts.

**Q: Why are some receipts ending up in Failed?**  
The extractor flags a receipt as low-confidence when it cannot identify a vendor name or a dollar amount. This happens with blurry images, heavily stylized receipts, or models that struggle with a particular format. Click **↺ Retry** to re-queue with the same or a different model, or try enabling the optional OCR model.

**Q: The app says "LM Studio unreachable" — what do I do?**  
Make sure the LM Studio Local Server is running and a model is loaded. If you're running the app inside Docker, verify `LMSTUDIO_BASE_URL` is set to `http://host.docker.internal:1234/v1` (not `localhost`) so the container can reach the host network.

**Q: Can I process receipts while the previous batch is still running?**  
Yes. "Add to Queue" and "Queue Intake Files" can be clicked at any time. Files are added to a persistent queue that the background worker drains continuously.

**Q: I see ghost cards on the Kanban board after reloading — how do I clean up?**  
Click **Clear Board** to wipe everything and start fresh. Individual cards can also be dismissed with the × button in the card's top-right corner.

---

## License

MIT — see [LICENSE](LICENSE).
