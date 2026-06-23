"""Gmail keyword filter generation (the primary email-intake setup path)."""
import xml.etree.ElementTree as ET

import email_intake
import gmail_filter


def test_search_query_has_keywords_senders_and_excludes():
    q = gmail_filter.build_search_query()
    assert q.startswith("{") and "}" in q
    assert '"transaction total"' in q and "gallons" in q
    assert "subject:receipt" in q
    # verified senders folded in as from: clauses
    assert "from:gasbuddy.com" in q
    assert "from:notifications.chevronmobileapp.com" in q
    # known-noise senders excluded (the mail that flooded the first run)
    assert "-from:google.com" in q and "-from:youtube.com" in q


def test_senders_single_source_of_truth():
    q = gmail_filter.build_search_query()
    for s in email_intake.FUEL_RECEIPT_SENDERS:
        assert f"from:{s}" in q


def test_filter_xml_is_wellformed_and_labels():
    xml = gmail_filter.build_gmail_filter_xml(label="Receipts")
    ET.fromstring(xml)                              # raises if malformed
    assert "name='label' value='Receipts'" in xml
    assert "shouldNeverSpam" in xml
    # query embedded with attribute quotes escaped
    assert "&quot;transaction total&quot;" in xml


def test_filter_xml_skip_inbox_optional():
    assert "shouldArchive" not in gmail_filter.build_gmail_filter_xml()
    assert "shouldArchive" in gmail_filter.build_gmail_filter_xml(skip_inbox=True)


def test_custom_query_passthrough():
    xml = gmail_filter.build_gmail_filter_xml(query="subject:receipt")
    assert "value='subject:receipt'" in xml
