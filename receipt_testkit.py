#!/usr/bin/env python3
"""Synthetic receipt test-bench — generate challenge receipts and score extractions.

Purpose
-------
A repeatable way to *gauge the app* and *score different LLMs*: a fixed suite of
receipt images, each crafted around one distinct challenge (rotation, faint
thermal print, multiple totals, an ambiguous US date, a missing vendor, a long
itemised list, low contrast, a big multi-currency total …), each with known
ground truth. Run the suite through the pipeline and you get a per-field,
per-receipt scorecard you can compare across models.

Two ways to use it
------------------
* As a library: ``build_test_receipts(dir)`` renders the PNGs + returns truth;
  ``score_extraction(truth, got)`` scores one result; ``run_benchmark(...)``
  drives a whole suite through an extractor and aggregates.
* As a CLI:
    ``python receipt_testkit.py --out test_receipts``         # just render them
    ``python receipt_testkit.py --out test_receipts --run``    # render + score via the real pipeline

The renderer and scorer have zero LLM/OCR dependencies (pure PIL), so they're
unit-tested deterministically; ``--run`` is what actually exercises a model.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ── Fonts ───────────────────────────────────────────────────────────────────
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "DejaVuSansMono.ttf",
    "DejaVuSans.ttf",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ── Challenge specs ──────────────────────────────────────────────────────────
@dataclass
class Challenge:
    id: str
    description: str
    truth: dict                     # {vendor, date(YYYY-MM-DD), amount, category}
    lines: list                     # printed receipt body lines
    rotate: int = 0                 # degrees CCW applied after render (tests autorotate)
    contrast: float = 1.0           # <1 fades the print (tests faint thermal)
    noise: int = 0                  # 0..255 speckle amplitude (tests dirty scans)
    blur: float = 0.0               # gaussian blur radius (tests soft focus)
    bg: int = 255
    fg: int = 20


def _body(vendor, items, total, date_str, *, total_label="TOTAL", header_addr=True):
    lines = [vendor]
    if header_addr:
        lines += ["123 Main Street", "Springfield, IL", "-" * 28]
    for name, price in items:
        lines.append(f"{name:<18}{price:>8.2f}")
    lines += ["-" * 28, f"{total_label:<18}{total:>8.2f}", "", f"Date: {date_str}"]
    return lines


def challenge_suite() -> list[Challenge]:
    """The fixed set of challenge receipts. Order/content is stable for comparison."""
    return [
        Challenge(
            id="clean",
            description="Baseline — crisp, well-lit, single total.",
            truth={"vendor": "Shell", "date": "2026-05-01", "amount": 52.40, "category": "fuel"},
            lines=_body("Shell", [("Unleaded 14.2g", 52.40)], 52.40, "05/01/2026"),
        ),
        Challenge(
            id="rotated_90",
            description="Rotated 90° — exercises auto-rotate before OCR.",
            truth={"vendor": "Home Depot", "date": "2026-05-03", "amount": 128.74, "category": "mats"},
            lines=_body("Home Depot", [("2x4 Lumber", 48.00), ("Screws box", 12.74),
                                        ("Paint 1gal", 68.00)], 128.74, "05/03/2026"),
            rotate=90,
        ),
        Challenge(
            id="faint_thermal",
            description="Faded thermal print — low contrast text.",
            truth={"vendor": "Chevron", "date": "2026-04-28", "amount": 41.10, "category": "fuel"},
            lines=_body("Chevron", [("Fuel", 41.10)], 41.10, "04/28/2026"),
            contrast=0.45,
        ),
        Challenge(
            id="multi_total",
            description="Subtotal + tax + grand total — must pick the grand total.",
            truth={"vendor": "Olive Garden", "date": "2026-05-10", "amount": 86.31, "category": "misc"},
            lines=(["Olive Garden", "Italian Kitchen", "-" * 28,
                    f"{'Entrees':<18}{72.00:>8.2f}", f"{'Drinks':<18}{7.00:>8.2f}",
                    "-" * 28, f"{'SUBTOTAL':<18}{79.00:>8.2f}",
                    f"{'TAX':<18}{7.31:>8.2f}", f"{'GRAND TOTAL':<18}{86.31:>8.2f}",
                    "", "Date: 05/10/2026"]),
        ),
        Challenge(
            id="us_date_ambiguous",
            description="Date 03/04/2026 — must read US month/day → March 4, not April 3.",
            truth={"vendor": "Mobil", "date": "2026-03-04", "amount": 38.90, "category": "fuel"},
            lines=_body("Mobil", [("Gasoline", 38.90)], 38.90, "03/04/2026"),
        ),
        Challenge(
            id="noisy_scan",
            description="Speckled, blurred scan — dirty/soft image.",
            truth={"vendor": "Lowe's", "date": "2026-05-06", "amount": 73.55, "category": "mats"},
            lines=_body("Lowe's", [("PVC pipe", 23.55), ("Fittings", 50.00)], 73.55, "05/06/2026"),
            noise=40, blur=0.6,
        ),
        Challenge(
            id="long_itemized",
            description="Many line items — must still find the printed total.",
            truth={"vendor": "Costco", "date": "2026-05-12", "amount": 214.83, "category": "misc"},
            lines=_body("Costco Wholesale",
                        [("Water 40pk", 4.99), ("Coffee 2lb", 17.99), ("Paper towels", 21.99),
                         ("Batteries", 15.49), ("Snacks", 28.50), ("Cleaning sup", 33.87),
                         ("Office chair", 91.99)], 214.83, "05/12/2026"),
        ),
        Challenge(
            id="missing_vendor",
            description="No legible vendor — must NOT fabricate one (blank is correct).",
            truth={"vendor": "", "date": "2026-05-08", "amount": 19.25, "category": "misc"},
            lines=(["", "", "-" * 28, f"{'Item':<18}{19.25:>8.2f}",
                    "-" * 28, f"{'TOTAL':<18}{19.25:>8.2f}", "", "Date: 05/08/2026"]),
        ),
        Challenge(
            id="big_amount",
            description="Large multi-thousand total with comma grouping.",
            truth={"vendor": "Ferguson", "date": "2026-05-15", "amount": 4218.00, "category": "mats"},
            lines=_body("Ferguson Supply", [("HVAC unit", 3998.00), ("Delivery", 220.00)],
                        4218.00, "05/15/2026"),
        ),
    ]


# ── Rendering ────────────────────────────────────────────────────────────────
def render_challenge(ch: Challenge, width: int = 460) -> Image.Image:
    pad, line_h = 24, 30
    title_font = _load_font(26)
    body_font = _load_font(22)

    height = pad * 2 + line_h * (len(ch.lines) + 1)
    img = Image.new("L", (width, height), ch.bg)
    d = ImageDraw.Draw(img)
    y = pad
    for i, ln in enumerate(ch.lines):
        font = title_font if i == 0 else body_font
        d.text((pad, y), str(ln), fill=ch.fg, font=font)
        y += line_h + (6 if i == 0 else 0)

    if ch.contrast != 1.0:
        # Fade the print toward the background (simulate worn thermal paper).
        img = Image.eval(img, lambda v: int(ch.bg - (ch.bg - v) * ch.contrast))
    if ch.noise:
        import random
        import zlib
        # Stable per-receipt seed: ``hash()`` of a str is salted per process
        # (PYTHONHASHSEED), so it rendered different speckle every run, defeating
        # the point of a *fixed* benchmark suite. crc32 is deterministic.
        rnd = random.Random(zlib.crc32(ch.id.encode()) & 0xFFFF)
        px = img.load()
        for _ in range((img.width * img.height) // 40):
            x = rnd.randrange(img.width); yy = rnd.randrange(img.height)
            delta = rnd.randint(-ch.noise, ch.noise)
            px[x, yy] = max(0, min(255, px[x, yy] + delta))
    if ch.blur:
        img = img.filter(ImageFilter.GaussianBlur(ch.blur))
    if ch.rotate:
        img = img.rotate(ch.rotate, expand=True, fillcolor=ch.bg)
    return img.convert("RGB")


def build_test_receipts(out_dir: Path,
                        suite: Optional[list[Challenge]] = None) -> list[dict]:
    """Render the whole suite to ``out_dir`` and return [{id, path, truth, description}]."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suite = suite or challenge_suite()
    manifest = []
    for ch in suite:
        path = out_dir / f"{ch.id}.png"
        render_challenge(ch).save(path, "PNG")
        manifest.append({"id": ch.id, "path": str(path),
                         "truth": dict(ch.truth), "description": ch.description})
    return manifest


# ── Scoring ──────────────────────────────────────────────────────────────────
def _norm_vendor(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _vendor_match(truth: str, got: str) -> bool:
    t, g = _norm_vendor(truth), _norm_vendor(got)
    if t == "":                      # blank truth: correct only if the model left it blank
        return g == ""
    if g == "":
        return False
    return t in g or g in t          # generous containment (OCR drops suffixes like "Inc")


def _amount_match(truth, got, tol: float = 0.01) -> bool:
    try:
        return abs(float(truth) - float(got)) <= tol
    except (TypeError, ValueError):
        return False


def score_extraction(truth: dict, got: dict) -> dict:
    """Score one extraction against ground truth. Returns per-field bools + a 0..1 score.

    Fields weighted: amount and vendor are the hard, high-value ones; date and
    category round it out. The blank-vendor challenge rewards *not* inventing one.
    """
    got = got or {}
    fields = {
        "vendor":   _vendor_match(truth.get("vendor", ""), got.get("vendor", "")),
        "amount":   _amount_match(truth.get("amount"), got.get("amount")),
        "date":     (truth.get("date", "") or "") == (got.get("date", "") or ""),
        "category": (truth.get("category", "") or "").lower()
                    == (got.get("_category") or got.get("category") or "").lower(),
    }
    weights = {"vendor": 0.30, "amount": 0.40, "date": 0.20, "category": 0.10}
    score = sum(weights[k] for k, ok in fields.items() if ok)
    return {"fields": fields, "score": round(score, 4)}


def run_benchmark(manifest: list[dict],
                  extract_fn: Callable[[str], dict]) -> dict:
    """Run each receipt through ``extract_fn(path)`` and aggregate scores."""
    rows = []
    for item in manifest:
        got = extract_fn(item["path"]) or {}
        sc = score_extraction(item["truth"], got)
        rows.append({"id": item["id"], "description": item["description"],
                     "truth": item["truth"], "got": {
                         "vendor": got.get("vendor"), "amount": got.get("amount"),
                         "date": got.get("date"),
                         "category": got.get("_category") or got.get("category")},
                     **sc})
    overall = round(sum(r["score"] for r in rows) / len(rows), 4) if rows else 0.0
    by_field = {f: round(sum(1 for r in rows if r["fields"][f]) / len(rows), 4)
                for f in ("vendor", "amount", "date", "category")} if rows else {}
    return {"overall": overall, "by_field": by_field, "rows": rows, "count": len(rows)}


def format_scorecard(result: dict, model: str = "") -> str:
    lines = [f"Receipt extraction benchmark{f'  —  model: {model}' if model else ''}",
             "=" * 64,
             f"{'receipt':<20}{'V':>3}{'A':>3}{'D':>3}{'C':>3}{'score':>8}"]
    for r in result["rows"]:
        f = r["fields"]
        mark = lambda b: " ✓" if b else " ·"
        lines.append(f"{r['id']:<20}{mark(f['vendor']):>3}{mark(f['amount']):>3}"
                     f"{mark(f['date']):>3}{mark(f['category']):>3}{r['score']:>8.2f}")
    lines += ["-" * 64,
              "by field: " + "  ".join(f"{k}={v:.0%}" for k, v in result["by_field"].items()),
              f"OVERALL: {result['overall']:.1%}  ({result['count']} receipts)"]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────
def _pipeline_extractor():
    """Build an extractor that runs the real pipeline (LM Studio + OCR)."""
    import process_receipts as pr
    pr.initialize_models()
    client = pr._make_client()

    def _extract(path: str) -> dict:
        return pr._extract_receipt_with_status(client, Path(path), None) or {}

    return _extract, pr._active_distill_model


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate/score synthetic test receipts.")
    ap.add_argument("--out", default="test_receipts", help="output directory for the PNGs")
    ap.add_argument("--run", action="store_true",
                    help="also run them through the real pipeline and print a scorecard")
    args = ap.parse_args(argv)

    manifest = build_test_receipts(Path(args.out))
    print(f"Rendered {len(manifest)} challenge receipts to {args.out}/")
    for m in manifest:
        print(f"  • {m['id']:<20} {m['description']}")

    if args.run:
        print("\nRunning them through the pipeline … (needs LM Studio + a model)\n")
        extract_fn, model = _pipeline_extractor()
        result = run_benchmark(manifest, extract_fn)
        print(format_scorecard(result, model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
