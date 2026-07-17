"""Food & hotel categories (classification, workbook sections, audit limits) and
the business-tone summary hygiene (obvious summaries scrubbed)."""
from fastapi.testclient import TestClient

import process_receipts as pr
import server
from spreadsheet_theme import CATEGORY_ORDER, build_themed_workbook


def _receipt(category, vendor="X", amount=10.0):
    return {"vendor": vendor, "date": "2026-05-01", "amount": amount,
            "category": category, "_category": category, "ai_summary": ""}


# ── Classification ───────────────────────────────────────────────────────────────

def test_known_food_and_hotel_vendors_classify():
    assert pr.classify_category({"vendor": "McDonald's", "category": "misc",
                                 "_db_exact": True, "_db_category": "food"}) == "food"
    assert pr.classify_category({"vendor": "Hampton Inn", "category": "misc",
                                 "_db_exact": True, "_db_category": "hotel"}) == "hotel"


def test_model_category_synonyms_map():
    assert pr.classify_category({"vendor": "Local Diner", "category": "meals"}) == "food"
    assert pr.classify_category({"vendor": "City Stay", "category": "lodging"}) == "hotel"
    assert pr.classify_category({"vendor": "Depot", "category": "materials"}) == "mats"


def test_venue_words_upgrade_misc_only():
    # Vendor venue words upgrade a misc classification…
    assert pr.classify_category({"vendor": "Main Street Cafe", "category": "misc"}) == "food"
    assert pr.classify_category({"vendor": "Sunset Motel", "category": "misc"}) == "hotel"
    # …but never override an explicit fuel/mats classification.
    assert pr.classify_category({"vendor": "Truck Stop Grill & Fuel",
                                 "category": "fuel",
                                 "ai_summary": "Diesel fill-up"}) == "fuel"


def test_unknown_categories_still_fall_to_misc():
    assert pr.classify_category({"vendor": "Acme", "category": "travel"}) == "misc"


# ── Workbook: five sections ──────────────────────────────────────────────────────

def test_workbook_has_food_and_hotel_sections():
    wb = build_themed_workbook({
        "fuel":  [_receipt("fuel", "Shell")],
        "mats":  [_receipt("mats", "Home Depot")],
        "food":  [_receipt("food", "Chipotle", 15.5)],
        "hotel": [_receipt("hotel", "Hampton Inn", 129.0)],
        "misc":  [_receipt("misc", "Staples")],
    })
    assert [lbl for _c, lbl in CATEGORY_ORDER] == \
        ["Fuel", "Materials", "Food", "Hotel", "Miscellaneous"]
    for sheet in ("Food", "Hotel"):
        assert sheet in wb.sheetnames
    ws = wb["Summary"]
    banners = [str(c.value).strip() for row in ws.iter_rows() for c in row
               if c.value and str(c.value).strip() in ("Food", "Hotel")]
    assert "Food" in banners and "Hotel" in banners
    # Grand TOTAL sums all five subtotals.
    tot = next(str(ws.cell(row=r, column=6).value) for r in range(1, ws.max_row + 1)
               if ws.cell(row=r, column=5).value == "TOTAL")
    assert tot.count("F") == 5


# ── Audit limits for the new categories ──────────────────────────────────────────

def test_audit_limits_for_food_and_hotel():
    c = TestClient(server.app)
    r = c.post("/settings/audit", json={"food_limit": 75, "hotel_limit": 400})
    assert r.status_code == 200 and r.json()["ok"]
    assert pr.AMOUNT_LIMITS["food"] == 75 and pr.AMOUNT_LIMITS["hotel"] == 400
    warns = pr.audit_warning_flags({"amount": 90}, "food")
    assert warns and "exceeds" in warns[0]
    assert pr.audit_warning_flags({"amount": 50}, "food") == []
    # Clear them so other tests see the defaults.
    c.post("/settings/audit", json={"food_limit": "", "hotel_limit": ""})
    assert pr.AMOUNT_LIMITS["food"] is None and pr.AMOUNT_LIMITS["hotel"] is None


# ── Summary hygiene ──────────────────────────────────────────────────────────────

def test_obvious_summaries_scrubbed():
    for s in ("Purchase at Shell", "purchase from Shell", "Shell",
              "Retail purchase", "Purchase", "A purchase made at Shell.",
              "Fuel at a gas station", "Lunch at a restaurant",
              "Transaction at Shell", "Items purchased"):
        assert pr.scrub_obvious_summary(s, "Shell") == "", s


def test_informative_summaries_kept():
    for s in ("Working lunch during on-site job",
              "Overnight lodging near the Riverside job site",
              "Fasteners and lumber for framing work",
              "Fuel for company vehicle between job sites",
              "Purchase of framing lumber and joist hangers"):
        assert pr.scrub_obvious_summary(s, "Shell") == s, s


def test_parse_llm_record_scrubs_summary():
    rec = pr._parse_llm_record('{"vendor": "Shell", "amount": 10.0, '
                               '"summary": "Purchase at Shell", "flags": []}')
    assert rec is not None and rec["ai_summary"] == ""
    rec = pr._parse_llm_record('{"vendor": "Shell", "amount": 10.0, '
                               '"summary": "Fuel for company truck", "flags": []}')
    assert rec["ai_summary"] == "Fuel for company truck"


def test_offline_parser_emits_no_summary():
    out = pr._local_distill_from_ocr("SHELL\nTOTAL $45.20\n05/01/2026")
    assert out["ai_summary"] == ""
