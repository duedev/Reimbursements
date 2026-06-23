"""Known-vendor reference database for offline (no-LLM) receipt parsing.

When LM Studio is unavailable the offline rule-based parser has to pick a vendor
name from raw OCR text. The naive "first text line" heuristic frequently grabbed
the store address instead of the business name. This module cross-references the
OCR text against a curated list of real brands so a known vendor is named (and
categorised) correctly, and exposes the brand/keyword sets that also drive
category scoring in ``process_receipts``.

Categories are limited to the app's taxonomy: "fuel", "mats", "misc".

Matching is deliberately CONSERVATIVE and runs in passes so it never misfires on
ordinary receipt text:

1. **Exact** — word-bounded match against the raw lowercased text (the original,
   long-standing behaviour; runs first so it is byte-for-byte unchanged).
2. **Glyph-normalized** — only when the exact pass finds nothing, the text and the
   aliases are folded through :func:`_normalize_ocr_strict` (a tiny set of letter
   OCR confusions: ``rn→m``, ``vv→w``, ``cl→d``, ``u→v``) so a stylised font that
   makes ``7-ELEVEN`` read as ``7-ELEUEN`` still resolves to ``7-Eleven``. Digits
   are never folded, protecting numeric brands like ``76``.
3. **Bounded fuzzy** (opt-in, off by default) — a tight ``difflib`` ratio backstop
   that only ever runs on a SHORT vendor-name candidate (never the whole receipt).

A confident exact/glyph hit is also used to CANONICALIZE the displayed vendor name
(see ``process_receipts.canonicalize_vendor``) — e.g. rewriting ``7-ELEUEN`` to the
canonical ``7-Eleven``.
"""
import difflib
import re

# ── Curated brand database for vendor *naming* ──────────────────────────────────
# Brands are grouped by category as {canonical display name: {alias keywords as
# they appear in OCR, lowercased}} and merged below into the single public
# ``KNOWN_VENDORS`` map. Keeping the three groups separate gives one source of
# truth that ALSO derives the category-scoring sets (FUEL_VENDORS / MATS_VENDORS).
#
# Alias rules: ≥3 chars (unless numeric-guarded, e.g. "76"), never a bare common
# English word (use a multi-word form — "delta air lines", not "delta"). On
# multiple hits the longest alias wins, tie-broken by earliest position.

_FUEL_BRANDS: dict[str, set[str]] = {
    # Majors / national fuel brands
    "Shell":          {"shell"},
    "Chevron":        {"chevron"},
    "ARCO":           {"arco"},
    "Mobil":          {"mobil"},
    "Exxon":          {"exxon", "exxonmobil"},
    "BP":             {"bp"},
    "76":             {"76", "76 gas", "phillips 76", "union 76"},
    "Valero":         {"valero"},
    "Marathon":       {"marathon"},
    "Sunoco":         {"sunoco"},
    "Citgo":          {"citgo"},
    "Texaco":         {"texaco"},
    "Gulf":           {"gulf"},
    "Hess":           {"hess"},
    "Conoco":         {"conoco"},
    "Phillips 66":    {"phillips 66"},
    "Sinclair":       {"sinclair"},
    "Cenex":          {"cenex"},
    "Diamond Shamrock": {"diamond shamrock"},
    "Esso":           {"esso"},
    "Petro-Canada":   {"petro-canada", "petro canada"},
    "Irving":         {"irving oil", "irving gas"},
    "Clark":          {"clark gas", "clark oil"},
    "Fina":           {"fina gas"},
    "Mystik":         {"mystik"},
    "Ultramar":       {"ultramar"},
    # Travel centers
    "Pilot":          {"pilot", "pilot flying j"},
    "Flying J":       {"flying j"},
    "Love's":         {"loves", "love's", "love's travel"},
    "TravelCenters of America": {"travelcenters", "ta petro", "travel centers of america"},
    "Petro Stopping Centers": {"petro stopping"},
    "Sapp Bros":      {"sapp bros"},
    # Convenience-store + fuel chains
    "7-Eleven":       {"7-eleven", "7 eleven", "7eleven", "seven eleven"},
    "Speedway":       {"speedway"},
    "Casey's":        {"casey", "casey's", "caseys"},
    "Kwik Trip":      {"kwik trip"},
    "Kwik Star":      {"kwik star"},
    "QuikTrip":       {"quiktrip", "quik trip"},
    "Wawa":           {"wawa"},
    "Circle K":       {"circle k"},
    "AMPM":           {"ampm", "am/pm"},
    "Buc-ee's":       {"bucees", "buc-ee", "buc-ee's"},
    "RaceTrac":       {"racetrac", "racetrack"},
    "Murphy USA":     {"murphy usa", "murphy"},
    "Murphy Express": {"murphy express"},
    "Sheetz":         {"sheetz"},
    "GetGo":          {"getgo"},
    "Kum & Go":       {"kum & go", "kum and go"},
    "Thorntons":      {"thorntons"},
    "Maverik":        {"maverik"},
    "Holiday Stationstores": {"holiday stationstores", "holiday station"},
    "Cumberland Farms": {"cumberland farms", "cumberland farm"},
    "Royal Farms":    {"royal farms"},
    "Rutter's":       {"rutter's", "rutters"},
    "Turkey Hill":    {"turkey hill"},
    "QuickChek":      {"quickchek", "quick chek"},
    "Stewart's Shops": {"stewart's shops", "stewarts shops"},
    "Allsup's":       {"allsup's", "allsups"},
    "Yesway":         {"yesway"},
    "Spinx":          {"spinx"},
    "Cefco":          {"cefco"},
    "Stinker Stores": {"stinker stores"},
    "United Dairy Farmers": {"united dairy farmers"},
    "Tom Thumb":      {"tom thumb"},
    "Stripes":        {"stripes"},
    "Kwik Fill":      {"kwik fill"},
    "MAPCO":          {"mapco"},
    "PDQ":            {"pdq"},
    "Flash Foods":    {"flash foods"},
    "Moto Mart":      {"moto mart", "motomart"},
    "Road Ranger":    {"road ranger"},
    "Git-N-Go":       {"git-n-go", "git n go"},
    "E-Z Mart":       {"e-z mart", "ez mart"},
    "Loaf 'N Jug":    {"loaf n jug", "loaf 'n jug"},
    "Kwik Shop":      {"kwik shop"},
    "Mountain Express": {"mountain express"},
    "Pacific Pride":  {"pacific pride"},
    "Costco Gas":     {"costco gas", "costco gasoline", "costco fuel"},
    "Sam's Club Fuel": {"sam's club fuel", "sams club gas"},
}

_MATS_BRANDS: dict[str, set[str]] = {
    # Home-improvement / big-box hardware
    "The Home Depot": {"home depot", "homedepot", "the home depot"},
    "Lowe's":         {"lowes", "lowe's"},
    "Menards":        {"menards"},
    "Ace Hardware":   {"ace hardware"},
    "True Value":     {"true value"},
    "Harbor Freight": {"harbor freight"},
    "Northern Tool":  {"northern tool"},
    "Tractor Supply": {"tractor supply"},
    "Rural King":     {"rural king"},
    "Do it Best":     {"do it best"},
    "Sutherlands":    {"sutherlands"},
    # Lumber / building-materials yards
    "84 Lumber":      {"84 lumber"},
    "Builders FirstSource": {"builders firstsource"},
    "Carter Lumber":  {"carter lumber"},
    "McCoy's Building Supply": {"mccoy's building", "mccoys building supply"},
    "ABC Supply":     {"abc supply"},
    "Beacon Building Products": {"beacon building", "beacon roofing"},
    "SiteOne":        {"siteone"},
    "HD Supply":      {"hd supply"},
    "White Cap":      {"white cap"},
    # Distribution: plumbing / electrical / industrial
    "Ferguson":       {"ferguson"},
    "Grainger":       {"grainger", "w.w. grainger"},
    "Fastenal":       {"fastenal"},
    "MSC Industrial": {"msc industrial"},
    "Graybar":        {"graybar"},
    "City Electric Supply": {"city electric supply"},
    "Rexel":          {"rexel"},
    "Platt Electric": {"platt electric"},
    "Winsupply":      {"winsupply", "winnelson"},
    "Johnstone Supply": {"johnstone supply"},
    # Paint
    "Sherwin-Williams": {"sherwin-williams", "sherwin williams"},
    "Benjamin Moore": {"benjamin moore"},
    "PPG Paints":     {"ppg paints", "ppg paint"},
    "Behr":           {"behr"},
    "Kelly-Moore":    {"kelly-moore", "kelly moore paints"},
    "Dunn-Edwards":   {"dunn-edwards", "dunn edwards"},
    # Flooring / tile
    "Floor & Decor":  {"floor & decor", "floor and decor", "floor decor"},
    "The Tile Shop":  {"tile shop"},
    "LL Flooring":    {"lumber liquidators", "ll flooring"},
    # Print / office supply (the "blueprint / reprographics" lane)
    "FedEx Office":   {"fedex office", "kinkos", "kinko's"},
    "The UPS Store":  {"ups store", "the ups store"},
    "Staples":        {"staples"},
    "Office Depot":   {"office depot", "officemax", "office max"},
    "Sir Speedy":     {"sir speedy"},
    "Minuteman Press": {"minuteman press"},
    "AlphaGraphics":  {"alphagraphics"},
    "W.B. Mason":     {"w.b. mason", "wb mason"},
    "Quill":          {"quill.com"},
}

_MISC_BRANDS: dict[str, set[str]] = {
    # Warehouse / big-box / department / discount
    "Walmart":        {"walmart", "wal-mart"},
    "Costco":         {"costco", "costco wholesale"},
    "Target":         {"target"},
    "Sam's Club":     {"sam's club", "sams club"},
    "BJ's Wholesale": {"bj's wholesale", "bjs wholesale"},
    "Kohl's":         {"kohl's", "kohls"},
    "Macy's":         {"macy's", "macys"},
    "Nordstrom":      {"nordstrom"},
    "JCPenney":       {"jcpenney", "jc penney"},
    "Dillard's":      {"dillard's", "dillards"},
    "TJ Maxx":        {"tj maxx", "t.j. maxx"},
    "Marshalls":      {"marshalls"},
    "Ross":           {"ross dress for less", "ross stores"},
    "Burlington":     {"burlington stores", "burlington coat"},
    "Dollar General": {"dollar general"},
    "Dollar Tree":    {"dollar tree"},
    "Family Dollar":  {"family dollar"},
    "Five Below":     {"five below"},
    "Big Lots":       {"big lots"},
    "IKEA":           {"ikea"},
    "At Home":        {"at home store"},
    "The Container Store": {"container store"},
    "World Market":   {"world market"},
    "Crate & Barrel": {"crate & barrel", "crate and barrel"},
    "Pottery Barn":   {"pottery barn"},
    "Williams-Sonoma": {"williams-sonoma", "williams sonoma"},
    "Bed Bath & Beyond": {"bed bath & beyond", "bed bath and beyond"},
    "Bath & Body Works": {"bath & body works", "bath and body works"},
    "Wayfair":        {"wayfair"},
    # Grocery
    "Kroger":         {"kroger"},
    "Safeway":        {"safeway"},
    "Albertsons":     {"albertsons"},
    "Publix":         {"publix"},
    "Aldi":           {"aldi"},
    "Meijer":         {"meijer"},
    "H-E-B":          {"h-e-b", "heb grocery"},
    "Wegmans":        {"wegmans"},
    "Food Lion":      {"food lion"},
    "Giant Eagle":    {"giant eagle"},
    "Giant Food":     {"giant food"},
    "Stop & Shop":    {"stop & shop", "stop and shop"},
    "ShopRite":       {"shoprite"},
    "WinCo Foods":    {"winco foods", "winco"},
    "Sprouts":        {"sprouts farmers", "sprouts market"},
    "Vons":           {"vons"},
    "Ralphs":         {"ralphs"},
    "Hy-Vee":         {"hy-vee", "hyvee"},
    "Harris Teeter":  {"harris teeter"},
    "Save A Lot":     {"save a lot", "save-a-lot"},
    "Fry's Food":     {"fry's food", "frys food"},
    "Acme Markets":   {"acme markets"},
    "Jewel-Osco":     {"jewel-osco", "jewel osco"},
    "Trader Joe's":   {"trader joe", "trader joe's"},
    "Whole Foods":    {"whole foods"},
    # Pharmacy
    "Walgreens":      {"walgreens"},
    "CVS":            {"cvs pharmacy", "cvs"},
    "Rite Aid":       {"rite aid"},
    "Duane Reade":    {"duane reade"},
    # Electronics / specialty retail
    "Best Buy":       {"best buy"},
    "Apple Store":    {"apple store"},
    "Microsoft Store": {"microsoft store"},
    "GameStop":       {"gamestop"},
    "Michaels":       {"michaels"},
    "Hobby Lobby":    {"hobby lobby"},
    "JOANN":          {"joann fabrics", "jo-ann"},
    "PetSmart":       {"petsmart"},
    "Petco":          {"petco"},
    "Dick's Sporting Goods": {"dick's sporting", "dicks sporting goods"},
    "Academy Sports": {"academy sports"},
    "Bass Pro Shops": {"bass pro"},
    "Cabela's":       {"cabela's", "cabelas"},
    "REI":            {"rei co-op"},
    # Quick-serve / restaurants
    "Starbucks":      {"starbucks"},
    "Dunkin'":        {"dunkin", "dunkin'", "dunkin donuts"},
    "Tim Hortons":    {"tim hortons"},
    "Dutch Bros":     {"dutch bros"},
    "McDonald's":     {"mcdonald's", "mcdonalds"},
    "Burger King":    {"burger king"},
    "Wendy's":        {"wendy's", "wendys"},
    "Taco Bell":      {"taco bell"},
    "KFC":            {"kfc", "kentucky fried chicken"},
    "Chick-fil-A":    {"chick-fil-a", "chick fil a"},
    "Popeyes":        {"popeyes"},
    "Arby's":         {"arby's", "arbys"},
    "Sonic Drive-In": {"sonic drive-in", "sonic drive in"},
    "Jack in the Box": {"jack in the box"},
    "Carl's Jr.":     {"carl's jr", "carls jr"},
    "Hardee's":       {"hardee's", "hardees"},
    "Whataburger":    {"whataburger"},
    "In-N-Out Burger": {"in-n-out", "in n out"},
    "Five Guys":      {"five guys"},
    "Shake Shack":    {"shake shack"},
    "Culver's":       {"culver's", "culvers"},
    "Raising Cane's": {"raising cane's", "raising canes"},
    "Subway":         {"subway"},
    "Jimmy John's":   {"jimmy john's", "jimmy johns"},
    "Jersey Mike's":  {"jersey mike's", "jersey mikes"},
    "Firehouse Subs": {"firehouse subs"},
    "Chipotle":       {"chipotle"},
    "Qdoba":          {"qdoba"},
    "Moe's Southwest": {"moe's southwest", "moes southwest"},
    "Panera Bread":   {"panera"},
    "Panda Express":  {"panda express"},
    "Wingstop":       {"wingstop"},
    "Zaxby's":        {"zaxby's", "zaxbys"},
    "Bojangles":      {"bojangles"},
    "Del Taco":       {"del taco"},
    "Domino's":       {"domino's", "dominos"},
    "Pizza Hut":      {"pizza hut"},
    "Papa John's":    {"papa john's", "papa johns"},
    "Little Caesars": {"little caesars"},
    "Olive Garden":   {"olive garden"},
    "Applebee's":     {"applebee's", "applebees"},
    "Chili's":        {"chili's", "chilis"},
    "Buffalo Wild Wings": {"buffalo wild wings"},
    "Outback Steakhouse": {"outback steakhouse"},
    "Texas Roadhouse": {"texas roadhouse"},
    "IHOP":           {"ihop"},
    "Denny's":        {"denny's", "dennys"},
    "Waffle House":   {"waffle house"},
    "Cracker Barrel": {"cracker barrel"},
    "Cheesecake Factory": {"cheesecake factory"},
    "Red Lobster":    {"red lobster"},
    # Lodging
    "Marriott":       {"marriott"},
    "Courtyard by Marriott": {"courtyard by marriott", "courtyard marriott"},
    "Fairfield Inn":  {"fairfield inn"},
    "Residence Inn":  {"residence inn"},
    "Hilton":         {"hilton"},
    "Hampton Inn":    {"hampton inn"},
    "DoubleTree":     {"doubletree"},
    "Embassy Suites": {"embassy suites"},
    "Holiday Inn":    {"holiday inn"},
    "Holiday Inn Express": {"holiday inn express"},
    "Hyatt":          {"hyatt"},
    "Best Western":   {"best western"},
    "La Quinta":      {"la quinta"},
    "Comfort Inn":    {"comfort inn"},
    "Quality Inn":    {"quality inn"},
    "Days Inn":       {"days inn"},
    "Super 8":        {"super 8"},
    "Motel 6":        {"motel 6"},
    "Sheraton":       {"sheraton"},
    "Westin":         {"westin"},
    "Wyndham":        {"wyndham"},
    "Ramada":         {"ramada"},
    "Red Roof Inn":   {"red roof inn"},
    "Extended Stay America": {"extended stay america"},
    "Airbnb":         {"airbnb"},
    "Vrbo":           {"vrbo"},
    # Travel: airlines / rail / car rental / rideshare
    "Delta Air Lines": {"delta air lines", "delta airlines"},
    "United Airlines": {"united airlines"},
    "American Airlines": {"american airlines"},
    "Southwest Airlines": {"southwest airlines"},
    "JetBlue":        {"jetblue"},
    "Alaska Airlines": {"alaska airlines"},
    "Spirit Airlines": {"spirit airlines"},
    "Frontier Airlines": {"frontier airlines"},
    "Enterprise Rent-A-Car": {"enterprise rent-a-car", "enterprise rent a car"},
    "Hertz":          {"hertz"},
    "Avis":           {"avis"},
    "Budget Rent a Car": {"budget rent a car"},
    "National Car Rental": {"national car rental"},
    "Alamo Rent a Car": {"alamo rent a car"},
    "Uber":           {"uber"},
    "Lyft":           {"lyft"},
    "Amtrak":         {"amtrak"},
    # Telecom
    "Verizon":        {"verizon"},
    "AT&T":           {"at&t", "at and t"},
    "T-Mobile":       {"t-mobile", "t mobile"},
    "Sprint":         {"sprint store"},
    "Xfinity":        {"xfinity", "comcast"},
    "Spectrum":       {"spectrum"},
    "Cox Communications": {"cox communications"},
    "CenturyLink":    {"centurylink"},
    "Boost Mobile":   {"boost mobile"},
    "Cricket Wireless": {"cricket wireless"},
    "MetroPCS":       {"metropcs", "metro by t-mobile"},
    # Auto parts / service
    "AutoZone":       {"autozone"},
    "O'Reilly Auto Parts": {"o'reilly", "oreilly", "o reilly auto"},
    "NAPA Auto Parts": {"napa auto", "napa"},
    "Advance Auto Parts": {"advance auto parts"},
    "Pep Boys":       {"pep boys"},
    "Jiffy Lube":     {"jiffy lube"},
    "Valvoline":      {"valvoline instant", "valvoline"},
    "Firestone":      {"firestone"},
    "Discount Tire":  {"discount tire"},
    "Les Schwab":     {"les schwab"},
    "Midas":          {"midas"},
    "Meineke":        {"meineke"},
    "Goodyear":       {"goodyear"},
    "Tires Plus":     {"tires plus"},
    "Carquest":       {"carquest"},
    # Shipping / postal
    "FedEx":          {"fedex"},
    "UPS":            {"ups ground", "united parcel"},
    "USPS":           {"usps", "u.s. postal", "post office"},
    "DHL":            {"dhl express"},
    # Online / subscription
    "Amazon":         {"amazon", "amazon.com", "amzn"},
    "Netflix":        {"netflix"},
    "Spotify":        {"spotify"},
    # Cinema
    "AMC Theatres":   {"amc theatres", "amc theatre"},
    "Regal Cinemas":  {"regal cinemas"},
    "Cinemark":       {"cinemark"},
}

# ── Printed slogans / taglines (logo-heavy brands) ──────────────────────────────
# Some brands print their NAME only as a logo (no machine text) but DO print a
# tagline — e.g. The Home Depot's "How doers get more done." These slogans are
# added as long aliases so the brand is still recognised, but they are EXCLUDED
# from the category-scoring sets (see ``_is_slogan``) since they are not the kind
# of literal brand keyword the scorer should weigh. Longest-alias-wins + the word
# boundary + their length (≥ _SLOGAN_MIN_LEN) make a false hit effectively
# impossible.
_SLOGAN_MIN_LEN = 12
_SLOGANS: set[str] = {
    "how doers get more done",
    "more saving more doing",
    "do it right for less",
    "never stop improving",
    "save money live better",
    "expect more pay less",
    "expert service unbeatable price",
    "that was easy",
}
_SLOGAN_BRANDS: dict[str, set[str]] = {
    "The Home Depot": {"how doers get more done", "more saving more doing"},
    "Lowe's":         {"do it right for less", "never stop improving"},
    "Walmart":        {"save money live better"},
    "Target":         {"expect more pay less"},
    "Best Buy":       {"expert service unbeatable price"},
    "Staples":        {"that was easy"},
}
for _canon, _slogans in _SLOGAN_BRANDS.items():
    for _group in (_FUEL_BRANDS, _MATS_BRANDS, _MISC_BRANDS):
        if _canon in _group:
            _group[_canon] |= _slogans
            break


def _tag(brands: dict[str, set[str]], category: str) -> dict[str, tuple[str, set[str]]]:
    """Tag every brand in one group with its category, for the merged map."""
    return {canonical: (category, set(aliases)) for canonical, aliases in brands.items()}


# canonical display name → (category, {alias keywords, lowercased}). Built from the
# three grouped dicts so there is a single source of truth.
KNOWN_VENDORS: dict[str, tuple[str, set[str]]] = {
    **_tag(_FUEL_BRANDS, "fuel"),
    **_tag(_MATS_BRANDS, "mats"),
    **_tag(_MISC_BRANDS, "misc"),
}


def _is_slogan(alias: str) -> bool:
    """True for a printed tagline (excluded from the category-scoring sets)."""
    return len(alias) >= _SLOGAN_MIN_LEN and alias in _SLOGANS


# ── Category-scoring sets (consumed by process_receipts) ────────────────────────
# These DERIVE from the brand aliases above (one source of truth) plus a handful of
# preserved GENERIC, non-brand keywords that help detect a category but are not
# vendor names. ``process_receipts`` imports FUEL_VENDORS / MATS_VENDORS / FUEL_KEYWORDS
# by these exact names — do not rename them.
_FUEL_GENERIC = {
    "gas station", "fuel station", "petro", "petroleum", "gas pump",
    "service station",
}
_MATS_GENERIC = {
    "building supply", "blueprint", "print shop", "reprographics",
    "planning department",
}


def _brand_aliases(brands: dict[str, set[str]]) -> set[str]:
    """All real (non-slogan) brand aliases in one group, for the scoring sets."""
    return {a for aliases in brands.values() for a in aliases if not _is_slogan(a)}


FUEL_VENDORS = _brand_aliases(_FUEL_BRANDS) | _FUEL_GENERIC
MATS_VENDORS = _brand_aliases(_MATS_BRANDS) | _MATS_GENERIC

FUEL_KEYWORDS = {
    "gas", "gasoline", "diesel", "petrol", "fuel", "pump", "gallon",
    "gallons", "unleaded", "e85", "fill-up",
    "fill up", "fueling", "service station", "gas pump", "octane",
    "auto fuel", "motor fuel", "regular unleaded", "premium unleaded",
    "price/gal", "per gallon",
}


# ── Matching machinery ──────────────────────────────────────────────────────────

def _boundary_pattern(alias: str) -> "re.Pattern[str]":
    """Word-boundary matcher for one alias against lowercased text.

    Mirrors process_receipts._kw_pattern: a purely numeric alias additionally
    must not touch digits, '.', ',', '#' or '$' so prices/store numbers/zips
    don't read as a brand (protects numeric brands like "76").
    """
    esc = re.escape(alias)
    if alias.isdigit():
        return re.compile(rf"(?<![a-z0-9.,#$]){esc}(?![a-z0-9.,])")
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")


# OCR letter confusions to fold, multi-char first then single-char. Letters ONLY —
# digits are never folded here so numeric brands ("76") stay intact.
_OCR_MULTI_FOLDS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
_OCR_SINGLE_FOLDS = (("u", "v"),)


def _normalize_ocr_strict(s: str) -> str:
    """Lowercase + fold a tiny set of letter OCR confusions, strip punctuation.

    Used for the conservative glyph-normalized matching pass so a stylised font
    that turns ``7-ELEVEN`` into ``7-ELEUEN`` (V read as U) still resolves. Applied
    identically to the aliases and the text, so matching stays consistent. Digits
    are deliberately left untouched.
    """
    if not s:
        return ""
    s = s.lower()
    for a, b in _OCR_MULTI_FOLDS:
        s = s.replace(a, b)
    for a, b in _OCR_SINGLE_FOLDS:
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Patterns for the EXACT pass (raw lowercased text) and the GLYPH pass (normalized
# text). Both keep the ORIGINAL alias so ranking is by the real alias length.
_VENDOR_ALIAS_PATTERNS: list[tuple[str, str, str, "re.Pattern[str]"]] = [
    (canonical, category, alias, _boundary_pattern(alias))
    for canonical, (category, aliases) in KNOWN_VENDORS.items()
    for alias in aliases
]

_VENDOR_ALIAS_PATTERNS_NORM: list[tuple[str, str, str, "re.Pattern[str]"]] = [
    (canonical, category, alias, _boundary_pattern(_normalize_ocr_strict(alias)))
    for canonical, (category, aliases) in KNOWN_VENDORS.items()
    for alias in aliases
    if _normalize_ocr_strict(alias)
]


def _search_patterns(text: str, patterns) -> "tuple | None":
    """Best brand hit in ``text``: longest alias wins, then earliest position.

    Returns ``(alias_len, -position, canonical, category, alias)`` or None.
    """
    best = None
    for canonical, category, alias, rx in patterns:
        m = rx.search(text)
        if m is None:
            continue
        key = (len(alias), -m.start(), canonical, category, alias)
        if best is None or key > best:
            best = key
    return best


# ── Bounded fuzzy backstop (opt-in) ─────────────────────────────────────────────
_FUZZY_MIN_LEN = 5       # ignore aliases / candidates shorter than this
_FUZZY_RATIO = 0.88      # difflib ratio threshold for a fuzzy hit
# Digit↔letter confusions only folded in the FULL fold used for fuzzy comparison
# (not in _normalize_ocr_strict, which protects numeric brands).
_DIGIT_FOLDS = {"0": "o", "1": "l", "5": "s", "8": "b"}


def _fold_full(s: str) -> str:
    """Aggressive fold (letters + digits, no spaces) for fuzzy comparison only."""
    s = _normalize_ocr_strict(s)
    s = "".join(_DIGIT_FOLDS.get(c, c) for c in s)
    return s.replace(" ", "")


_FUZZY_ALIASES: list[tuple[str, str, str]] = []
for _canonical, (_category, _aliases) in KNOWN_VENDORS.items():
    for _alias in _aliases:
        if _is_slogan(_alias) or len(_alias) < _FUZZY_MIN_LEN:
            continue
        _folded = _fold_full(_alias)
        if _folded:
            _FUZZY_ALIASES.append((_folded, _canonical, _category))


def _fuzzy_match_vendor(candidate: str) -> "tuple[str, str, float, str] | None":
    """Tight fuzzy match for a SHORT vendor-name candidate (never a whole receipt).

    Returns ``(canonical, category, ratio, folded_alias)`` for the best alias whose
    difflib ratio clears ``_FUZZY_RATIO``, or None. Off the hot path — only called
    when exact + glyph passes both miss and the caller opts in.
    """
    if not candidate:
        return None
    cand = candidate.strip()
    # Guard: only ever a short vendor-name candidate, not the whole receipt.
    if len(cand) > 40 or len(cand.split()) > 6:
        return None
    folded = _fold_full(cand)
    if len(folded) < _FUZZY_MIN_LEN:
        return None
    sm = difflib.SequenceMatcher()
    sm.set_seq2(folded)
    best: tuple[float, int, str, str, str] | None = None
    for fa, canonical, category in _FUZZY_ALIASES:
        if abs(len(fa) - len(folded)) > 3:          # cheap length gate
            continue
        sm.set_seq1(fa)
        if sm.real_quick_ratio() < _FUZZY_RATIO or sm.quick_ratio() < _FUZZY_RATIO:
            continue
        r = sm.ratio()
        if r < _FUZZY_RATIO:
            continue
        key = (r, len(fa), canonical, category, fa)
        if best is None or key > best:
            best = key
    if best is None:
        return None
    return best[2], best[3], best[0], best[4]


def match_vendor_detailed(text: str, fuzzy: bool = False) -> "tuple[str, str, str] | None":
    """Like :func:`match_vendor` but also returns the matched alias.

    Two-pass + optional fuzzy:
      1. exact on the raw lowercased text (unchanged historical behaviour),
      2. glyph-normalized (only when the exact pass finds nothing),
      3. bounded fuzzy on a short candidate (only when ``fuzzy=True``).

    Returns ``(canonical_name, category, matched_alias)`` or None.
    """
    if not text:
        return None
    low = text.lower()
    hit = _search_patterns(low, _VENDOR_ALIAS_PATTERNS)
    if hit is not None:
        return hit[2], hit[3], hit[4]
    norm = _normalize_ocr_strict(text)
    if norm:
        hit = _search_patterns(norm, _VENDOR_ALIAS_PATTERNS_NORM)
        if hit is not None:
            return hit[2], hit[3], hit[4]
    if fuzzy:
        fz = _fuzzy_match_vendor(text)
        if fz is not None:
            return fz[0], fz[1], fz[3]
    return None


def match_vendor(text: str, fuzzy: bool = False) -> "tuple[str, str] | None":
    """Cross-reference OCR text against the known-vendor database.

    Returns ``(canonical_name, category)`` for the best brand hit, or None when no
    known vendor is present. The most specific (longest) alias wins, so a
    multi-word brand is preferred over a generic single word it contains. The
    public contract is unchanged; ``fuzzy`` (default False) enables the bounded
    fuzzy backstop for a short vendor-name candidate.
    """
    hit = match_vendor_detailed(text, fuzzy=fuzzy)
    if hit is None:
        return None
    return hit[0], hit[1]
