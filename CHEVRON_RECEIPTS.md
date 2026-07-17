# Chevron/Texaco Rewards → Receipt App

Chevron and Texaco don't offer a consumer receipt API (see
`GAS_RECEIPT_IMPORT.md`), but the Rewards site's **Wallet** page shows every
purchase receipt behind an infinite-scroll history. The bundled userscript —
**`chevron_receipt_downloader.user.js`** — exports them as **one combined PDF,
one receipt per page**, which this app then imports with perfect fidelity.

## How the export works

Runs entirely in your own browser on the already-signed-in Wallet page
(`chevrontexacorewards.com … loyalty-wallet-page`). It:

1. Auto-scrolls the history until your chosen start date has loaded.
2. Collects every receipt modal's raw text, keeping **Purchase** rows only
   (the "Discount activated" rows repeat the same receipt and are skipped).
3. Filters to your date range, sorts chronologically, and builds one PDF with
   jsPDF — each receipt on its own page, labeled with its date.
4. Names the file by the **actual** first/last receipt dates it contains, e.g.
   `chevron-receipts_2025-05-09_to_2026-06-24.pdf`.

Nothing is transmitted anywhere; it never touches credentials or settings —
it only reads receipt text your account already displays and saves a local PDF.

## Using it

**Userscript (recommended):** install `chevron_receipt_downloader.user.js` in
Tampermonkey/Violentmonkey, sign in to the Rewards site, open the Wallet page —
a floating **Download receipts** button appears and prompts for the date range.

**Console:** open the Wallet page → DevTools console → paste the script → run
`chevronDownloadReceipts('2025-05-09')` (start → today) or
`chevronDownloadReceipts('2025-05-09', '2026-07-16')`.

## Importing into the app

Drop the PDF onto the **Add Receipts** card (or into the intake folder). The
app expands each page into its own receipt — and because these pages are
digitally generated text, the pipeline's **PDF text-layer fast path** kicks in:
the page's exact text is read straight from the PDF (a `pdf_text` step in the
card's step log, engine `pdf-text`) instead of OCR-ing a render of it. That
means zero transcription errors and no OCR/LLM-OCR cost per page; the rendered
page image is still kept as the receipt image embedded in the report.

Mind the per-PDF page cap (`PDF_MAX_PAGES`, default 50, adjustable in
Settings → AI Model → Advanced tuning): split very long exports into two date
ranges if needed.

## Notes

- The fast path only triggers for pages with a real text layer and **no raster
  images** — photographed/scanned PDFs keep the normal image + OCR path.
- Fuel receipts classify as **fuel** through the existing vendor database
  (Chevron/Texaco are known brands), so they land in the right report section.
