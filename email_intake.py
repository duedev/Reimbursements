"""email_intake.py — pull receipts from an email inbox over IMAP into the pipeline.

The recommended way to feed receipts in without per-brand APIs (see
`GAS_RECEIPT_IMPORT.md`): forward/BCC any receipt — gas, materials, meals,
lodging, anything — to a dedicated mailbox; this module polls that mailbox over
IMAP, pulls each new message apart, and hands the receipt artifacts to a callback
that drops them onto the normal processing queue/board. It is **vendor-agnostic**:
the pipeline's own `classify_category` buckets each receipt, so "more than gas" is
free.

Gmail is the easy host: enable IMAP + 2-Step Verification, generate a 16-char
**App Password**, and connect with that — no Google Cloud project, OAuth consent
screen, or app verification. (A managed Microsoft 365 account usually can't do
this without admin-consented OAuth, which is why a dedicated Gmail is recommended.)

Design notes:
- **Parsing is pure and import-light** (`message_artifacts` / `strip_html_to_text`
  / `route_user`) so it's unit-testable from a raw RFC-822 message with no live
  IMAP and no server/pipeline imports.
- **Three artifact kinds:** image/PDF attachments + inline images (→ existing
  image pipeline, unchanged), and the HTML/plain-text **body** (→ a text source the
  pipeline distils without OCR — a digital receipt's text beats OCR).
- The IMAP side (`poll_once`) is a thin wrapper: fetch UNSEEN, parse, hand off,
  mark \\Seen. Idempotency = server-side \\Seen plus a local processed-id guard.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import ssl
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

try:  # classify attachment kinds by the same extension sets the pipeline uses
    from process_receipts import IMAGE_EXTENSIONS, PDF_EXTENSIONS
except Exception:  # pragma: no cover - keep the module importable standalone
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
    PDF_EXTENSIONS = {".pdf"}

# Text "receipt files" the pipeline distils without OCR (see process_receipts).
TEXT_EXTENSIONS = {".eml.txt", ".html", ".htm", ".txt"}


# ── Config ──────────────────────────────────────────────────────────────────────

@dataclass
class ImapConfig:
    enabled: bool = False
    host: str = ""
    port: int = 993
    username: str = ""
    use_ssl: bool = True
    mailbox: str = "INBOX"
    poll_seconds: int = 120
    mark_seen: bool = True
    process_body: bool = True          # ingest HTML/plain-text body receipts
    plus_routing: bool = False         # route receipts+<user>@… to that user
    allow_senders: list[str] = field(default_factory=list)  # '' = accept all

    @classmethod
    def from_dict(cls, d: dict | None) -> "ImapConfig":
        d = d or {}
        senders = d.get("allow_senders") or []
        if isinstance(senders, str):
            senders = [s.strip() for s in senders.split(",") if s.strip()]
        try:
            port = int(d.get("port") or 993)
        except (TypeError, ValueError):
            port = 993
        try:
            poll = int(d.get("poll_seconds") or 120)
        except (TypeError, ValueError):
            poll = 120
        return cls(
            enabled=bool(d.get("enabled")),
            host=str(d.get("host") or "").strip(),
            port=port,
            username=str(d.get("username") or "").strip(),
            use_ssl=bool(d.get("use_ssl", True)),
            mailbox=str(d.get("mailbox") or "INBOX"),
            poll_seconds=max(15, poll),
            mark_seen=bool(d.get("mark_seen", True)),
            process_body=bool(d.get("process_body", True)),
            plus_routing=bool(d.get("plus_routing", False)),
            allow_senders=[s.lower() for s in senders],
        )

    def to_public_dict(self) -> dict:
        """Config for the UI — never includes the password (that's a secret)."""
        return {
            "enabled": self.enabled, "host": self.host, "port": self.port,
            "username": self.username, "use_ssl": self.use_ssl,
            "mailbox": self.mailbox, "poll_seconds": self.poll_seconds,
            "mark_seen": self.mark_seen, "process_body": self.process_body,
            "plus_routing": self.plus_routing,
            "allow_senders": ", ".join(self.allow_senders),
        }


# ── HTML → text ─────────────────────────────────────────────────────────────────

_BLOCK_TAGS = {"p", "div", "br", "tr", "table", "li", "ul", "ol", "h1", "h2",
               "h3", "h4", "h5", "h6", "section", "header", "footer"}


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank lines / trailing spaces a receipt's markup leaves.
        lines = [ln.strip() for ln in raw.splitlines()]
        out, blank = [], False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def strip_html_to_text(html_str: str) -> str:
    """Best-effort, dependency-free HTML → readable text for an e-receipt body."""
    if not html_str:
        return ""
    p = _HTMLToText()
    try:
        p.feed(html_str)
    except Exception:
        # Last resort: drop tags with a regex so a malformed body still yields text.
        return re.sub(r"<[^>]+>", " ", html_str)
    return p.text()


# ── MIME parsing (pure) ─────────────────────────────────────────────────────────

@dataclass
class Artifact:
    """One receipt candidate pulled out of a message."""
    kind: str            # "image" | "pdf" | "text"
    filename: str        # suggested on-disk name (basename only)
    data: bytes | str    # bytes for image/pdf, str for text
    content_type: str = ""


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _safe_name(name: str, fallback: str) -> str:
    name = (name or "").replace("\x00", "")
    name = os.path.basename(name).strip() or fallback
    # Strip anything that could wander outside a folder; the pipeline also basenames.
    name = re.sub(r"[^A-Za-z0-9._+\- ]", "_", name)[:120]
    return name or fallback


def _ext_kind(filename: str, content_type: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in PDF_EXTENSIONS or content_type == "application/pdf":
        return "pdf"
    if ext in IMAGE_EXTENSIONS or content_type.startswith("image/"):
        return "image"
    return None


def message_artifacts(msg: Message, *, process_body: bool = True,
                      msg_id: str = "") -> list[Artifact]:
    """Pull every receipt candidate out of a parsed email message.

    Returns image/PDF attachments and inline images as binary artifacts, plus —
    when `process_body` and there are no usable attachments — the HTML or plain
    body as a single text artifact (a digital e-receipt with no attachment).
    """
    artifacts: list[Artifact] = []
    html_body: str | None = None
    text_body: str | None = None
    stem = _safe_name(msg_id, "email")[:48] or "email"

    idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get_content_disposition() or "").lower()
        fname = _decode_header(part.get_filename() or "")

        # Binary attachment / inline image → a file the image pipeline handles.
        kind = _ext_kind(fname, ctype)
        if kind or disp == "attachment":
            if kind is None:
                continue  # an attachment we can't process (e.g. .docx) — skip
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            if not payload:
                continue
            idx += 1
            base = _safe_name(fname, f"{stem}_{idx}")
            if not Path(base).suffix:
                base += ".pdf" if kind == "pdf" else ".jpg"
            artifacts.append(Artifact(kind=kind, filename=base, data=payload,
                                      content_type=ctype))
            continue

        # Body parts — keep the richest for a possible text-source fallback.
        if ctype == "text/html" and html_body is None:
            html_body = _part_text(part)
        elif ctype == "text/plain" and text_body is None:
            text_body = _part_text(part)

    has_binary = any(a.kind in ("image", "pdf") for a in artifacts)
    if process_body and not has_binary:
        if html_body and strip_html_to_text(html_body).strip():
            artifacts.append(Artifact(kind="text", filename=f"{stem}.html",
                                      data=html_body, content_type="text/html"))
        elif text_body and text_body.strip():
            artifacts.append(Artifact(kind="text", filename=f"{stem}.txt",
                                      data=text_body, content_type="text/plain"))
    return artifacts


def _part_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return ""


_PLUS_RE = re.compile(r"^[^@]+\+([a-z0-9][a-z0-9_-]{0,31})@", re.IGNORECASE)


def route_user(msg: Message) -> str:
    """Resolve a plus-addressed user tag (receipts+<user>@…) from the recipient
    headers, e.g. for multi-user routing. '' when none/invalid."""
    for hdr in ("Delivered-To", "X-Original-To", "To", "Cc"):
        for raw in msg.get_all(hdr, []) or []:
            for addr in re.split(r"[,\s]+", _decode_header(raw)):
                m = _PLUS_RE.search(addr.strip().strip("<>"))
                if m:
                    return m.group(1).lower()
    return ""


def sender_address(msg: Message) -> str:
    raw = _decode_header(msg.get("From", ""))
    m = re.search(r"[\w.+-]+@[\w.-]+", raw)
    return (m.group(0).lower() if m else raw.lower()).strip()


# DuckDuckGo Email Protection (and similar privacy relays) rewrite the From: header
# to  <local>_at_<domain>_<youralias>@duck.com  so the real sender is buried in the
# local-part — e.g. a Chevron receipt arrives as
#   no-reply_at_notifications.chevronmobileapp.com_drhamilton@duck.com
# Recover <local>@<domain> so a domain allowlist matches relayed receipts.
_DUCK_RELAY_RE = re.compile(r"^(?P<local>[^@]+?)_at_(?P<rest>[^@]+)@duck\.com$", re.I)


def decode_relay_sender(addr: str) -> str:
    """Unwrap a DuckDuckGo-relayed From: to the original sender; passthrough else."""
    m = _DUCK_RELAY_RE.match(addr or "")
    if not m:
        return addr
    local  = m.group("local")
    domain = m.group("rest").rsplit("_", 1)[0]   # strip the trailing _<youralias>
    if local and "." in domain:
        return f"{local}@{domain}".lower()
    return addr


def sender_allowed(msg: Message, allow_senders: list[str]) -> bool:
    if not allow_senders:
        return True
    addr = sender_address(msg)
    # Match against both the literal From: and the relay-decoded original sender, so
    # a privacy-relayed (Duck) receipt still matches its brand domain.
    candidates = {addr, decode_relay_sender(addr)}
    for a in allow_senders:
        a = (a or "").strip().lstrip("@").lower()
        if not a:
            continue
        if any(c == a or c.endswith("@" + a) or a in c for c in candidates):
            return True
    return False


# Verified fuel/receipt sender domains — research + a real received Chevron receipt
# header. Used as an OPTIONAL secondary allowlist; the PRIMARY intake mechanism is a
# Gmail keyword filter → label (see gmail_filter.py / GMAIL_RECEIPTS_FILTER_SETUP.md),
# because most fuel brands don't email receipts, don't publish a sender domain, and
# forwarding/relays rewrite From: anyway. Only reasonably-sourced domains here — no
# guesses. (`earnify.com` is deliberately excluded — it's an unrelated ad company.)
FUEL_RECEIPT_SENDERS = [
    "shell.com", "fuelrewards.com", "email.fuelrewards.com",  # Shell — official
    "gasbuddy.com",                                           # GasBuddy — official safe-sender
    "notifications.chevronmobileapp.com",                     # Chevron/Texaco — real receipt header
    "rewards.sheetz.com",                                     # Sheetz — DNS/SPF = Salesforce MC
    "upside.com", "getupside.com",                            # Upside
    "circlekeasy.com", "kwiktrip.com", "murphyusa.com",       # medium confidence
    "murphydriverewards.com", "maverik.com", "speedway.com",
]


def message_subject(msg: Message) -> str:
    return _decode_header(msg.get("Subject", ""))[:200]


def message_id(msg: Message) -> str:
    mid = _decode_header(msg.get("Message-ID", "")).strip().strip("<>")
    return re.sub(r"[^A-Za-z0-9._@+\-]", "_", mid)[:120]


# ── IMAP polling ────────────────────────────────────────────────────────────────

# Bound the TCP connect/read so an unreachable or filtered host fails fast instead
# of blocking the poller thread (or a "Test connection" request) for minutes.
IMAP_TIMEOUT = int(os.getenv("IMAP_TIMEOUT", "20"))


def _connect(cfg: ImapConfig, password: str) -> imaplib.IMAP4:
    if cfg.use_ssl:
        conn = imaplib.IMAP4_SSL(cfg.host, cfg.port,
                                 ssl_context=ssl.create_default_context(),
                                 timeout=IMAP_TIMEOUT)
    else:
        conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=IMAP_TIMEOUT)
    conn.login(cfg.username, password)
    return conn


def test_connection(cfg: ImapConfig, password: str) -> dict:
    """Verify the IMAP settings with a real login + mailbox select. UI 'Test'."""
    if not (cfg.host and cfg.username and password):
        return {"ok": False, "error": "Set the IMAP host, username and app password first."}
    try:
        conn = _connect(cfg, password)
        try:
            typ, data = conn.select(cfg.mailbox, readonly=True)
            if typ != "OK":
                return {"ok": False, "error": f"Mailbox '{cfg.mailbox}' not found."}
            count = int(data[0]) if data and data[0] else 0
            return {"ok": True, "message": f"Connected — {count} message(s) in {cfg.mailbox}."}
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        return {"ok": False, "error": f"Login/select failed: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Connection failed: {exc}"}


def poll_once(cfg: ImapConfig, password: str, handle, *,
              already_seen: set[str] | None = None, limit: int = 50) -> dict:
    """Fetch UNSEEN messages, parse each, and hand its artifacts to `handle(msg,
    artifacts)`. Marks messages \\Seen (when configured) so they aren't re-ingested.

    `handle(msg, artifacts) -> bool` returns True if it accepted the message (so we
    record/seen it). Returns a small summary dict.
    """
    already_seen = already_seen if already_seen is not None else set()
    summary = {"messages": 0, "receipts": 0, "skipped": 0, "seen_ids": []}
    conn = _connect(cfg, password)
    try:
        typ, _ = conn.select(cfg.mailbox)
        if typ != "OK":
            raise RuntimeError(f"Could not select mailbox '{cfg.mailbox}'")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return summary
        uids = (data[0].split() if data and data[0] else [])[:limit]
        for uid in uids:
            typ, msg_data = conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            mid = message_id(msg)
            if mid and mid in already_seen:
                continue
            if not sender_allowed(msg, cfg.allow_senders):
                summary["skipped"] += 1
                if cfg.mark_seen:
                    conn.store(uid, "+FLAGS", "\\Seen")
                continue
            arts = message_artifacts(msg, process_body=cfg.process_body, msg_id=mid)
            summary["messages"] += 1
            accepted = False
            if arts:
                accepted = bool(handle(msg, arts))
                summary["receipts"] += len(arts)
            if mid:
                summary["seen_ids"].append(mid)
            if cfg.mark_seen and (accepted or not arts):
                conn.store(uid, "+FLAGS", "\\Seen")
        return summary
    finally:
        try:
            conn.logout()
        except Exception:
            pass
