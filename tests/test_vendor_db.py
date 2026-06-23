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


# ── Glyph-normalized (OCR confusion) matching ────────────────────────────────────

def test_seven_eleven_via_glyph_normalization():
    # Stylized 7-ELEVEN font reads V as U → "7-ELEUEN"; glyph folding rescues it.
    assert vendor_db.match_vendor("7-ELEUEN") == ("7-Eleven", "fuel")
    assert vendor_db.match_vendor("7-ELEVEN #233 UNLEADED") == ("7-Eleven", "fuel")
    # End-to-end-style raw OCR text still resolves (offline parser feeds this).
    txt = "7-ELEUEN\n123 MAIN ST\nUNLEADED\nTOTAL $40.00\n06/01/2026"
    assert vendor_db.match_vendor(txt) == ("7-Eleven", "fuel")


def test_glyph_pass_only_runs_after_exact_miss():
    # A clean brand still resolves via the exact pass (byte-for-byte path).
    assert vendor_db.match_vendor("SHELL") == ("Shell", "fuel")


def test_home_depot_via_printed_slogan():
    # The brand prints only a logo; the readable text is the tagline.
    assert vendor_db.match_vendor("How doers get more done.") == ("The Home Depot", "mats")
    assert vendor_db.match_vendor("Thank you — never stop improving") == ("Lowe's", "mats")


# ── Expanded database sampling (new brands across the three groups) ───────────────

def test_sampling_of_new_brands():
    cases = {
        "7-ELEVEN":           ("7-Eleven", "fuel"),
        "MAVERIK COUNTRY STORE": ("Maverik", "fuel"),
        "PUBLIX SUPER MARKET": ("Publix", "misc"),
        "ALDI":               ("Aldi", "misc"),
        "CHICK-FIL-A":        ("Chick-fil-A", "misc"),
        "HAMPTON INN & SUITES": ("Hampton Inn", "misc"),
        "FERGUSON PLUMBING":  ("Ferguson", "mats"),
        "SHERWIN WILLIAMS":   ("Sherwin-Williams", "mats"),
    }
    for text, expected in cases.items():
        assert vendor_db.match_vendor(text) == expected, text


def test_speedway_kept_separate_from_seven_eleven():
    assert vendor_db.match_vendor("SPEEDWAY 04421") == ("Speedway", "fuel")
    assert "Speedway" in vendor_db.KNOWN_VENDORS
    assert "7-Eleven" in vendor_db.KNOWN_VENDORS


# ── Numeric-brand safety (digits never folded) ───────────────────────────────────

def test_numeric_brand_safety():
    # A store #76 and a $9.76 price next to a known mats brand must not become a
    # numeric "76" fuel hit — the longer, real brand wins and 76 is guarded.
    assert vendor_db.match_vendor("OFFICE DEPOT STORE #76\nPAPER 9.76") == ("Office Depot", "mats")
    # Standalone branded 76 still resolves.
    assert vendor_db.match_vendor("PHILLIPS 66 STATION") == ("Phillips 66", "fuel")


# ── Bounded fuzzy backstop (opt-in only) ─────────────────────────────────────────

def test_fuzzy_near_miss_matches_only_when_enabled():
    # A one-character OCR slip on a short brand name.
    assert vendor_db.match_vendor("Costo") is None                    # default: no fuzzy
    assert vendor_db.match_vendor("Costo", fuzzy=True) == ("Costco", "misc")
    assert vendor_db.match_vendor("Walmrt", fuzzy=True) == ("Walmart", "misc")


def test_fuzzy_returns_none_for_genuine_unknown():
    assert vendor_db.match_vendor("Joe's Corner Cafe", fuzzy=True) is None


def test_fuzzy_never_runs_on_a_whole_receipt():
    # The fuzzy backstop guards against long input so it can't fuzzy a receipt.
    long_text = "Joe's Corner Cafe\n" + "ITEM 1.00\n" * 20 + "TOTAL 20.00"
    assert vendor_db._fuzzy_match_vendor(long_text) is None


# ── Derived scoring sets keep one source of truth ────────────────────────────────

def test_scoring_sets_contain_generics_and_brand_strings():
    # Preserved generic, non-brand keywords.
    assert {"gas station", "petroleum"} <= vendor_db.FUEL_VENDORS
    assert {"building supply", "blueprint", "reprographics"} <= vendor_db.MATS_VENDORS
    # Real brand aliases derived from the brand dicts.
    assert {"shell", "76"} <= vendor_db.FUEL_VENDORS
    assert {"home depot", "sherwin williams"} <= vendor_db.MATS_VENDORS
    # Slogans are excluded from the scoring sets.
    assert "how doers get more done" not in vendor_db.MATS_VENDORS


def test_known_vendors_shape_preserved():
    assert isinstance(vendor_db.KNOWN_VENDORS, dict)
    for name, value in vendor_db.KNOWN_VENDORS.items():
        assert isinstance(name, str)
        cat, aliases = value
        assert cat in ("fuel", "mats", "misc")
        assert isinstance(aliases, set) and aliases
    # The expansion landed (~300 canonical brands).
    assert len(vendor_db.KNOWN_VENDORS) >= 250


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
