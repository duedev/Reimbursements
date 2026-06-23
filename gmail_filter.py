"""gmail_filter.py — build an importable Gmail filter that labels receipt mail.

The robust way to feed the email intake WITHOUT a fragile per-brand sender
allowlist: match receipt KEYWORDS in the subject/body, apply a Gmail label, and
point the app's intake at that label instead of INBOX. Keyword matching is the
right tool here because most fuel brands don't email per-transaction receipts,
the ones that do don't publish a sender domain, and forwarding / privacy relays
(DuckDuckGo) rewrite the From: header anyway — but the subject/body survive.

This module is pure and dependency-free: it builds the Gmail search query and the
importable filter XML (Gmail → Settings → Filters and Blocked Addresses → Import
filters). The handful of *verified* fuel-receipt sender domains are folded in as
optional ``from:`` clauses (belt-and-suspenders), not the load-bearing part.

CLI:  ``python gmail_filter.py > gmail_receipts_filter.xml``
"""
from __future__ import annotations

from xml.sax.saxutils import escape

try:  # the verified senders live with the intake module (single source of truth)
    from email_intake import FUEL_RECEIPT_SENDERS
except Exception:  # pragma: no cover - keep importable standalone
    FUEL_RECEIPT_SENDERS = []

# Receipt signals, strongest first. Phrases are quoted so Gmail matches them
# verbatim; bare tokens (gallons) and subject: clauses widen recall.
RECEIPT_KEYWORDS = [
    '"thank you for your purchase"',
    '"thanks for your purchase"',
    '"transaction total"',
    '"amount charged"',
    '"your receipt"',
    '"order confirmation"',
    '"e-receipt"',
    '"payment received"',
    '"fuel receipt"',
    '"price per gallon"',
    "gallons",
    '"fuel rewards"',
    "subject:receipt",
    "subject:invoice",
]

# Mail that flooded the intake before the filter existed (Google security alerts,
# YouTube notifications) — never a receipt, excluded so a shared inbox stays clean.
EXCLUDE_SENDERS = ["google.com", "youtube.com", "accounts.google.com"]

DEFAULT_LABEL = "Receipts"


def build_search_query(keywords: list[str] | None = None,
                       senders: list[str] | None = None,
                       exclude: list[str] | None = None) -> str:
    """Build the Gmail search query: OR-group of keywords + verified senders, minus
    the known-noise senders. Braces ``{ }`` are Gmail's OR operator."""
    keywords = list(RECEIPT_KEYWORDS if keywords is None else keywords)
    senders  = list(FUEL_RECEIPT_SENDERS if senders is None else senders)
    exclude  = list(EXCLUDE_SENDERS if exclude is None else exclude)
    terms = list(keywords) + [f"from:{s}" for s in senders]
    or_group = "{" + " ".join(terms) + "}"
    tail = "".join(f" -from:{e}" for e in exclude)
    return or_group + tail


def _attr(val: str) -> str:
    """XML-escape an attribute value (the query) for single-quoted attributes."""
    return escape(val, {'"': "&quot;", "'": "&apos;"})


def build_gmail_filter_xml(label: str = DEFAULT_LABEL,
                           query: str | None = None,
                           skip_inbox: bool = False) -> str:
    """Render an importable Gmail filter (Atom + apps: schema) that applies ``label``
    to mail matching the receipt query and never marks it spam."""
    q = query if query is not None else build_search_query()
    props: list[tuple[str, str]] = [
        ("hasTheWord", q),
        ("label", label),
        ("shouldNeverSpam", "true"),
    ]
    if skip_inbox:
        props.append(("shouldArchive", "true"))
    prop_lines = "\n".join(
        "    <apps:property name='%s' value='%s'/>" % (name, _attr(val))
        for name, val in props
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<feed xmlns='http://www.w3.org/2005/Atom' "
        "xmlns:apps='http://schemas.google.com/apps/2006'>\n"
        "  <title>Mail Filters — Receipts</title>\n"
        "  <entry>\n"
        "    <category term='filter'></category>\n"
        f"    <title>{escape(label)}</title>\n"
        "    <content></content>\n"
        f"{prop_lines}\n"
        "  </entry>\n"
        "</feed>\n"
    )


if __name__ == "__main__":  # pragma: no cover - CLI helper to regenerate the file
    print(build_gmail_filter_xml(), end="")
