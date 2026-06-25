"""email_template.py — render a report email's subject + body from a context.

Pure and dependency-free (easy to unit-test). The report is always *sent* through
one shared identity (the configured SMTP/ESP account — see EMAIL_DELIVERABILITY.md),
but the subject and body are templated per user / per report so the recipient sees
who and what each report is for.

Templates use ``{placeholder}`` fields; an unknown or missing placeholder renders
as empty rather than raising, so a hand-edited template can never 500 a send.

Available placeholders: ``employee``, ``date``, ``count``, ``total``, ``job_name``,
``job_number``, ``job_clause`` (a ready-made " for job NN" phrase, blank when none),
``report_name``.
"""
from __future__ import annotations

DEFAULT_SUBJECT = "Reimbursement report — {employee} — {date}"

DEFAULT_BODY = (
    "Hello,\n\n"
    "Please find attached the reimbursement report for {employee}{job_clause}, "
    "generated on {date}.\n\n"
    "Receipts: {count}\n"
    "Total: {total}\n\n"
    "Thank you."
)

# Placeholder values that are really "unset" — used to suppress the job clause and
# avoid stamping the literal default-job constants into a recipient's email.
_PLACEHOLDER_JOB = {"", "Default Job Name", "Default Job Number"}


class _SafeDict(dict):
    """format_map helper: a missing key renders empty instead of raising KeyError."""

    def __missing__(self, key):  # noqa: D401
        return ""


def build_context(*, employee: str = "", count=0, total=None,
                  date: str = "", job_name: str = "", job_number: str = "",
                  report_name: str = "") -> dict:
    """Assemble a render context from report facts. ``total`` may be a number or a
    pre-formatted string; numbers are rendered as ``$1,234.56``."""
    if total is None:
        total_str = ""
    elif isinstance(total, (int, float)):
        total_str = f"${total:,.2f}"
    else:
        total_str = str(total)

    jn = (job_name or "").strip()
    jnum = (job_number or "").strip()
    if jn in _PLACEHOLDER_JOB:
        jn = ""
    if jnum in _PLACEHOLDER_JOB:
        jnum = ""
    if jn and jnum:
        job_clause = f" for job {jn} (#{jnum})"
    elif jn:
        job_clause = f" for job {jn}"
    elif jnum:
        job_clause = f" for job #{jnum}"
    else:
        job_clause = ""

    return {
        "employee":   (employee or "").strip() or "the employee",
        "count":      count,
        "total":      total_str,
        "date":       date,
        "job_name":   jn,
        "job_number": jnum,
        "job_clause": job_clause,
        "report_name": report_name,
    }


def render_report_email(context: dict, subject_template: str | None = None,
                        body_template: str | None = None) -> tuple[str, str]:
    """Render ``(subject, body)`` from ``context`` and optional templates.

    A blank/None template falls back to the built-in default. Rendering never
    raises on a bad placeholder (missing → empty)."""
    subj_t = (subject_template or "").strip() or DEFAULT_SUBJECT
    body_t = (body_template or "").strip() or DEFAULT_BODY
    safe = _SafeDict(context)
    try:
        subject = subj_t.format_map(safe)
    except (ValueError, IndexError):
        subject = DEFAULT_SUBJECT.format_map(safe)
    try:
        body = body_t.format_map(safe)
    except (ValueError, IndexError):
        body = DEFAULT_BODY.format_map(safe)
    return subject.strip(), body
