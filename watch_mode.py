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

from openai import OpenAI  # used only for type annotations below

import app_secrets
from process_receipts import (
    CONFIG_FILE,
    IMAGE_EXTENSIONS,
    MAX_PARALLEL_REQUESTS,
    OUTPUT_FOLDER,
    classify_category,
    extract_receipt_data,
    generate_spreadsheet,
    initialize_models,
    make_client,
    rename_receipt_image,
    sort_key_for_receipt,
    receipt_identity,
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
# Shared app config (CONFIG_FILE) is imported from process_receipts so the
# watcher reads the exact same file the web UI writes — its Settings → Email
# values take precedence over the SMTP_* environment variables, so email can be
# configured without editing docker-compose.yml or restarting.


def load_email_config() -> dict:
    """Resolve SMTP settings: UI-saved config (app config file) over env vars."""
    saved: dict = {}
    try:
        if CONFIG_FILE.exists():
            saved = (json.loads(CONFIG_FILE.read_text()).get("email") or {})
    except Exception:
        saved = {}

    def pick(key: str, env: str, default: str = "") -> str:
        val = saved.get(key)
        return str(val) if val not in (None, "") else os.getenv(env, default)

    try:
        port = int(saved.get("smtp_port") or os.getenv("SMTP_PORT", "587") or 587)
    except (TypeError, ValueError):
        port = 587

    return {
        "host":    pick("smtp_host", "SMTP_HOST"),
        "port":    port,
        "user":    pick("smtp_user", "SMTP_USER"),
        # Password lives in the secrets store (out of the synced config), with a
        # fallback to a legacy in-config value or the SMTP_PASS env var.
        "pass":    app_secrets.get_secret("smtp_pass", "email", "smtp_pass", "SMTP_PASS"),
        "from":    pick("smtp_from", "SMTP_FROM"),
        "to":      pick("email_to",  "EMAIL_TO"),
        "subject": pick("email_subject", "EMAIL_SUBJECT", "Weekly Reimbursement Report"),
        # Optional per-report templates (placeholders like {employee}/{date}/{total});
        # blank falls back to email_template defaults inside render_report_email.
        "subject_template": pick("subject_template", "EMAIL_SUBJECT_TEMPLATE"),
        "body_template":    pick("body_template", "EMAIL_BODY_TEMPLATE"),
    }


def _recipients(to_field: str) -> list[str]:
    return [a.strip() for a in (to_field or "").split(",") if a.strip()]


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
    return {"employee_name": EMPLOYEE_NAME, "receipts": [], "last_emailed": None,
            "sent_ledger": []}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def _ledger_keys(state: dict) -> set:
    """Identity keys of receipts already included in a sent report (this state)."""
    out = set()
    for e in state.get("sent_ledger") or []:
        if isinstance(e, dict) and e.get("key"):
            out.add(tuple(e["key"]))
    return out


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

        # Sent-ledger dedup: a receipt already included in a previously sent report
        # is staged (so it isn't re-processed) but not re-added to the report set.
        key = receipt_identity(data)
        if key[2] != 0 and key in _ledger_keys(state):
            print(f"[watch] Already reported — skipping {dest.name}")
            continue

        state["receipts"].append(data)
        save_state(state)
        processed += 1

    return processed


# ── Report generation ──────────────────────────────────────────────────────────

def build_report(state: dict, client: OpenAI | None = None) -> Path:
    """Build the Excel from all accumulated state. Returns the output path."""
    results = list(state.get("receipts", []))
    # Exclude any receipt already included in a previously sent report.
    ledger = _ledger_keys(state)
    if ledger:
        results = [r for r in results if receipt_identity(r) not in ledger]
    if not results:
        raise ValueError("No receipts in state — nothing to build.")

    _detect_duplicates(results)

    employee_name = state.get("employee_name", EMPLOYEE_NAME)
    out_path = generate_spreadsheet(results, _OUTPUT_DIR, employee_name)
    if not out_path:
        raise ValueError("Spreadsheet generation returned no output.")
    print(f"[watch] Report saved: {out_path}")
    return out_path


def send_workbook_email(workbook_path: Path, receipt_count: int,
                        context: dict | None = None) -> dict:
    """Email an existing workbook via the configured SMTP / ESP account.

    Shared by watch-mode reports and the scheduled-export task in scheduler.py.
    When ``context`` is given (employee/date/total/job…), the subject and body are
    rendered from the per-report templates (see email_template); otherwise a simple
    default subject/body is used. Sending always uses the ONE shared identity — the
    per-user detail lives in the templated content, not the sender.
    """
    import email_template

    cfg = load_email_config()
    if not all([cfg["host"], cfg["user"], cfg["pass"], cfg["to"]]):
        return {"ok": False, "error": "SMTP not configured. Set host, username, password and recipient in Settings → Email."}

    sender = cfg["from"] or cfg["user"]
    recipients = _recipients(cfg["to"])

    ctx = dict(context or {})
    ctx.setdefault("count", receipt_count)
    ctx.setdefault("date", datetime.now().strftime("%B %d, %Y"))
    ctx.setdefault("report_name", workbook_path.name)
    full_ctx = email_template.build_context(**{
        k: ctx[k] for k in ("employee", "count", "total", "date",
                            "job_name", "job_number", "report_name") if k in ctx
    })
    subject, body_text = email_template.render_report_email(
        full_ctx,
        subject_template=cfg.get("subject_template") or cfg.get("subject"),
        body_template=cfg.get("body_template"),
    )
    try:
        msg = MIMEMultipart()
        msg["From"]    = sender
        msg["To"]      = cfg["to"]
        msg["Subject"] = subject

        body = MIMEText(body_text, "plain")
        msg.attach(body)

        with open(workbook_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=workbook_path.name)
        part["Content-Disposition"] = f'attachment; filename="{workbook_path.name}"'
        msg.attach(part)

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["pass"])
            smtp.sendmail(sender, recipients, msg.as_string())

        print(f"[watch] Email sent to {cfg['to']}")
        return {"ok": True, "filename": workbook_path.name}
    except Exception as exc:
        return {"ok": False, "error": f"Email failed: {exc}"}


def send_test_email() -> dict:
    """Send a tiny test message to verify the SMTP settings. Used by the UI."""
    cfg = load_email_config()
    if not all([cfg["host"], cfg["user"], cfg["pass"], cfg["to"]]):
        return {"ok": False, "error": "SMTP not configured. Set host, username, password and recipient first."}
    sender = cfg["from"] or cfg["user"]
    try:
        msg = MIMEText(
            "This is a test message from Receipt Processor. "
            "If you're reading this, your email settings work.",
            "plain",
        )
        msg["From"]    = sender
        msg["To"]      = cfg["to"]
        msg["Subject"] = "Receipt Processor — test email"
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["pass"])
            smtp.sendmail(sender, _recipients(cfg["to"]), msg.as_string())
        return {"ok": True, "message": f"Test email sent to {cfg['to']}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_report(state: dict, client: OpenAI | None = None) -> dict:
    """
    Build the Excel and email it. Returns {"ok": bool, "filename": str, "error": str}.
    Called by POST /watch/send-email in server.py.
    """
    cfg = load_email_config()
    if not all([cfg["host"], cfg["user"], cfg["pass"], cfg["to"]]):
        return {"ok": False, "error": "SMTP not configured. Set host, username, password and recipient in Settings → Email."}

    try:
        out_path = build_report(state, client=client)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Report generation failed: {exc}"}

    receipts = state.get("receipts", [])
    _total = 0.0
    for r in receipts:
        try:
            _total += float(r.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    result = send_workbook_email(out_path, len(receipts), {
        "employee": state.get("employee_name", EMPLOYEE_NAME),
        "total": round(_total, 2),
    })
    if result.get("ok"):
        state["last_emailed"] = datetime.now().date().isoformat()
        # Record every receipt that went into this report so the next one doesn't
        # re-send it (the sent-ledger dedup).
        ledger = state.setdefault("sent_ledger", [])
        seen = _ledger_keys(state)
        now = int(time.time())
        report_name = out_path.name
        for r in state.get("receipts", []):
            key = receipt_identity(r)
            if key[2] == 0 or key in seen:
                continue
            seen.add(key)
            ledger.append({
                "key":      list(key),
                "vendor":   r.get("vendor") or "",
                "date":     r.get("date") or "",
                "amount":   key[2],
                "filename": r.get("_new_filename") or r.get("_file") or "",
                "report":   report_name,
                "sent_at":  now,
            })
        save_state(state)
    return result


# ── Watch loop ─────────────────────────────────────────────────────────────────

def main():
    _ensure_dirs()
    print(f"[watch] Starting. Polling inbox every {WATCH_INTERVAL}s …")
    print(f"[watch] Inbox:  {WATCH_INBOX}")
    print(f"[watch] Staged: {WATCH_STAGED}")
    print(f"[watch] State:  {STATE_FILE}")

    # Restore the saved LLM provider config (OpenRouter / custom server URL +
    # API key) BEFORE building the client, mirroring the web server's startup.
    # Without this the watcher ignored the user's provider choice and hard-coded
    # the LM Studio dummy key — so OpenRouter and any custom endpoint silently
    # 401'd and every receipt fell back to the offline parser. Imported lazily so
    # the watcher doesn't pull in the FastAPI app unless it actually runs.
    try:
        import server as _srv
        _srv._first_run_provider_default()
        _srv._apply_llm_server_config()
    except Exception as exc:
        print(f"[watch] Warning: could not apply saved LLM provider config: {exc}")

    initialize_models()
    client = make_client()
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
