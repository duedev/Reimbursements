# Google Drive Receipt Capture + Gmail→Drive Ingestion — Research / Design Write-up

> **Status:** ✅ **Phase 1 + Phase 2 IMPLEMENTED** (2026-06-23). The in-app Drive API
> poller is `gdrive_intake.py` (wired into `server.py` as `_run_gdrive_poller` + the
> `GET/POST /settings/gdrive` group, Settings → **Google Drive Intake** card); the
> Gmail→Drive Apps Script ships as `gmail_to_drive.gs` with the step-by-step
> `GMAIL_TO_DRIVE_SETUP.md`. Off by default, opt-in; the OAuth refresh token lives in
> `app_secrets` (`drive.readonly` scope). Phase 3 polish (move-to-"Done" subfolder,
> richer SSE status) remains optional/future. The sections below are the design this
> was built from.
> **Scope:** Two requests — (1) use **Google Drive** as a receipt-capture inbox
> (phone photos / Drive document-scan land in a Drive folder the app ingests),
> and (2) ingest **emailed receipts from Gmail into that same Drive folder**, so
> both phone and email feed one place that flows into the existing local
> OCR + vision-LLM pipeline.
> **Audience:** A developer deciding what to build.
> **Companion doc:** `GAS_RECEIPT_IMPORT.md` (the email/IMAP angle for fuel
> receipts). This doc is the **Google-Drive-as-hub** variant of that idea.
> **Last updated:** 2026-06-23.

---

## 1. Executive summary — the recommendation

Both asks collapse into **one design: make a single Google Drive folder the app's
"receipts inbox," feed it from two sources (your phone and your Gmail), and have
the app pull from it.** The pipeline does not change — the app already watches a
folder and drains a work queue — so the new work is *getting Drive content into
that intake*, not re-plumbing extraction.

```
 Phone (Drive Scan / share-sheet) ┐
                                  ├──►  Drive "Receipts Inbox" ──►  app pulls ──►  existing _work_queue ──► pipeline
 Gmail (Apps Script time-trigger) ┘        (one folder, by ID)     (Drive API)      (unchanged)
```

**Chosen approach (per product decision 2026-06-23):**

1. **App side — in-app Drive API poller.** A new module that mirrors
   `watch_mode.py`: authenticate once (OAuth), poll the inbox folder *by file ID*,
   download new files, and hand them to the existing queue. Self-contained and
   works headless / in Docker. Adds two Google client libraries and stores an
   OAuth refresh token in `app_secrets.py`.
2. **Email side — Gmail→Drive Apps Script.** A one-time Google Apps Script on the
   user's own account, on a time-driven trigger, that finds labeled receipt
   emails and saves their attachments (and optionally the email body as a PDF)
   into the *same* Drive inbox folder. It runs in Google's cloud — nothing to
   host — and unifies email + phone into one path the poller already drains.

**Why Drive-as-hub instead of direct IMAP** (the `GAS_RECEIPT_IMPORT.md` pick):
it matches the request literally ("gmail → gdrive"), gives one place to eyeball
everything captured, lets phone and email share a single ingestion path, and
reuses 100% of the existing pipeline. Direct IMAP is leaner on dependencies but
bypasses the Drive hub the user specifically wants. Both remain valid; this doc
commits to the Drive-hub design.

**The real cost is privacy posture, not engineering** — see §6.

---

## 2. Findings — the starting point

### 2.1 The user's Drive is greenfield
A live search of the connected Drive (`duaneroberthamilton@gmail.com`) for
`fullText / title contains 'receipt'` returned **nothing**; recent files are all
unrelated (course mappings, a résumé, study docs). There is **no existing
receipts folder or naming convention to preserve** — we design the workflow from
scratch, nothing to migrate.

### 2.2 The app already has the ingestion machinery
Confirmed in code:

| Piece | Where | Behavior |
|---|---|---|
| Folder watcher | `server.py:_run_watcher` | Polls `INTAKE_FOLDER` every **5s**, queues new files, handles images / PDFs / zips. |
| Standalone daemon | `watch_mode.py:process_inbox` / `main()` | Polls `WATCH_INBOX` every **60s**, dedups inbox-vs-staged by **filename**, runs the pipeline, persists state JSON. |
| HTTP upload | `server.py:POST /queue/add` | Size/type-guarded file upload into the same `_work_queue`. |
| Cloud-API precedent | `scheduler.py:upload_dropbox()` | Already uploads the **export** to Dropbox with a stored token — a Drive **import** is the mirror image. |
| Secrets store | `app_secrets.py` | `save_secret` / `get_secret`, 0600, atomic, kept out of the synced config. SMTP pass + OpenRouter key already live here. |
| Settings pattern | `server.py:GET/POST /settings/*` + `_apply_*()` | Group persisted in `.app_config.json`, applied at startup. Mirror this for a `gdrive` group. |

**Key gotcha:** dedup today is **filename-based**. A Drive source must dedup by
**Drive file ID** (and the Gmail bridge by **Message-ID**), because names collide
across sources and re-uploads. This mirrors the same note in
`GAS_RECEIPT_IMPORT.md` §3 (track UID + Message-ID).

### 2.3 No inbound cloud ingestion exists yet
There is **no** Drive API, Gmail API, IMAP, rclone, or gdrive code in the repo
today. `TUTORIAL.md` only suggests pointing the **export** folder at a
Drive-synced directory — that's delivery, not capture. This is all new surface.

---

## 3. Feature A — Google Drive as the capture inbox

**Capture** (no app code — these are how a user fills the inbox):
- **Google Drive mobile app → Scan** (the built-in document scanner) saves a
  de-skewed PDF straight into the folder. Ideal for paper receipts.
- **Phone share-sheet → Drive** for an existing photo/PDF.
- Drag-drop on desktop, or the Gmail bridge in §4.

**Pull** — three ways to get the folder's contents into the app. The product
decision is **the API poller**; the other two are documented as alternatives.

| Approach | Mechanism | New deps | Headless/Docker | Token in app? |
|---|---|---|---|---|
| **In-app Drive API poller (chosen)** | OAuth once; poll folder by ID; download new files (dedup by file-ID); enqueue. | `google-api-python-client`, `google-auth-oauthlib` | ✅ | ✅ (refresh token in `app_secrets`) |
| Drive for Desktop sync | Point `INTAKE_FOLDER` at the locally synced folder; existing watcher handles it. | none | ❌ (needs desktop client) | ❌ |
| rclone mount | `rclone mount` the folder; point `INTAKE_FOLDER` at the mount. | rclone binary | ✅ | ❌ (rclone holds it) |

### 3.1 The poller module (design sketch — not implemented)

A new `gdrive_intake.py` that mirrors `watch_mode.py`'s shape:

```python
# gdrive_intake.py  (DESIGN SKETCH — not implemented)
#
# Reuses: process_receipts pipeline, app_secrets for the token, the same
# _work_queue path the folder watcher uses. Adds NOTHING to extraction.

def poll_once(service, state: dict, intake_dir: Path) -> int:
    """List the inbox folder, download files we haven't seen, drop into intake."""
    seen_ids: set[str] = set(state.get("gdrive_seen_ids", []))
    q = (f"'{FOLDER_ID}' in parents and trashed = false "
         f"and (mimeType contains 'image/' or mimeType = 'application/pdf')")
    resp = service.files().list(q=q, fields="files(id,name,mimeType,md5Checksum)",
                                pageSize=100).execute()
    new = 0
    for f in resp.get("files", []):
        if f["id"] in seen_ids:            # dedup by Drive file ID, NOT name
            continue
        dest = intake_dir / _safe_name(f["name"])      # basename only, no traversal
        _download(service, f["id"], dest)              # files().get_media
        seen_ids.add(f["id"]); new += 1
    state["gdrive_seen_ids"] = sorted(seen_ids)
    return new
```

Integration choices:
- **Simplest wiring:** download into `INTAKE_FOLDER`; the existing 5s
  `_run_watcher` picks them up — *no queue code touched at all*.
- **Tighter wiring:** enqueue directly via the same `_tag_item` / `_work_queue`
  path `/queue/add` uses, skipping the disk round-trip.
- Run it either as an `asyncio` task in `server.py`'s lifespan (like
  `scheduler.run_scheduler`) **or** as an opt-in mode of the `watch_mode.py`
  daemon. Recommend the lifespan task so the web UI can show status/errors over SSE.

### 3.2 Auth & setup (the fiddly part)
- A **Google Cloud project** with the Drive API enabled and an **OAuth client
  (Desktop app)**. The user does a one-time consent (installed-app / device flow);
  the app stores the **refresh token** via `app_secrets.save_secret("gdrive_token", …)`.
- Scope: `https://www.googleapis.com/auth/drive.readonly` is enough to list +
  download. Use `drive.file` if the app should also move/label processed files.
- Token refresh handled by `google.auth` transport; surface a clear "reconnect
  Google" action in Settings when refresh fails.

---

## 4. Feature B — Gmail → Drive (emailed receipts)

The standard, **server-less** pattern: a **Google Apps Script** with a
time-driven trigger searches Gmail and copies attachments into a Drive folder by
ID. It runs in Google's cloud (nothing to host) and drops files into the **same**
inbox the §3 poller drains — so email and phone converge with zero extra app code.
A maintained open-source reference is [`piraveen/gmail2gdrive`](https://github.com/piraveen/gmail2gdrive),
which sorts Gmail attachments into Drive folders by rule.

### 4.1 The bridge script (design sketch — ship as a setup guide)

```javascript
// Apps Script (runs in the user's Google account, on a time trigger)
// Setup: create a Gmail filter that labels receipt mail "Receipts"
//        (by sender domain / subject), then schedule this every 15 min.
function saveReceiptsToDrive() {
  const folder = DriveApp.getFolderById('YOUR_INBOX_FOLDER_ID');
  // Unprocessed receipt mail; re-label instead of reprocessing (the dedup).
  const threads = GmailApp.search('label:Receipts -label:Receipts-Saved');
  for (const thread of threads) {
    for (const msg of thread.getMessages()) {
      for (const att of msg.getAttachments()) {
        const t = att.getContentType();
        if (t.startsWith('image/') || t === 'application/pdf') {
          folder.createFile(att);                 // → the Drive inbox
        }
      }
      // Optional: also save the email body as a PDF for body-only receipts
      // folder.createFile(msg.getAttachments().length ? ... : bodyAsPdf(msg));
    }
    thread.addLabel(GmailApp.getUserLabelByName('Receipts-Saved')); // mark done
  }
}
```

Notes:
- **Dedup is label-based** (`-label:Receipts-Saved` + re-label) — the Apps Script
  equivalent of tracking Message-IDs. The §3 poller's file-ID dedup is the second
  safety net.
- **Filtering** (sender allow-list, subject heuristics) happens in the **Gmail
  filter** that applies the `Receipts` label, so the script stays tiny and the
  user controls what counts as a receipt.
- **No app credentials** — the script lives entirely in the user's account; the
  app never touches Gmail. This is strictly less privilege than direct IMAP.

### 4.2 Alternatives (documented, not chosen)
- **Direct IMAP poll in the app** — `GAS_RECEIPT_IMPORT.md`'s pick. Stdlib-only
  (`imaplib` + `email`), most local-first, but skips Drive entirely.
- **Gmail API in the app** — same OAuth cost as Drive; heavier than IMAP for the
  same result. Only worth it if we want Gmail and Drive under one Google client.

---

## 5. How it bolts onto the existing app

| Concern | Reuse / pattern | File |
|---|---|---|
| Settings group `gdrive` | Mirror the `/settings/email` GET+POST + `_apply_*()` flow; persist non-secret fields in `.app_config.json`. | `server.py` |
| OAuth refresh token | `app_secrets.save_secret("gdrive_token", …)` / `get_secret(...)`. Never in the synced config. | `app_secrets.py` |
| Poll loop | `asyncio` lifespan task like `scheduler.run_scheduler`, **or** a mode of the daemon. | `server.py` / `watch_mode.py` |
| Enqueue | Drop into `INTAKE_FOLDER` (watcher picks up) **or** `_tag_item` → `_work_queue`. | `server.py` |
| Dedup | Persist `gdrive_seen_ids` in the workspace state JSON (where benchmarks/runs already live). | state file |
| Status/errors to UI | Broadcast `{"type":"log", …}` like the watcher; add a Settings card + "reconnect Google" action. | `server.py` / `templates/index.html` |

Proposed `.app_config.json` shape:

```json
{
  "gdrive": {
    "enabled": false,
    "folder_id": "",
    "poll_interval": 300,
    "scope": "drive.readonly",
    "move_processed": false
  }
}
```
(The OAuth token is **not** here — it lives in `.app_secrets.json`.)

---

## 6. Privacy posture — go in with eyes open

The documented promise is *"no receipt data ever leaves the machine except to the
local model"* (`CLAUDE.md`), already softened to allow opt-in OpenRouter. Drive /
Gmail ingestion is a **further, material change** and must be treated like the
OpenRouter opt-in: off by default, explicit consent, disclosed in
README/TUTORIAL/ADVISORY.

Nuances worth stating plainly:
- **Receipts in your Gmail are already on Google's servers.** Pulling them down to
  process locally does **not** newly expose them to Google — it arguably improves
  things (you get a local copy + local extraction). The honest framing is "Google
  already has these; the app reads them," not "the app sends them to Google."
- **The genuinely new surface is the OAuth credential the app holds.** A stored
  Drive refresh token can read (or, with `drive.file`, modify) the user's Drive.
  Minimize scope (`drive.readonly`), store via `app_secrets` (0600), and provide a
  one-click disconnect/revoke.
- **The Apps Script bridge holds no app credential at all** — it's the
  least-privilege half of the design.
- **Local extraction is unaffected:** OCR + offline parser stay local; the
  existing `LLM_ALLOW_IMAGE` gate still governs whether an image reaches the cloud
  LLM. Drive ingestion changes *transport/storage*, not the extraction privacy gate.

---

## 7. Dependencies & effort

| Item | Cost |
|---|---|
| `google-api-python-client`, `google-auth-oauthlib` | New deps — a real departure from the app's lean/stdlib ethos. Only the API-poller path needs them (sync/rclone need none). |
| Google Cloud OAuth client setup | One-time, by the user; the fiddliest onboarding step. Document it carefully. |
| Poller module + settings + UI card + tests | ~1 module mirroring `watch_mode.py` + the standard settings group + a `tests/test_gdrive_intake.py` mocking the Drive client (same way OCR/LLM are mocked). |
| Apps Script bridge | A script file + a setup guide (Gmail filter + label + trigger). No app code. |

---

## 8. Recommended phased build (when implemented)

1. **Phase 1 — Drive API poller (opt-in, off by default).** Settings `gdrive`
   group, OAuth connect flow, token in `app_secrets`, poll-by-ID with file-ID
   dedup, enqueue via `INTAKE_FOLDER`. Tests mock the Drive client. Docs: privacy
   disclosure + OAuth setup.
2. **Phase 2 — Gmail→Drive bridge.** Ship the Apps Script + a step-by-step guide
   (filter → label → trigger → folder ID). No app change; it just fills the inbox
   Phase 1 already drains.
3. **Phase 3 (optional) — polish.** "Reconnect Google" + revoke in Settings;
   optional `move_processed` to a "Done" Drive subfolder; SSE status card; a
   `camscanner`-style preset note for Drive Scan PDFs.

---

## 9. Open questions / decisions for a future build

- **Deployment target** decides defaults: headless/Docker → API poller (chosen);
  a desktop install could instead use zero-code Drive-for-Desktop sync.
- **Scope:** `drive.readonly` (simplest) vs `drive.file` (needed only for
  `move_processed`).
- **Shared Drive vs My Drive** folder (multi-user mode would want per-user folders
  or per-user tokens — currently the LLM/secrets are instance-level; a per-user
  Drive token is a new wrinkle to design alongside `multiuser.py`).
- **Body-only email receipts** (no attachment): render the email body to PDF in
  the Apps Script, or skip and rely on attachments only.

---

## 10. Sources

- [Automatically Save Email Attachments to Google Drive Using Google Apps Script — Medium](https://medium.com/@pablopallocchi/automatically-save-email-attachments-to-google-drive-using-google-apps-script-7a751a5d3ac9)
- [`piraveen/gmail2gdrive` — Apps Script that sorts Gmail attachments into Drive folders](https://github.com/piraveen/gmail2gdrive)
- [Class GmailAttachment — Google Apps Script reference](https://developers.google.com/apps-script/reference/gmail/gmail-attachment)
- [Download Gmail Attachments to Google Drive with Apps Script — labnol.org](https://www.labnol.org/code/20617-download-gmail-attachments-to-google-drive)
- Companion in-repo: `GAS_RECEIPT_IMPORT.md` (the direct-IMAP / email-receipt angle).
