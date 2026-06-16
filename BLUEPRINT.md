# Receipt Processor — Build-From-Scratch Blueprint

This document specifies **what** to build and **why**, in enough detail to recreate
the application from zero. It deliberately says nothing about **how** to build it:
no languages, frameworks, libraries, containers, or platforms are prescribed.
Choose whatever stack best delivers the behavior below.

The only hard external requirement is a **locally-running, vision-capable large
language model** reachable over a standard chat-completions style HTTP API. The
model runtime is the implementer's choice; the app must never send receipt data
to any third-party cloud service.

---

## 1. Goal

Turn a pile of receipt photos and PDFs into a clean, print-ready expense
reimbursement report with **zero manual data entry** and **complete privacy**. A
field worker or back-office admin drops in receipts; the app reads each one with a
local AI model, organizes and renames the files, lets the user review/correct
anything that looks off, and produces a polished spreadsheet (plus the organized
images) ready to submit for reimbursement.

Success is measured by: how little the user has to type, how trustworthy the
extracted numbers are, and how presentable the final spreadsheet is in the tools
people actually open it with (Microsoft Excel and Apple Numbers).

---

## 2. Primary users & workflow

**Users:** a tradesperson/field employee submitting their own receipts, and an
office admin assembling reports for one or more employees.

**Happy path:**
1. Open the app in a browser (desktop or phone).
2. Enter (or pick from remembered values) the employee name and optional job
   name / job number for this batch.
3. Add receipts — drag-and-drop, file picker, or drop them into a watched intake
   folder. Images and PDFs are both accepted.
4. Watch a live board as each receipt moves through processing.
5. Fix anything flagged; approve receipts.
6. Generate the report. Download the spreadsheet and, if desired, the organized
   image files.

The user can keep adding receipts at any time; processing is continuous.

---

## 3. Core principles

- **Local & private.** All inference runs against a local model. The only
  outbound network call is to that local model endpoint. No telemetry.
- **Never lose work.** Processed results survive a restart/crash.
- **Don't trust the model blindly.** Cross-check extracted amounts, score
  confidence, flag anomalies, and make correction effortless.
- **Don't make the user type.** Remember prior entries; auto-categorize;
  auto-name files; default sensibly.
- **The output must look professional** and open correctly in both Excel and
  Numbers.

---

## 4. Ingestion

- Accept common image formats and PDFs. PDFs are expanded to one image per page
  before processing; each page becomes its own receipt.
- Three intake paths, all feeding one continuous queue:
  - **Upload** (drag-drop / picker) from the browser.
  - **Watched intake folder** — files dropped here are auto-detected within a few
    seconds and queued.
  - **Manual "queue everything in the intake folder"** action.
- Uploaded files are moved through clearly-named working locations: a staging
  area on arrival, a "processing" area while in flight (so failed/in-progress
  images have a visible home), and a "completed receipts" area once renamed.
- The queue is **persistent and drains continuously** by a background worker;
  multiple receipts process in parallel under a **small, configurable concurrency
  limit**. The local model is the bottleneck, so a handful at a time (default a few)
  is both faster and more reliable than flooding it — an oversized pool only causes
  request timeouts that degrade to the lower-accuracy offline parser.

---

## 5. Processing pipeline (per receipt)

Order matters. The canonical order is:

1. **Auto-rotate, then greyscale, then autocrop** — first turn the receipt the right
   way up (bake the photo's stored orientation into the pixels; a deeper check during
   OCR retries the 90° rotations and keeps whichever the engine reads best — see §5.2),
   then flatten to high-contrast greyscale, then trim uniform background borders so the
   receipt fills the frame. All run in place and **before OCR** (the canonical
   autorotate→greyscale→autocrop→OCR order), so the OCR engine, the vision model, the
   on-image markup boxes, and the stored preview all see the same upright, cleaned-up
   image. Autorotate and autocrop are conservative no-ops when nothing needs doing
   (already upright; crop would discard too much or almost nothing). Keep the image at
   **full resolution** here. All rules-based — no model involved.
2. **Extraction** — read the receipt with a built-in OCR engine plus the local
   model. The canonical flow:
   - **OCR (built-in, primary):** a local on-device OCR engine transcribes the
     visible text. Fast, fully offline, runs on every receipt.
   - **OCR (LLM, optional):** when the user has selected a dedicated OCR model,
     a vision LLM *also* transcribes the same receipt. When both an engine and a
     model produce text, **both transcriptions are handed to the distillation
     model together so it can cross-reference the two readings** — preferring
     values that agree and using the clearer reading where they differ. This
     catches engine misreads on hard/handwritten/low-res receipts.
   - **Distillation:** the "distillation" model turns the OCR text into
     structured fields, reconciling the printed total.
   - **Direct vision (rescue):** only when OCR produced no usable text (or
     distillation came back low-confidence) does a vision-capable model read the
     image directly.
   - **Resilient fallback:** if the model endpoint is unreachable, fall back to a
     plain regex parser over the OCR text so the receipt yields *something*
     (flagged for manual review) instead of failing outright.
   - **Reasoning is per stage:** the OCR/transcription pass **always** runs with
     reasoning off (verbatim transcription never benefits from it); the
     distillation/vision pass uses reasoning by default (a live UI toggle), since
     reconciling fields and catching anomalies is where step-by-step reasoning
     helps.
   - **On-image field markup (no model):** the built-in OCR engine reports the
     position of every line it reads. The app maps the final vendor, date and amount
     back to the exact line each value came from — purely rule-based, reusing the
     same money/date/vendor matchers used elsewhere — and records a normalized box.
     The review screen and the full-screen image view then **highlight on the receipt
     image precisely where each field was taken**, so a human can verify at a glance.
     A field that can't be confidently located is left un-boxed and flagged as such
     rather than mis-highlighted. Works with no model running.
3. **Classify** — assign a category (see §7).
4. **Validate** — confidence scoring, amount verification, and any **opt-in**
   spending/date warnings (see §7; off by default).
5. **Rename & file** — move to the completed area under a sortable name.
6. **Accumulate** — add to the in-memory results set; run duplicate detection
   across the batch.
7. **Compress — deferred.** Image compression/downscaling does **not** run per
   receipt. It runs once, later, when the spreadsheet is generated (see §12), so
   the OCR stage always reads the sharpest image and the output folder + embedded
   workbook images are optimized together. (Compressing mid-pipeline historically
   caused stale-file-path bugs and softened the image OCR sees — avoid it.)

**Extracted fields (the receipt record):**

| Field | Meaning |
|---|---|
| vendor / store | Merchant name |
| date | Purchase date |
| amount | Total amount |
| category | fuel / materials / misc (derived) |
| summary | One-line AI description of the purchase |
| job name / job number | User-supplied per batch (never trusted from the model). When left blank, stamped with the literal placeholders `Default Job Name` / `Default Job Number` so the value is visible in the sheet and the user can Ctrl+F find-and-replace it in one pass. |
| expense description | For misc receipts |
| flags | Any anomalies (threshold, stale date, duplicate, low confidence, …) |
| confidence | 0–100 score (derived) |
| amount-verified | Whether the amount was cross-checked against OCR text |
| review-required / approved | Review-gate state |
| processing time, OCR engine used, raw OCR text, step log | Diagnostics |
| original filename, renamed filename, on-disk image path | File tracking |

---

## 6. Confidence & amount verification

- **Confidence score:** compute a 0–100 score from completeness and plausibility
  (e.g. missing vendor or amount tanks it). Below a threshold (~60), auto-mark the
  receipt as needing review. Surface the score on the card as a labeled badge
  (e.g. "82% conf") with a tooltip explaining what it means.
- **Amount verification (against the OCR text):** independently scan the raw OCR text
  for money values on total-like lines and compare to the extracted amount. If
  they agree, mark verified (✓ badge); if they conflict, flag for review. This is
  a cheap regex cross-check that catches model hallucinations.

---

## 7. Categorization & flagging rules

- **Categories:** Fuel, Materials, Miscellaneous. Decide by vendor-name lookup
  against curated lists (gas stations → fuel; hardware/supply stores → materials;
  everything else → misc), refined by receipt content.
- **Spending & date warnings are opt-in and off by default.** There are no
  built-in dollar thresholds or date cutoffs. The user may set, in Settings,
  per-category dollar caps and/or a maximum receipt age; receipts that exceed a
  configured cap or age are then flagged. These checks are **deterministic**
  (evaluated in code, never delegated to the model) so behaviour is predictable.
- Flagged receipts still appear in the report, visibly marked (red Notes cell with
  the reason). With no caps configured, nothing is flagged on amount or age.

---

## 8. Duplicate detection

- Within a batch, receipts sharing the same vendor + date + amount are flagged as
  potential duplicates.
- Before generating, if duplicates exist, present a dialog listing the groups and
  let the user check which ones to **exclude** from the report. Recompute flags on
  the filtered set so excluded items don't leave stale duplicate marks behind.

---

## 9. Review & approval

- Every completed card can be opened in a **review dialog**: shows the receipt
  image and editable fields (vendor, date, amount, category, job name/number,
  summary). Saving updates the record and recomputes duplicates.
- **Inline editing:** common fields are also editable directly on the card.
- **Approval gate (optional setting):** when enabled, spreadsheet generation is
  blocked — both in the UI (disabled button) and server-side (rejected request) —
  until **every** completed receipt is approved. A live status line shows how many
  still need review; a counter badge appears on the board's Completed column.
- **Approve-and-next sweep:** the review dialog shows a counter of how many
  completed receipts still need review/approval, and its primary action approves
  the current receipt and **immediately loads the next one still needing
  approval** (the button reads "Approve & Next" while more remain, "Approve" on
  the last). When the last one is approved the dialog closes with an all-clear
  toast — so a batch can be cleared in one continuous pass without reopening the
  dialog per card.
- The review dialog's **Cancel** control must be clearly visible (not look
  disabled or secondary).

---

## 10. Web UI specification

A single-page app, responsive (works on phone and desktop), installable to the
home screen, with a **light and dark theme** toggle.

**Theming:** Dark theme is the default. The **light theme must feel warm and
intentional, not bland** — give its background subtle, tasteful color washes.
Deliberately avoid the generic purple/indigo "AI default" palette.

**Workspace tab** (top to bottom):
1. **Insights dashboard** (hidden until results exist) — see §11.
2. **Report Details** — employee name, job name, job number; each with
   manage-able autocomplete remembering the last ~20 entries.
3. **Add Receipts** and **Export Report**, side by side, **sharing the page
   width evenly and matching heights**. Add Receipts has the drop zone, an "Add to
   Queue" action, intake-folder helpers, and a file list. Export Report (which
   holds many controls) is laid out to fill the shared space: a Generate
   Spreadsheet action, Export CSV, New Batch, the approval-gate toggle, and — once
   built — a Download control.
4. **Receipt Progress (Kanban board)** — four columns: Queued → Processing →
   Completed → Failed. Each column header has a count and a button to open its
   folder. Cards show filename, status, confidence badge, verified badge,
   approved/needs-review tags (each with an explanatory tooltip), vendor +
   category + amount + date, AI summary, flags, processing time, a "View image"
   button, and contextual actions (Retry/Review/Approve/Dismiss) plus an
   expandable per-step processing log. Completed cards sort by date with approved
   ones last. A search box filters cards (focusable with `/`).
5. **Processing & Errors** — progress bar, live log, and a collapsible error
   sub-section.
6. **Report History** — list and re-download past reports, plus a **Clear
   History** control that deletes all generated report files from the output
   folder (confirmed first; receipt images are untouched).

**Settings tab:** AI model selectors (OCR + distillation, switchable live and
**auto-refreshing** so the list tracks whatever is loaded in the model runtime —
no manual refresh required), a per-stage **reasoning** toggle (drives the
distillation pass; OCR always runs without reasoning), folder paths, schedule,
image-processing toggles, review/approval toggle, email delivery config (with a
"send test" action), and a **Maintenance** card (§13).

**Celebration:** when a batch finishes, fire a generous confetti burst **and play
a short, cheerful celebratory tone**. (The confetti should be lavish — on the
order of several hundred pieces — and the same celebration is replayable by
clicking the "time saved" stat.)

**Download reminder:** after the user downloads the spreadsheet, show a reminder
that the original receipt images live in the output folder and can be copied
alongside the report, with a shortcut to open that folder.

**Accessibility:** keyboard-operable dialogs (Escape to close), focus states,
ARIA labels, no duplicate element IDs.

---

## 11. Insights / analytics

Computed live from the current results and shown both in the web dashboard **and
mirrored inside the generated workbook** (§12).

- **KPI figures:** total spend, receipt count, average per receipt, flagged
  count, verified count, average processing time.
- **By category:** count and total per category, shown as a donut (web) / pie
  (sheet).
- **Top vendors:** ranked by total spend (top ~8), as horizontal bars.
- **Spend over time — must be detailed, not bare bars.** Per-day data carries the
  daily total, the receipt count, and a running cumulative. The chart shows: daily
  spend bars, a **cumulative trend line**, a dashed **average** marker, value/axis
  labels, rich per-day tooltips (date, amount, count, running total), and a
  caption summarizing total, date range, and the peak day.

---

## 12. Spreadsheet output specification

Generating produces one workbook file named like
`Reimbursements_<EmployeeName>_<YYYY-MM-DD>` (sanitize the name for filesystem
safety). It contains **five sheets**, in this order:

1. **Summary** — the reimbursement form:
   - Title banner; then meta rows for **Employee** and **Expense Period** where
     the **label and its value sit immediately side-by-side** (label right-aligned,
     value directly to its right — no gap column, no stray cell borders/lines
     around the value).
   - A "due" note row.
   - Three category sections (Fuel, Materials, Misc), each: a colored banner, a
     header row, one row per receipt, and a subtotal. Then a grand total row.
   - Columns: `# | Date | Store | Job Name | Job Number/Expense | Amount |
     Summary | Notes`. Amounts use an accounting currency format; dates a short
     `m/d/yy`. Flagged notes get a red background; amounts over the category
     threshold are conditionally highlighted. Receipts with no job supplied show
     the literal `Default Job Name` / `Default Job Number` placeholders so they
     are visible and Ctrl+F find-and-replaceable across the sheet.
   - **Fit to content:** columns auto-fit their width and rows grow to fit wrapped
     text so nothing is clipped.
   - Header/meta rows are frozen so they stay visible when scrolling; print setup
     fits to one page wide, landscape, repeating the header rows.
   - Each receipt's `#` cell is an **internal hyperlink** jumping to that
     receipt's image on its category sheet (use the in-workbook link form both
     Excel and Numbers follow).
2. **Insights** — mirrors §11: KPI tiles, a spend-by-category pie, top-vendor
   bars, and a detailed spend-over-time chart (daily columns + cumulative line),
   each backed by a small data table. Use **native charts** (not images) so they
   render in Excel and Numbers.
3–5. **Fuel / Materials / Miscellaneous** — one sheet per category, embedding each
   receipt image with its metadata; the metadata cells reference the Summary rows
   so edits stay consistent.

**Compatibility requirement:** every feature used (charts, conditional
formatting, internal hyperlinks, frozen panes, number formats, embedded images,
auto-fit) must render correctly in **both Microsoft Excel and macOS Numbers**.
Verify this explicitly.

**Image compression happens here:** just before building the workbook, each stored
receipt image is re-encoded/downscaled to an optimized JPEG (honoring quality and
max-dimension settings), updating the tracked file paths. This is idempotent — a
second export of the same batch re-uses the already-optimized files. It both
shrinks the on-disk output folder and the images embedded in the workbook.

Also offer a one-click **CSV export** of all completed receipts.

---

## 13. Maintenance & housekeeping

- **Orphan check:** scan the working folders for files that no result, board
  card, or queued item references (leftovers from clears, crashes, interrupted
  renames). Report each orphan with its folder, name, **full on-disk location**,
  size, and modified time — report only, delete nothing. Handle the case where a
  file's extension changed during compression (match by name *and* stem) and where
  an archived source PDF is still referenced through its converted pages.
- **Delete empty folders:** a routine (exposed as an action) that removes
  **emptied, orphaned job/temp folders — temp or not** — from the working
  directories (upload staging, PDF page folders, and any empty job subfolder),
  walking bottom-up so nested empties collapse. Only ever removes directories that
  contain no files; never the pending-intake root or real input files.
- **Clear report history:** a manual action (in the Report History card) that
  deletes every generated report file from the output folder. Scoped to the safe
  report-name glob — never touches receipt images or unrelated files. Confirmed
  before running.

---

## 14. Real-time updates & persistence

- The board and logs update live via a server-push event stream (the client
  connects once and receives status changes, batch-complete, progress, log lines,
  and board-reset events). On connect, the client receives a full board snapshot.
- **Crash-safe persistence:** completed and failed receipts (and the board) are
  snapshotted to disk and restored on startup, so a restart never loses a batch.
  A stall checker revives a stuck/crashed worker and re-queues stalled items.

---

## 15. Scheduling & delivery (optional)

- A **continuous watch-mode daemon** can monitor an inbox folder, process new
  receipts, and email the accumulated report on a schedule.
- A built-in **weekly scheduler** can generate the report into a configured
  export folder (point it at a synced cloud folder for zero-config upload),
  optionally push it to a cloud storage provider, and optionally email it.
- **Email delivery** over SMTP, fully configurable in the UI with a "send test"
  action; never echo stored passwords back to the client.

---

## 16. Configuration surface

All of the following are adjustable at runtime from the UI (no restart), with
sensible defaults, and persisted:

- Folder paths (intake, output) and host-path display hints.
- AI model selection (OCR model optional — when set, runs LLM OCR alongside the
  built-in engine and cross-references both; distillation model). The selectable
  model list auto-refreshes from the running model runtime.
- Reasoning mode for the distillation pass (OCR always runs without reasoning).
- Image processing: auto-rotate-to-upright on/off, greyscale on/off, autocrop
  on/off, compression on/off, JPEG quality, max stored image dimension.
- Local-OCR fallback on/off.
- Review/approval requirement on/off.
- Schedule (enabled, time, days, delivery targets) and email/SMTP settings.
- Remembered autocomplete lists (employees, job names, job numbers).

---

## 17. Non-functional requirements

- **Privacy:** no receipt data leaves the machine except to the local model.
- **Resilience:** survive per-receipt failures, model outages, and restarts
  without losing processed work.
- **Performance:** parallel processing; responsive UI; don't block the event loop
  on heavy image/spreadsheet work.
- **Compatibility:** spreadsheet output must open cleanly in Excel and Numbers;
  UI must work on mobile and desktop, light and dark.
- **Usability:** minimize typing, make corrections trivial, make every badge and
  control self-explanatory via labels/tooltips.

---

## 18. Definition of done (acceptance checklist)

- [ ] Drop in mixed images + PDFs → each becomes a categorized, named, reviewable
      receipt on a live board, with no manual data entry required.
- [ ] Confidence, verified, approved, and needs-review states are visible and
      self-explanatory.
- [ ] Duplicates are detected and can be excluded before export.
- [ ] The approval gate blocks export until everything is approved (UI + server).
- [ ] Generating produces a 5-sheet workbook (Summary, Insights, 3 image sheets)
      that opens correctly in **both Excel and Numbers**, with the Summary meta
      values sitting beside their labels, fit-to-content columns/rows, no stray
      cell lines, native charts, and working image hyperlinks.
- [ ] The workbook's Insights match the live dashboard, including a detailed
      spend-over-time chart.
- [ ] Image compression happens at export time, not per receipt.
- [ ] Batch completion triggers lavish confetti + a celebratory tone.
- [ ] After download, the user is reminded to copy the receipt images.
- [ ] Orphan check reports full file locations; empty job/temp folders can be
      deleted.
- [ ] Nothing is lost across a restart.
