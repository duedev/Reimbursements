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
