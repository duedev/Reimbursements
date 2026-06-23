"""Inbound email (IMAP) receipt intake — MIME parsing, the pipeline text path, the
IMAP poll orchestration, and the settings/ingest endpoints.

Vendor-agnostic by design: the pipeline classifies whatever arrives, so these use a
mix of gas and non-gas receipts. No live IMAP server — the connection is faked.
"""
import email
import json
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import email_intake as ei
import process_receipts as pr
import server


# ── HTML → text ─────────────────────────────────────────────────────────────────

def test_strip_html_drops_script_and_keeps_text():
    html = ("<html><head><style>.x{color:red}</style></head><body>"
            "<h1>Shell</h1><p>Date: 06/20/2026</p><div>Total: $52.30</div>"
            "<script>steal()</script></body></html>")
    txt = ei.strip_html_to_text(html)
    assert "Shell" in txt and "Total: $52.30" in txt and "Date: 06/20/2026" in txt
    assert "steal" not in txt and "color:red" not in txt


def test_strip_html_malformed_falls_back():
    assert "Hello" in ei.strip_html_to_text("<p>Hello <b>world</p")


# ── MIME artifact extraction ────────────────────────────────────────────────────

def _html_msg():
    raw = (b"From: Costco <noreply@costco.com>\r\n"
           b"To: receipts+alice@gmail.com\r\n"
           b"Subject: Fuel receipt\r\nMessage-ID: <abc@costco.com>\r\n"
           b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<body><h2>Costco Gas</h2><p>Total: $48.12</p><p>06/20/2026</p></body>\r\n")
    return email.message_from_bytes(raw)


def test_html_body_becomes_text_artifact():
    arts = ei.message_artifacts(_html_msg(), msg_id="abc")
    assert len(arts) == 1
    a = arts[0]
    assert a.kind == "text" and a.filename.endswith(".html") and "Costco Gas" in a.data


def test_pdf_and_image_attachments_extracted():
    m = MIMEMultipart()
    m["From"] = "v@x.com"; m["Subject"] = "r"; m["Message-ID"] = "<p@x.com>"
    m.attach(MIMEText("see attached", "plain"))
    pdf = MIMEApplication(b"%PDF-1.4 ...", _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="invoice.pdf")
    m.attach(pdf)
    img = MIMEImage(b"\x89PNG\r\n\x1a\n fake", _subtype="png")
    img.add_header("Content-Disposition", "attachment", filename="photo.png")
    m.attach(img)
    arts = ei.message_artifacts(m, msg_id="p")
    kinds = sorted(a.kind for a in arts)
    assert kinds == ["image", "pdf"]          # body text dropped when binaries present
    assert {a.filename for a in arts} == {"invoice.pdf", "photo.png"}


def test_unprocessable_attachment_skipped():
    m = MIMEMultipart()
    m["From"] = "v@x.com"; m["Subject"] = "r"
    doc = MIMEApplication(b"PK..", _subtype="vnd.openxmlformats")
    doc.add_header("Content-Disposition", "attachment", filename="notes.docx")
    m.attach(doc)
    m.attach(MIMEText("<p>Walmart $9.99 06/01/2026</p>", "html"))
    arts = ei.message_artifacts(m, msg_id="d")
    # .docx can't be processed → no binary → falls back to the HTML body.
    assert [a.kind for a in arts] == ["text"]


def test_plain_text_body_when_no_html():
    raw = (b"From: a@b.com\r\nSubject: r\r\nContent-Type: text/plain\r\n\r\n"
           b"Home Depot\r\nTotal $19.99\r\n")
    arts = ei.message_artifacts(email.message_from_bytes(raw), msg_id="t")
    assert [a.kind for a in arts] == ["text"]
    assert arts[0].filename.endswith(".txt")


def test_process_body_false_skips_bodies():
    assert ei.message_artifacts(_html_msg(), process_body=False) == []


# ── Routing / allowlist / config ────────────────────────────────────────────────

def test_route_user_plus_addressing():
    assert ei.route_user(_html_msg()) == "alice"
    raw = b"From: a@b.com\r\nTo: receipts@gmail.com\r\n\r\nhi"
    assert ei.route_user(email.message_from_bytes(raw)) == ""


def test_sender_allowed():
    msg = _html_msg()
    assert ei.sender_allowed(msg, [])                       # empty = accept all
    assert ei.sender_allowed(msg, ["costco.com"])
    assert ei.sender_allowed(msg, ["noreply@costco.com"])
    assert not ei.sender_allowed(msg, ["shell.com"])


def test_config_roundtrip_hides_password():
    cfg = ei.ImapConfig.from_dict({
        "enabled": True, "host": "imap.gmail.com", "username": "x@gmail.com",
        "port": "993", "allow_senders": "a.com, b.com", "poll_seconds": "5",
    })
    assert cfg.enabled and cfg.port == 993 and cfg.allow_senders == ["a.com", "b.com"]
    assert cfg.poll_seconds == 15           # clamped to the 15s floor
    pub = cfg.to_public_dict()
    assert "password" not in pub and pub["host"] == "imap.gmail.com"


# ── Pipeline text path ──────────────────────────────────────────────────────────

def test_is_text_source():
    assert pr._is_text_source(Path("a.html")) and pr._is_text_source(Path("a.txt"))
    assert not pr._is_text_source(Path("a.jpg")) and not pr._is_text_source(Path("a.pdf"))


def test_pipeline_distills_html_body_without_ocr(tmp_path):
    p = tmp_path / "costco.html"
    p.write_text("<body><h2>Costco Gas</h2><p>Total: $48.12</p><p>06/20/2026</p></body>")
    steps = []
    data = pr._extract_receipt_with_status(None, p, None, steps)
    assert data is not None
    assert data["vendor"] == "Costco Gas" and abs(data["amount"] - 48.12) < 0.01
    assert data["date"] == "2026-06-20"
    assert data["_ocr_engine"] == "email-text" and data["_text_source"] is True
    stepnames = [s.get("step") for s in steps]
    assert "email_text" in stepnames
    # No image-prep ran on a text source.
    assert not ({"exif_rotate", "grayscale", "autocrop"} & set(stepnames))


def test_pipeline_empty_body_returns_none(tmp_path):
    p = tmp_path / "empty.html"
    p.write_text("<body><script>x</script></body>")
    assert pr._extract_receipt_with_status(None, p, None, []) is None


def test_render_fallback_off_by_default(tmp_path, monkeypatch):
    p = tmp_path / "r.html"
    p.write_text("<body>hi</body>")
    monkeypatch.setattr(pr, "RENDER_HTML_FALLBACK", False)
    assert pr._maybe_render_text_source(p, []) is None
    # Enabled but no renderer installed → still None (graceful), with a logged step.
    monkeypatch.setattr(pr, "RENDER_HTML_FALLBACK", True)
    steps = []
    assert pr._maybe_render_text_source(p, steps) is None
    assert any(s.get("step") == "render" for s in steps)


# ── IMAP poll orchestration (faked connection) ──────────────────────────────────

class _FakeIMAP:
    def __init__(self, messages):
        self.messages = messages                 # list[(uid_bytes, raw_bytes)]
        self.stored = []
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        return ("OK", [str(len(self.messages)).encode()])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(u for u, _ in self.messages)])

    def fetch(self, uid, spec):
        for u, raw in self.messages:
            if u == uid:
                return ("OK", [(b"1 (RFC822)", raw)])
        return ("NO", [None])

    def store(self, uid, flags, value):
        self.stored.append((uid, value))
        return ("OK", [b""])

    def logout(self):
        self.logged_out = True


def test_poll_once_fetches_parses_and_marks_seen(monkeypatch):
    raw = (b"From: v@x.com\r\nSubject: r\r\nMessage-ID: <m1@x.com>\r\n"
           b"Content-Type: text/html\r\n\r\n<p>Shell $30.00 06/01/2026</p>")
    fake = _FakeIMAP([(b"1", raw)])
    monkeypatch.setattr(ei, "_connect", lambda cfg, pw: fake)
    captured = []
    cfg = ei.ImapConfig(enabled=True, host="h", username="u", mark_seen=True)
    summary = ei.poll_once(cfg, "pw", lambda msg, arts: (captured.append(arts) or True))
    assert summary["messages"] == 1 and summary["receipts"] == 1
    assert captured and captured[0][0].kind == "text"
    assert fake.stored and fake.stored[0][1] == "\\Seen"   # marked seen after accept
    assert fake.logged_out


def test_poll_once_skips_disallowed_sender(monkeypatch):
    raw = b"From: spam@evil.com\r\nSubject: r\r\nContent-Type: text/plain\r\n\r\nTotal $5"
    fake = _FakeIMAP([(b"1", raw)])
    monkeypatch.setattr(ei, "_connect", lambda cfg, pw: fake)
    cfg = ei.ImapConfig(enabled=True, host="h", username="u", allow_senders=["trusted.com"])
    got = []
    summary = ei.poll_once(cfg, "pw", lambda m, a: got.append(a) or True)
    assert summary["skipped"] == 1 and summary["messages"] == 0 and not got


# ── Server endpoints + ingest integration ───────────────────────────────────────

def test_email_intake_settings_roundtrip(tmp_path, monkeypatch):
    c = TestClient(server.app)
    r = c.post("/settings/email-intake", json={
        "enabled": True, "host": "imap.gmail.com", "username": "receipts@gmail.com",
        "password": "app-pw-1234", "mailbox": "INBOX", "poll_seconds": 90,
        "allow_senders": "amazon.com",
    })
    assert r.status_code == 200 and r.json()["ok"]
    got = c.get("/settings/email-intake").json()
    assert got["host"] == "imap.gmail.com" and got["username"] == "receipts@gmail.com"
    assert got["password_set"] is True and "password" not in got
    assert got["poll_seconds"] == 90 and got["allow_senders"] == "amazon.com"


def test_test_and_pollnow_require_config(tmp_path, monkeypatch):
    c = TestClient(server.app)
    # Nothing configured yet → test reports not-ok, poll-now 400.
    assert c.post("/settings/email-intake/test").json()["ok"] is False
    assert c.post("/settings/email-intake/poll-now").status_code == 400


def test_ingest_message_enqueues_onto_board(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "IMAGES_FOLDER", tmp_path / "receipts")
    monkeypatch.setattr(server, "PROCESSING_FOLDER", tmp_path / "processing")
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)  # don't drain
    server._work_queue.clear()
    server._kanban.clear()

    msg = _html_msg()
    arts = ei.message_artifacts(msg, msg_id="abc")
    assert server._ingest_email_message(msg, arts) is True
    assert len(server._work_queue) == 1
    item = server._work_queue[0]
    assert item["filename"].endswith(".html") and item["user_id"] == "default"
    assert server._kanban[item["filename"]]["status"] == "queued"
