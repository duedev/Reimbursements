"""Tests for vendor/keyword-based category classification."""
from process_receipts import classify_category


def test_fuel_vendor_match_overrides_model_category():
    data = {"vendor": "Shell Oil 4421", "category": "misc", "ai_summary": "Snacks"}
    assert classify_category(data) == "fuel"


def test_fuel_model_category_with_keyword_support():
    data = {"vendor": "Joe's Stop", "category": "fuel", "ai_summary": "Diesel fill up"}
    assert classify_category(data) == "fuel"


def test_fuel_model_category_without_signal_falls_through_to_model():
    # No fuel vendor/keyword evidence: the fuel branches don't fire and the
    # final fallback returns the model's own category unchanged.
    data = {"vendor": "Olive Garden", "category": "fuel", "ai_summary": "Team dinner"}
    assert classify_category(data) == "fuel"


def test_mats_vendor_match():
    data = {"vendor": "The Home Depot #1234", "category": "misc"}
    assert classify_category(data) == "mats"


def test_materials_alias_normalised_to_mats():
    data = {"vendor": "Some Lumber Yard", "category": "Materials"}
    assert classify_category(data) == "mats"


def test_unknown_category_defaults_to_misc():
    data = {"vendor": "Starbucks", "category": "coffee"}
    assert classify_category(data) == "misc"


def test_missing_fields_default_to_misc():
    assert classify_category({}) == "misc"


def test_restaurant_stays_misc():
    data = {"vendor": "Butch's Grinders", "category": "misc",
            "ai_summary": "Lunch sandwiches"}
    assert classify_category(data) == "misc"


def test_fuel_keyword_in_summary_promotes_gas_station():
    data = {"vendor": "Circle K", "category": "misc",
            "ai_summary": "Unleaded gasoline purchase"}
    assert classify_category(data) == "fuel"


# ── Raw-OCR fuel scoring must use word boundaries ─────────────────────────────
# Regression: substring matching let "76" match addresses/prices and "regular"
# match REGULAR PRICE, flipping almost every receipt with raw OCR text to fuel.

def test_restaurant_with_raw_ocr_stays_misc():
    data = {"vendor": "Butch's Grinders", "category": "misc",
            "ai_summary": "Lunch sandwiches",
            "_raw_ocr": ("BUTCH'S GRINDERS\n1376 MAIN ST\n"
                         "REGULAR SUB 8.99\nPREMIUM SUB 10.99\n"
                         "SUBTOTAL 19.98\nTAX 1.62\nTOTAL 21.60")}
    assert classify_category(data) == "misc"


def test_hotel_with_raw_ocr_stays_misc():
    data = {"vendor": "Hampton Inn", "category": "misc",
            "ai_summary": "One night hotel stay",
            "_raw_ocr": ("HAMPTON INN\n762 AIRPORT RD\nLAS VEGAS NV\n"
                         "ROOM 204 REGULAR RATE 129.00\nTOTAL 154.37")}
    assert classify_category(data) == "misc"


def test_price_ending_in_76_is_not_a_fuel_vendor():
    data = {"vendor": "Office Depot", "category": "misc",
            "ai_summary": "Printer paper",
            "_raw_ocr": "OFFICE DEPOT STORE #76\nPAPER 9.76\nTOTAL 9.76"}
    assert classify_category(data) == "misc"


def test_real_gas_receipt_with_raw_ocr_is_fuel():
    data = {"vendor": "Quick Stop", "category": "misc", "ai_summary": "",
            "_raw_ocr": ("SHELL OIL 57444\nPUMP 4\nUNLEADED 12.503 GAL\n"
                         "PRICE/GAL $3.499\nTOTAL $45.20")}
    assert classify_category(data) == "fuel"


def test_standalone_76_branding_is_fuel():
    data = {"vendor": "76 Station #4421", "category": "misc",
            "ai_summary": "Diesel"}
    assert classify_category(data) == "fuel"
