"""Tests for the known-vendor database and offline vendor selection."""
import process_receipts as pr
import vendor_db


# ── match_vendor ───────────────────────────────────────────────────────────────

def test_match_vendor_known_brand_and_category():
    assert vendor_db.match_vendor("SHELL\n123 Main St\nTOTAL $45.20") == ("Shell", "fuel")
    assert vendor_db.match_vendor("THE HOME DEPOT #1234") == ("The Home Depot", "mats")
    assert vendor_db.match_vendor("WALMART SUPERCENTER") == ("Walmart", "misc")


def test_match_vendor_longest_alias_wins():
    # "home depot" must beat a bare "depot"; "phillips 66" must beat "66".
    assert vendor_db.match_vendor("WELCOME TO HOME DEPOT")[0] == "The Home Depot"
    assert vendor_db.match_vendor("PHILLIPS 66 STATION")[0] == "Phillips 66"


def test_match_vendor_none_when_unknown():
    assert vendor_db.match_vendor("JOE'S CORNER CAFE\nTOTAL $9.00") is None
    assert vendor_db.match_vendor("") is None


def test_match_vendor_is_word_bounded():
    # "bp" should not match inside "subprime" or similar; ensure no false hit.
    assert vendor_db.match_vendor("SUBPRIME LENDING LLC") is None


# ── address skipping in the heuristic fallback ──────────────────────────────────

def test_looks_like_address():
    assert pr._looks_like_address("123 Main St")
    assert pr._looks_like_address("Springfield, IL 62704")
    assert pr._looks_like_address("(555) 123-4567")
    assert pr._looks_like_address("www.example.com")
    assert not pr._looks_like_address("JOE'S DINER")


def test_guess_vendor_line_skips_address():
    txt = "123 Main St\nJOE'S DINER\nSpringfield, IL 62704\nTOTAL $12.00"
    assert pr._guess_vendor_line(txt) == "JOE'S DINER"


def test_guess_vendor_line_falls_back_to_first_alpha_line():
    # When every early line looks address-like, still return something usable.
    txt = "100 W 5th Ave\n200 Industrial Rd"
    assert pr._guess_vendor_line(txt)  # non-empty fallback


# ── integration through the offline parser ──────────────────────────────────────

def test_offline_parser_prefers_known_vendor_over_address():
    txt = "123 Main Street\nSHELL\nUNLEADED\nTOTAL $45.20\n05/01/2026"
    out = pr._local_distill_from_ocr(txt)
    assert out["vendor"] == "Shell"      # not "123 Main Street"
    assert out["category"] == "fuel"


def test_offline_parser_picks_business_name_when_vendor_unknown():
    txt = "456 Commerce Blvd\nACME WIDGETS LLC\nTOTAL $30.00"
    out = pr._local_distill_from_ocr(txt)
    assert out["vendor"] == "ACME WIDGETS LLC"   # address line skipped
