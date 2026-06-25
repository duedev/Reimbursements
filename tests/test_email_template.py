"""Per-report email templating (email_template.render_report_email) + the
send_workbook_email wiring. The report is sent from one shared identity; the
subject/body are templated per user / per report.
"""
import email_template as et


def test_defaults_render_with_context():
    ctx = et.build_context(employee="Alice", count=3, total=120.5, date="June 25, 2026")
    subj, body = et.render_report_email(ctx)
    assert "Alice" in subj and "June 25, 2026" in subj
    assert "Alice" in body
    assert "Receipts: 3" in body
    assert "$120.50" in body


def test_job_clause_built_and_placeholders_suppressed():
    ctx = et.build_context(employee="Bob", job_name="Bridge", job_number="42")
    assert ctx["job_clause"] == " for job Bridge (#42)"
    # The literal default-job constants are treated as "unset".
    ctx2 = et.build_context(employee="Bob", job_name="Default Job Name",
                            job_number="Default Job Number")
    assert ctx2["job_clause"] == ""


def test_custom_templates_used():
    ctx = et.build_context(employee="Carol", count=5, total=10)
    subj, body = et.render_report_email(
        ctx, subject_template="Report for {employee} ({count})",
        body_template="Total {total} — thanks {employee}")
    assert subj == "Report for Carol (5)"
    assert body == "Total $10.00 — thanks Carol"


def test_unknown_placeholder_renders_empty_not_error():
    ctx = et.build_context(employee="Dave")
    subj, body = et.render_report_email(ctx, subject_template="Hi {nope} {employee}")
    assert subj == "Hi  Dave"          # missing {nope} -> empty, no KeyError


def test_total_string_passthrough():
    ctx = et.build_context(total="N/A")
    assert ct_total(ct := ctx) == "N/A"


def ct_total(ctx):
    return ctx["total"]


def test_send_workbook_email_uses_templates(monkeypatch, tmp_path):
    import watch_mode as wm

    sent = {}

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, sender, to, msg):
            sent["sender"] = sender
            sent["msg"] = msg

    monkeypatch.setattr(wm.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(wm, "load_email_config", lambda: {
        "host": "h", "port": 587, "user": "shared@esp", "pass": "p",
        "from": "receipts@mydomain.com", "to": "boss@office.com",
        "subject": "ignored-when-template-set",
        "subject_template": "Reimbursement for {employee} — {date}",
        "body_template": "Hi, report for {employee}. Total {total}.",
    })
    wb = tmp_path / "R.xlsx"
    wb.write_text("x")
    res = wm.send_workbook_email(wb, 2, {"employee": "Alice", "total": 99.0,
                                         "date": "June 25, 2026"})
    assert res["ok"]
    assert sent["sender"] == "receipts@mydomain.com"

    # Parse the MIME message so RFC2047-encoded headers / base64 bodies decode.
    import email as emaillib
    from email.header import decode_header, make_header
    m = emaillib.message_from_string(sent["msg"])
    subject = str(make_header(decode_header(m["Subject"])))
    assert "Reimbursement for Alice" in subject
    body = ""
    for part in m.walk():
        if part.get_content_type() == "text/plain":
            body = part.get_payload(decode=True).decode()
    assert "Total $99.00" in body
