# Gmail → Google Drive receipt bridge — setup guide

This is the **email half** of the Google-Drive-as-hub capture path (Phase 2 of
`GOOGLE_DRIVE_IMPORT.md`). A tiny **Google Apps Script** (`gmail_to_drive.gs`)
runs in *your* Google account on a timer, copies receipt-email attachments (and,
optionally, body-only e-receipts as PDFs) into a Drive folder, and re-labels the
mail so it is never processed twice. The receipt app's **Drive poller** (Settings →
**Google Drive Intake**) then pulls from that same folder into the normal pipeline.

It runs entirely in Google's cloud — **nothing to host, no app credentials**. It is
strictly less privilege than giving the app your mailbox: the script only moves mail
that is *already in your Gmail* into *your own* Drive folder.

> Privacy note: this is an **opt-in cloud capture source**, off by default. See
> `GOOGLE_DRIVE_IMPORT.md` §6 and the ADVISORY. Local OCR/LLM and the
> `LLM_ALLOW_IMAGE` gate are unaffected — this only changes how receipts *arrive*.

---

## 1. Make the Drive "Receipts Inbox" folder

1. In Google Drive, create a folder, e.g. **`Receipts Inbox`**.
2. Open it and copy the **folder ID** from the URL — the part after `/folders/`:
   `https://drive.google.com/drive/folders/`**`1AbC…XyZ`** → `1AbC…XyZ`.

This is the same folder the app's Drive poller is pointed at.

## 2. Label receipt mail in Gmail

1. Create two labels (Gmail → **Settings → Labels → Create new label**):
   **`Receipts`** and **`Receipts-Saved`**.
2. Create a **filter** that applies the **`Receipts`** label to receipt mail.
   Gmail → search box → **Show search options**, e.g.:
   - *From:* `noreply@shell.com OR receipts@homedepot.com OR …`, or
   - *Subject:* `receipt OR invoice OR "order confirmation"`
   - → **Create filter → Apply the label: Receipts** (and optionally "Skip the
     Inbox"). Tick *also apply to matching conversations* to backfill.

The filter is where *you* decide what counts as a receipt, so the script stays tiny.

## 3. Install the Apps Script

1. Go to **https://script.google.com → New project**.
2. Replace the contents with `gmail_to_drive.gs` from this repo.
3. Set the two values at the top:
   - `INBOX_FOLDER_ID` → the folder ID from step 1.
   - `SAVE_BODY_AS_PDF` → `true` if you also want body-only e-receipts saved as PDFs.
4. **Save**, then **Run → `saveReceiptsToDrive`** once to grant the Drive + Gmail
   permissions (you'll see a Google consent screen — approve it for your account).

## 4. Schedule it

1. In the Apps Script editor, open **Triggers** (the clock icon) → **Add Trigger**.
2. Choose function **`saveReceiptsToDrive`**, event source **Time-driven**,
   **Minutes timer → Every 15 minutes** (or your preference). Save.

That's it. New labelled receipt mail now lands in the Drive folder within ~15
minutes, and the app's poller imports it on its own poll interval.

## 5. Connect the app's Drive poller

In the receipt app: **Settings → Google Drive Intake** → paste the **folder ID**,
complete the one-time **Connect Google** OAuth, and enable polling. See
`GOOGLE_DRIVE_IMPORT.md` §3.2 for the Google Cloud OAuth-client setup.

---

### Dedup & troubleshooting
- **Dedup is label-based** here (`-label:Receipts-Saved` + re-label) — the Apps
  Script equivalent of tracking Message-IDs. The app's **file-ID dedup** is the
  second safety net, so a file copied twice is still only imported once.
- **Nothing appears in Drive?** Run `saveReceiptsToDrive` manually and check the
  Apps Script **Executions** log; confirm the `Receipts` label actually has mail and
  the folder ID is correct.
- **Re-process a message:** remove its `Receipts-Saved` label.
