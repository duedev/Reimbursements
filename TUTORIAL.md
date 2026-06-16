# Receipt Processor — Tutorial for New Users

This guide is written for someone who has never used Docker or run a local AI model before. Follow the steps in order and you will have the app running in about 20–30 minutes.

---

## Step 1 — Install Docker Desktop

Docker is the engine that runs the app. You only need to install it once.

**Windows**

1. Go to [https://docs.docker.com/desktop/install/windows/](https://docs.docker.com/desktop/install/windows/) and click **Download Docker Desktop for Windows**.
2. Open the installer and keep clicking **Next** until it finishes.
3. During setup Windows may ask: *"Install WSL 2 components?"* — click **Yes** or **OK**. This is normal and expected.
4. Restart your computer when prompted.
5. After restart, Docker Desktop will launch automatically. You will see a small whale icon in the system tray (bottom-right corner of the screen). Wait until it stops animating — that means Docker is ready.

**Mac**

1. Go to [https://docs.docker.com/desktop/install/mac/](https://docs.docker.com/desktop/install/mac/) and download the version matching your Mac (Intel or Apple chip — check **Apple menu → About This Mac**).
2. Open the `.dmg` file, drag Docker to Applications, then open Docker from Launchpad.
3. macOS will ask for your password to allow the system extension — enter it.
4. Wait for the whale icon in the menu bar to stop animating.

---

## Step 2 — Install LM Studio and Download the Models

LM Studio runs the AI that reads your receipts. Everything stays on your computer.

1. Go to [https://lmstudio.ai](https://lmstudio.ai) and click **Download**.
2. Open the installer and follow the prompts (Windows: Next → Next → Install; Mac: drag to Applications).
3. Open LM Studio.
4. In the left sidebar, click the **Search** icon (magnifying glass).
5. In the search box, look for a **vision / multimodal** model that fits your computer — its model card will be tagged *Vision*, and a 7–12B size suits most laptops. Click **Download** next to it. The download is a few gigabytes — let it finish. (The exact model doesn't matter: the app auto-detects whatever vision model you load.)
6. Optional but recommended for difficult receipts: also search `olmOCR-2-7B` and download it the same way. This model handles blurry or handwritten receipts much better. Once it's loaded, you can pick it as the **OCR Model** in the app's Settings tab — the app then reads each receipt with *both* its built-in reader and this model and cross-checks the two for accuracy. (The model list in Settings refreshes itself, so whatever you load in LM Studio shows up automatically.)
7. Click the **Developer** tab in the left sidebar (looks like `</>` or says "Local Server").
8. Click **Start Server**. The status should turn green and show port **1234**.
9. Leave LM Studio open in the background — the app needs it running while you process receipts.

---

## Step 3 — Download This Project

1. Go to the project page on GitHub (your team lead or IT contact can give you the URL, or you may have received it by email).
2. Click the green **Code** button near the top-right of the page.
3. Click **Download ZIP**.
4. Once the download finishes, right-click the ZIP file and choose **Extract All** (Windows) or double-click it (Mac).
5. Move the resulting folder somewhere easy to find, such as your **Documents** folder. The folder is called `Reimbursements`.

---

## Step 4 — First Launch

This step starts the app for the first time. The launch script asks you three questions about where to keep your files, then starts everything automatically.

**Windows**

1. Open the `Reimbursements` folder.
2. Double-click **launch.bat**.
3. A black command-prompt window will open. It will ask three questions — see below.

**Mac**

1. Open **Terminal**: press `Command + Space`, type `Terminal`, press Enter.
2. In Finder, open the `Reimbursements` folder.
3. Drag the file **launch.sh** from Finder into the Terminal window. The file path will appear in Terminal.
4. Press **Enter**.
5. If you see a message like *"permission denied"*, type `chmod +x ` (note the space), drag `launch.sh` in again, press Enter, then drag it in and press Enter once more.

**The three folder questions**

The script will ask:

> **1) Receipts drop folder — put receipt photos here**

This is the folder you will drop receipt photos into. You can press **Enter** to accept the default, or type a path like `C:\Users\YourName\Desktop\Receipts` (Windows) or `/Users/yourname/Desktop/Receipts` (Mac).

> **2) Reports folder — spreadsheets are saved here**

This is where finished Excel files will be saved. Press Enter to accept the default, or choose a folder you will remember.

> **3) Auto-export folder — scheduled reports are copied here**

This is where the weekly automatic export goes.

**Tip:** For this third folder, choose a folder that is already synced to Dropbox, Google Drive, or OneDrive. That way the weekly report automatically appears in the cloud with no extra steps. For example: `C:\Users\YourName\Dropbox\Reimbursements` or `/Users/yourname/Google Drive/Reimbursements`.

After you answer the questions, the script builds and starts the app (this takes a few minutes the first time — Docker is downloading components). Your browser will open automatically to **http://localhost:8000** when it is ready.

To change the folder settings later, open Terminal (Mac) or Command Prompt (Windows), navigate to the Reimbursements folder, and run `./launch.sh --reconfigure` (Mac) or `launch.bat --reconfigure` (Windows).

---

## Step 5 — Processing Receipts

**Adding a receipt**

You have two options:

- **Drag and drop:** Drag an image file (JPEG, PNG, etc.) or a PDF directly onto the blue upload zone on the web page.
- **Drop folder:** Copy or move receipt photos into the receipts folder you chose in Step 4. The app checks that folder every few seconds and picks them up automatically.

After adding files, click **Add to Queue**.

**Watching the board**

The Kanban board shows each receipt moving through four columns:

| Column | What it means |
|---|---|
| Queued | Waiting its turn |
| Processing | The AI is reading it right now |
| Completed | Done — vendor, date, and amount extracted |
| Failed | The AI could not read it — see Troubleshooting |

**Fixing mistakes**

If the AI got a field wrong (wrong vendor name, wrong amount, etc.), click on the field directly in the card. A small text box will appear. Type the correct value and press Enter.

**Job name and job number**

If you leave the **Job Name** or **Job Number** boxes empty, every receipt is stamped with the placeholder text **"Default Job Name"** and **"Default Job Number"** in the spreadsheet. That is on purpose: open the finished sheet, use Find & Replace (Ctrl+F / Cmd+F), and swap those placeholders for the real values in one go.

**Reviewing and approving**

Each completed card has a **Review & Approve** button. Click it to see the receipt image next to its extracted fields, fix anything that looks off, and approve it. To speed through a whole batch, use **Approve & Next**: it approves the current receipt and immediately opens the next one that still needs review, with a counter showing how many remain — so you can clear the batch in one pass. (If you turned on "Require review & approval" in Settings, the Generate button stays disabled until that counter reaches zero.)

**Generating the spreadsheet**

Once at least one receipt reaches Completed, a **Generate Spreadsheet** button appears. Click it. Your browser will download the Excel file. The filename will look like `Reimbursements_YourName_2025-06-10.xlsx`. You will also find a copy in the Reports folder you chose in Step 4.

**Report history**

Past reports are listed in the **Report History** card, where you can re-download any of them. To tidy up, click **Clear History** — this deletes the saved report files from the Reports folder (your receipt images are not affected).

---

## Step 6 — Setting Up the Weekly Schedule and Email

You can have the app automatically save (and optionally email) a report on a weekly schedule — no clicking required.

1. On the web page, look for the **Settings** card (usually a gear icon or a section labeled Settings near the bottom of the page).
2. In the **Schedule** section:
   - Turn the schedule **on**.
   - Set the **day** (e.g., Thursday) and **time** (e.g., 5:00 PM) for the export to run.
   - The report will automatically be saved to the export folder you chose in Step 4.
3. To also send the report by email, fill in the **Email** fields:
   - **SMTP Host** — your outgoing mail server (e.g., `smtp.gmail.com` for Gmail).
   - **SMTP Port** — usually `587`.
   - **SMTP User / Password** — your email address and password (or an app-specific password if your account uses two-factor authentication).
   - **From** — your email address.
   - **To** — the address to send reports to (can be yourself, your accountant, etc.).
4. Click **Save** (or the equivalent button in the Settings panel).

If you want to test the email before the scheduled day, look for a **Send Report Now** button in the Settings panel.

---

## Step 7 — Troubleshooting

| Problem | Plain-language fix |
|---|---|
| **Docker is not running** | Look for the whale icon in the system tray (Windows) or menu bar (Mac). If it is not there, open Docker Desktop from the Start menu or Launchpad. Wait for it to finish starting (the icon stops animating) then try again. |
| **"LM Studio unreachable" message** | Open LM Studio, click the Developer tab, and make sure the server shows **Running** on port 1234. Also make sure at least one model is loaded. If you closed LM Studio, open it again. |
| **Port 8000 already in use** | Another program on your computer is using port 8000. The quickest fix: open Docker Desktop, find the `receipt-processor` container, stop it, then restart via launch.bat / launch.sh. If the conflict persists, restart your computer. |
| **Receipt is stuck in Processing** | The AI model may have crashed or timed out. Refresh the browser page. If the card reappears in Failed, click the **Retry** button. If LM Studio shows no model loaded, reload the model there first. |
| **The app says "Failed" for all receipts** | Make sure a vision-capable model is loaded in LM Studio (not a text-only model) — its model card should be tagged *Vision*. This is the model you downloaded in Step 2. |
| **Browser shows "This site can't be reached"** | The app container may not have finished starting. Wait 30 seconds and refresh. If it still does not work, check that Docker Desktop is running and open a new launch.bat / launch.sh window. |
| **Spreadsheet has no data** | Only receipts in the **Completed** column are included. If all your receipts are in Failed, fix them first using Retry, then click Generate Spreadsheet again. |
