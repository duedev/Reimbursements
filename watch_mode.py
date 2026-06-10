#!/usr/bin/env python3
"""
watch_mode.py
Polls WATCH_INBOX for new receipt images, OCRs them, stores results in a JSON
state file, and moves processed photos to WATCH_STAGED.

The Excel is only generated when "Send Report Now" is triggered via the web UI
(POST /watch/send-email on server.py) or called directly via send_report().

Usage:
    python watch_mode.py
    docker-compose --profile watch up receipt-watcher
"""
from __future__ import annotations

import email as _email_lib
import json
import os
import shutil
import smtplib
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from openai import OpenAI

from process_receipts import (
    IMAGE_EXTENSIONS,
    LMSTUDIO_BASE_URL,
    MAX_PARALLEL_REQUESTS,
    OUTPUT_FOLDER,
    classify_category,
    extract_receipt_data,
    generate_spreadsheet,
    initialize_models,
    rename_receipt_image,
    sort_key_for_receipt,
    _detect_duplicates,
)

# ── Config ─────────────────────────────────────────────────────────────────────
WATCH_INBOX    = Path(os.getenv("WATCH_INBOX",    "/data/watch_inbox"))
WATCH_STAGED   = Path(os.getenv("WATCH_STAGED",   "/data/watch_staged"))
WATCH_STATE    = Path(os.getenv("WATCH_STATE",    "/data/watch_state"))
_OUTPUT_DIR    = Path(os.getenv("OUTPUT_FOLDER",  OUTPUT_FOLDER))
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL",  "60"))
EMPLOYEE_NAME  = os.getenv("WATCH_EMPLOYEE_NAME", "Duane Hamilton")

SMTP_HOST    = os.getenv("SMTP_HOST", "")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")
SMTP_PASS    = os.getenv("SMTP_PASS", "")
SMTP_FROM    = os.getenv("SMTP_FROM", "")
EMAIL_TO     = os.getenv("EMAIL_TO", "")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Weekly Reimbursement Report")

STATE_FILE = WATCH_STATE / "receipts_state.json"


# ── State helpers ──────────────────────────────────────────────────────────────

def _ensure_dirs():
    for d in (WATCH_INBOX, WATCH_STAGED, WATCH_STATE, _OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"employee_name": EMPLOYEE_NAME, "receipts": [], "last_emailed": None}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


# ── Processing ─────────────────────────────────────────────────────────────────

def process_inbox(client: OpenAI, state: dict) -> int:
    """
    Scan WATCH_INBOX for new images, OCR them, append to state, move to staged.
    Returns the count of newly processed files.
    """
    staged_names = {p.name for p in WATCH_STAGED.iterdir() if p.is_file()}
    new_images = sorted(
        p for p in WATCH_INBOX.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS and p.name not in staged_names
    )

    if not new_images:
        return 0

    print(f"[watch] Found {len(new_images)} new image(s) in inbox.")
    processed = 0

    for img_path in new_images:
        print(f"[watch] Processing: {img_path.name}")
        data = extract_receipt_data(client, img_path)
        if data is None:
            print(f"[watch] SKIPPED — extraction failed for {img_path.name}")
            continue

        category = classify_category(data)
        data["_category"] = category

        new_path = rename_receipt_image(img_path, data, category)
        data["_new_filename"] = new_path.name
        data["_file"]         = new_path.name

        dest = WATCH_STAGED / new_path.name
        shutil.move(str(new_path), str(dest))
        print(f"[watch] Staged: {dest.name}  [{category.upper()}] ${data.get('amount', 0):.2f}")

        state["receipts"].append(data)
        save_state(state)
        processed += 1

    return processed


# ── Report generation ──────────────────────────────────────────────────────────

def build_report(state: dict, client: OpenAI | None = None) -> Path:
    """Build the Excel from all accumulated state. Returns the output path."""
    results = list(state.get("receipts", []))
    if not results:
        raise ValueError("No receipts in state — nothing to build.")

    _detect_duplicates(results)

    employee_name = state.get("employee_name", EMPLOYEE_NAME)
    out_path = generate_spreadsheet(results, _OUTPUT_DIR, employee_name)
    if not out_path:
        raise ValueError("Spreadsheet generation returned no output.")
    print(f"[watch] Report saved: {out_path}")
    return out_path


def send_report(state: dict, client: OpenAI | None = None) -> dict:
    """
    Build the Excel and email it. Returns {"ok": bool, "filename": str, "error": str}.
    Called by POST /watch/send-email in server.py.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO]):
        return {"ok": False, "error": "SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO."}

    try:
        out_path = build_report(state, client=client)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Report generation failed: {exc}"}

    try:
        msg = MIMEMultipart()
        msg["From"]    = SMTP_FROM or SMTP_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = EMAIL_SUBJECT

        body = MIMEText(
            f"Please find attached the reimbursement report generated on "
            f"{datetime.now().strftime('%B %d, %Y')}.\n\n"
            f"Receipts processed: {len(state.get('receipts', []))}\n",
            "plain",
        )
        msg.attach(body)

        with open(out_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=out_path.name)
        part["Content-Disposition"] = f'attachment; filename="{out_path.name}"'
        msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(SMTP_FROM or SMTP_USER, EMAIL_TO, msg.as_string())

        print(f"[watch] Email sent to {EMAIL_TO}")
        state["last_emailed"] = datetime.now().date().isoformat()
        save_state(state)
        return {"ok": True, "filename": out_path.name}

    except Exception as exc:
        return {"ok": False, "error": f"Email failed: {exc}"}


# ── Watch loop ─────────────────────────────────────────────────────────────────

def main():
    _ensure_dirs()
    print(f"[watch] Starting. Polling inbox every {WATCH_INTERVAL}s …")
    print(f"[watch] Inbox:  {WATCH_INBOX}")
    print(f"[watch] Staged: {WATCH_STAGED}")
    print(f"[watch] State:  {STATE_FILE}")

    initialize_models()
    client = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lmstudio")
    state  = load_state()

    while True:
        try:
            count = process_inbox(client, state)
            if count:
                print(f"[watch] {count} receipt(s) added. Total accumulated: {len(state['receipts'])}")
        except Exception as exc:
            print(f"[watch] Error during inbox scan: {exc}")
        time.sleep(WATCH_INTERVAL)


if __name__ == "__main__":
    main()
