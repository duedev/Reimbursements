# Microsoft OneDrive Receipt Import — Setup Guide

> **Status: implemented.** `onedrive_intake.py` + the **Settings → OneDrive Intake**
> card are live. This document is the full setup walkthrough (the short version is
> on the app's **Guides** tab).

Make one OneDrive folder the app's **"receipts inbox."** Fill it from your phone
(the OneDrive mobile app has a built-in document **Scan** button), from the iOS/
Android share-sheet, or from any folder synced to a PC — the app polls that folder
and pulls every new image/PDF into the normal processing pipeline automatically.

It mirrors the Google Drive intake exactly: an opt-in, off-by-default cloud
*capture* source. Files move **from** OneDrive **into** the local pipeline; nothing
is uploaded, and the receipt image still only reaches a cloud LLM if you separately
enabled that (Settings → AI Model).

---

## 1. What you need

- A **Microsoft account** with OneDrive (a free personal account is fine).
- A one-time, **free** Azure "app registration" (~3 minutes, no credit card, no
  Azure subscription needed — the registration only mints a client ID the app uses
  to ask Microsoft for permission to read your OneDrive).

Unlike the Google path there is **nothing to install**: the app talks to the
Microsoft Graph API with Python's standard library, so no new dependencies.

## 2. One-time Azure app registration

1. Go to <https://portal.azure.com> and sign in with your Microsoft account.
2. Search for **App registrations** (under **Microsoft Entra ID**) → **New registration**.
3. Fill in:
   - **Name:** anything, e.g. `Receipt App`.
   - **Supported account types:** pick **Personal Microsoft accounts only** for a
     personal OneDrive. (Pick *"Accounts in any organizational directory and
     personal Microsoft accounts"* if you might also sign in with a work/school
     account.)
   - **Redirect URI:** leave **empty** — the device-code sign-in needs none.
4. Click **Register**, then copy the **Application (client) ID** from the Overview
   page (a GUID like `4f0a…-…`). This is *not* a secret.
5. Open the app's **Authentication** page → scroll to **Advanced settings** →
   set **Allow public client flows** to **Yes** → **Save**.
   *(This is what enables the "enter a code on microsoft.com/devicelogin" sign-in —
   the only step people commonly miss.)*

No client secret, no API permissions page edits, no admin consent: the app requests
the delegated `Files.Read` scope at sign-in time and you approve it on your own
account. *(A client secret is only needed for advanced confidential-client setups;
the Settings card and `ONEDRIVE_CLIENT_SECRET` env support one but the recommended
public-client flow above does not use it.)*

## 3. Create the inbox folder

In OneDrive (web, app, or synced folder), create a folder for receipts — e.g.
**`Receipts`** at the top level of your OneDrive. Subfolders are ignored; only
files directly in the folder are imported.

## 4. Connect the app

1. Open the app → **Settings → OneDrive Intake**.
2. Paste the **Application (client) ID** from step 2.4.
3. Set **Account type** to match step 2.3 (Personal / Work / Either).
4. Set the **folder path** — `Receipts` for the folder above (a nested folder is a
   path like `Documents/Receipts`).
5. Click **Save OneDrive settings**, then **Connect Microsoft**.
6. A short code appears. Open **<https://microsoft.com/devicelogin>** on any device,
   enter the code, sign in, and approve **Files.Read** access. The app notices by
   itself within a few seconds and shows **✓ Connected**.
7. Tick **Enable OneDrive intake** and click **Save OneDrive settings** again.
   Use **🔌 Test connection** / **⬇ Check for receipts now** to verify.

The device-code sign-in works even when the app runs headless in Docker on another
machine — the browser half can happen on your phone.

## 5. Daily use

Drop receipt photos/PDFs into the folder any way you like:

- **Phone:** OneDrive app → **+ → Scan**, save into `Receipts` (best quality — it
  crops/de-skews for you), or share any photo to OneDrive via the share-sheet.
- **PC:** save into the synced `OneDrive/Receipts` folder.
- **Email attachments:** Outlook's "Save to OneDrive" on an attachment.

The app polls on the configured interval (default 5 minutes), downloads anything
new, and the receipts appear on the Workspace board like any other upload. Each
OneDrive file is imported **once** (tracked by its OneDrive file ID in
`output/.onedrive_seen.json`), so editing or re-listing the folder never
duplicates receipts — and the originals in OneDrive are never touched.

## 6. Privacy & security posture

- **Opt-in, off by default.** Nothing runs until you configure and connect it.
- **Read-only by default** (`Files.Read`). The app can read your OneDrive files but
  never modify, move, or delete them.
- **The new surface is the stored OAuth token,** not the receipts (they were
  already in OneDrive). The refresh token is kept in the local secrets file
  (`.app_secrets.json`, mode 0600), **never** in the syncable `.app_config.json`.
  Note: Microsoft *rotates* refresh tokens — the app persists the replacement token
  it receives on every poll, which is why the stored value changes over time.
- **Disconnect any time** from the Settings card (clears the local token). To also
  revoke the grant on Microsoft's side, remove the app at
  <https://account.live.com/consent/Manage> (personal accounts) or
  <https://myapps.microsoft.com> (work/school). Microsoft offers no programmatic
  revoke for consumer tokens, hence the manual link.
- **Multi-user mode:** the card and endpoints are **admin-only** (the token is
  instance-level); downloaded receipts land in the default workspace's intake.

## 7. Environment variables (optional — the UI covers everything)

| Var | Meaning |
|---|---|
| `ONEDRIVE_CLIENT_SECRET` | Client secret via env instead of the UI (confidential clients only — not needed for the recommended public-client setup) |
| `ONEDRIVE_REFRESH_TOKEN` | Pre-obtained refresh token via env (note it will rotate; the rotated value is persisted to the secrets file) |
| `ONEDRIVE_TIMEOUT` | Seconds before an unreachable Graph call fails fast (default 30) |

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Connect fails immediately with `invalid_client` / AADSTS7000218 | **Allow public client flows** wasn't enabled — step 2.5. |
| Sign-in page says the app "is not configured for consumers" (AADSTS50020) | The registration's *Supported account types* doesn't match your account — re-check step 2.3 and the card's **Account type**. |
| Test connection: "folder reachable" but nothing imports | Files must sit **directly** in the folder (subfolders are skipped) and be images/PDFs. Also check the intake toggle is on. |
| "token refresh failed" after a long offline period | Refresh tokens expire after ~90 days of disuse (or a password change/revoke). Click **Connect Microsoft** and sign in again. |
| Same receipt re-imported | Only happens if `output/.onedrive_seen.json` was deleted — the dedup ledger lives there. |

## 9. How it compares to the other intake paths

| Path | Best when |
|---|---|
| **Email/IMAP intake** | Receipts arrive as emails; universal, no cloud project at all |
| **Google Drive intake** | You live in Google's ecosystem (Drive Scan, Gmail→Drive script) |
| **OneDrive intake** (this) | You live in Microsoft's ecosystem — Windows-synced folders, Outlook "Save to OneDrive", the OneDrive mobile Scan button |

All three feed the identical local pipeline; enable any combination.
