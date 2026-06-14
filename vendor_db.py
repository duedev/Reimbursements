"""Known-vendor reference database for offline (no-LLM) receipt parsing.

When LM Studio is unavailable the offline rule-based parser has to pick a vendor
name from raw OCR text. The naive "first text line" heuristic frequently grabbed
the store address instead of the business name. This module cross-references the
OCR text against a curated list of real brands so a known vendor is named (and
categorised) correctly, and exposes the brand/keyword sets that also drive
category scoring in ``process_receipts``.

Categories are limited to the app's taxonomy: "fuel", "mats", "misc".
"""
import re

# ── Brand / keyword sets (also consumed by process_receipts for category scoring)
# These intentionally include generic, non-brand keywords ("gas station",
# "building supply") that help detect a category but are NOT vendor names — vendor
# *naming* uses KNOWN_VENDORS below.
FUEL_VENDORS = {
    "shell", "chevron", "arco", "mobil", "exxon", "bp", "76", "valero",
    "marathon", "speedway", "sunoco", "citgo", "texaco", "pilot", "loves",
    "love's", "casey", "kwik trip", "wawa", "quiktrip", "circle k", "ampm",
    "gas station", "fuel station", "petro", "petroleum", "flying j",
    "bucees", "buc-ee", "racetrac", "racetrack", "cenex", "sinclair",
    "murphy", "murphy usa", "tom thumb", "stripes", "kwik fill",
    "kum & go", "sheetz", "thorntons", "mapco", "gulf", "hess",
    "conoco", "phillips 66", "pdq", "getgo", "flash foods", "moto mart",
    "pantry", "road ranger", "git n go", "corner store",
}

FUEL_KEYWORDS = {
    "gas", "gasoline", "diesel", "petrol", "fuel", "pump", "gallon",
    "gallons", "unleaded", "e85", "fill-up",
    "fill up", "fueling", "service station", "gas pump", "octane",
    "auto fuel", "motor fuel", "regular unleaded", "premium unleaded",
    "price/gal", "per gallon",
}

MATS_VENDORS = {
    "home depot", "lowes", "lowe's", "menards", "ace hardware", "true value",
    "harbor freight", "fastenal", "grainger", "blueprint", "print shop",
    "reprographics", "planning department", "building supply",
}

# ── Curated brand database for vendor *naming* ──────────────────────────────────
# canonical display name → (category, {alias keywords as they appear, lowercased}).
# Aliases are matched word-bounded against the lowercased OCR text; on multiple
# hits the longest alias wins (so "home depot" beats a stray "depot", and
# "phillips 66" beats "66"), tie-broken by earliest position in the text.
KNOWN_VENDORS: dict[str, tuple[str, set[str]]] = {
    # Fuel / gas stations
    "Shell":          ("fuel", {"shell"}),
    "Chevron":        ("fuel", {"chevron"}),
    "ARCO":           ("fuel", {"arco"}),
    "Mobil":          ("fuel", {"mobil"}),
    "Exxon":          ("fuel", {"exxon", "exxonmobil"}),
    "BP":             ("fuel", {"bp"}),
    "76":             ("fuel", {"76 gas", "phillips 76", "union 76"}),
    "Valero":         ("fuel", {"valero"}),
    "Marathon":       ("fuel", {"marathon"}),
    "Speedway":       ("fuel", {"speedway"}),
    "Sunoco":         ("fuel", {"sunoco"}),
    "Citgo":          ("fuel", {"citgo"}),
    "Texaco":         ("fuel", {"texaco"}),
    "Pilot":          ("fuel", {"pilot", "pilot flying j"}),
    "Flying J":       ("fuel", {"flying j"}),
    "Love's":         ("fuel", {"loves", "love's", "love's travel"}),
    "Casey's":        ("fuel", {"casey", "casey's", "caseys"}),
    "Kwik Trip":      ("fuel", {"kwik trip"}),
    "QuikTrip":       ("fuel", {"quiktrip", "quik trip"}),
    "Wawa":           ("fuel", {"wawa"}),
    "Circle K":       ("fuel", {"circle k"}),
    "AMPM":           ("fuel", {"ampm", "am/pm"}),
    "Buc-ee's":       ("fuel", {"bucees", "buc-ee", "buc-ee's"}),
    "RaceTrac":       ("fuel", {"racetrac", "racetrack"}),
    "Cenex":          ("fuel", {"cenex"}),
    "Sinclair":       ("fuel", {"sinclair"}),
    "Murphy USA":     ("fuel", {"murphy usa", "murphy"}),
    "Sheetz":         ("fuel", {"sheetz"}),
    "Gulf":           ("fuel", {"gulf"}),
    "Hess":           ("fuel", {"hess"}),
    "Conoco":         ("fuel", {"conoco"}),
    "Phillips 66":    ("fuel", {"phillips 66"}),
    "GetGo":          ("fuel", {"getgo"}),
    "Kum & Go":       ("fuel", {"kum & go", "kum and go"}),
    "Thorntons":      ("fuel", {"thorntons"}),
    "Costco Gas":     ("fuel", {"costco gas", "costco gasoline", "costco fuel"}),
    # Hardware / materials / print (category "mats")
    "The Home Depot": ("mats", {"home depot", "homedepot", "the home depot"}),
    "Lowe's":         ("mats", {"lowes", "lowe's"}),
    "Menards":        ("mats", {"menards"}),
    "Ace Hardware":   ("mats", {"ace hardware"}),
    "True Value":     ("mats", {"true value"}),
    "Harbor Freight": ("mats", {"harbor freight"}),
    "Fastenal":       ("mats", {"fastenal"}),
    "Grainger":       ("mats", {"grainger", "w.w. grainger"}),
    "Northern Tool":  ("mats", {"northern tool"}),
    "Sherwin-Williams": ("mats", {"sherwin-williams", "sherwin williams"}),
    "FedEx Office":   ("mats", {"fedex office", "kinkos", "kinko's"}),
    "The UPS Store":  ("mats", {"ups store", "the ups store"}),
    "Staples":        ("mats", {"staples"}),
    "Office Depot":   ("mats", {"office depot", "officemax", "office max"}),
    # General retail / grocery / food / misc
    "Walmart":        ("misc", {"walmart", "wal-mart"}),
    "Costco":         ("misc", {"costco", "costco wholesale"}),
    "Target":         ("misc", {"target"}),
    "Sam's Club":     ("misc", {"sam's club", "sams club"}),
    "Kroger":         ("misc", {"kroger"}),
    "Safeway":        ("misc", {"safeway"}),
    "Albertsons":     ("misc", {"albertsons"}),
    "Trader Joe's":   ("misc", {"trader joe", "trader joe's"}),
    "Whole Foods":    ("misc", {"whole foods"}),
    "Walgreens":      ("misc", {"walgreens"}),
    "CVS":            ("misc", {"cvs pharmacy", "cvs"}),
    "Best Buy":       ("misc", {"best buy"}),
    "Starbucks":      ("misc", {"starbucks"}),
    "McDonald's":     ("misc", {"mcdonald's", "mcdonalds"}),
    "Subway":         ("misc", {"subway"}),
    "Chipotle":       ("misc", {"chipotle"}),
    "Panera Bread":   ("misc", {"panera"}),
    "Dunkin'":        ("misc", {"dunkin", "dunkin'", "dunkin donuts"}),
    "Amazon":         ("misc", {"amazon", "amazon.com", "amzn"}),
    "AutoZone":       ("misc", {"autozone"}),
    "O'Reilly Auto Parts": ("misc", {"o'reilly", "oreilly", "o reilly auto"}),
    "NAPA Auto Parts": ("misc", {"napa auto", "napa"}),
}


def _boundary_pattern(alias: str) -> "re.Pattern[str]":
    """Word-boundary matcher for one alias against lowercased text.

    Mirrors process_receipts._kw_pattern: a purely numeric alias additionally
    must not touch digits, '.', ',', '#' or '$' so prices/store numbers/zips
    don't read as a brand. (Numeric-only brands aren't in KNOWN_VENDORS as bare
    digits, but this keeps the matcher safe for entries like "76 gas".)
    """
    esc = re.escape(alias)
    if alias.isdigit():
        return re.compile(rf"(?<![a-z0-9.,#$]){esc}(?![a-z0-9.,])")
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")


_VENDOR_ALIAS_PATTERNS: list[tuple[str, str, str, "re.Pattern[str]"]] = [
    (canonical, category, alias, _boundary_pattern(alias))
    for canonical, (category, aliases) in KNOWN_VENDORS.items()
    for alias in aliases
]


def match_vendor(text: str) -> "tuple[str, str] | None":
    """Cross-reference OCR text against the known-vendor database.

    Returns ``(canonical_name, category)`` for the best brand hit, or None when
    no known vendor is present. The most specific (longest) alias wins, so a
    multi-word brand is preferred over a generic single word it contains.
    """
    if not text:
        return None
    low = text.lower()
    best: tuple[int, int, str, str] | None = None  # (alias_len, -position, name, cat)
    for canonical, category, alias, rx in _VENDOR_ALIAS_PATTERNS:
        m = rx.search(low)
        if m is None:
            continue
        key = (len(alias), -m.start(), canonical, category)
        if best is None or key > best:
            best = key
    if best is None:
        return None
    return best[2], best[3]
