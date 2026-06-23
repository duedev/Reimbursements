# Importing Receipts from Gas-Provider Websites — Research Write-up

> **Status:** Research only. No code in this document is implemented.
> **Scope:** Can the Reimbursements app pull *itemized fuel receipts* (vendor,
> date, amount, gallons, price/gal, fuel grade, station address) directly from
> gas-brand websites/apps — by API, data export, or otherwise — and feed them
> into the existing local OCR + vision-LLM pipeline?
> **Audience:** A developer deciding what to build.
> **Last updated:** 2026-06-23. Vendor portals (`developer.shell.com`,
> `developer.wexinc.com`, `chevrontexacobusinesscard.com`, `docs.knotapi.com`)
> serve **HTTP 403 to automated fetchers**, so several claims below rest on
> search-engine snapshots of those pages rather than a direct page fetch — these
> are flagged *(snapshot)* and should be re-verified against the live portal
> before any build. Anything I could not corroborate is labelled **UNVERIFIED**.

---

## 1. Executive summary — the honest bottom line

**There is no public, per-consumer "get my receipts" API from any major US gas
brand — Chevron/Texaco included.** Consumer digital receipts for mobile-pay
fuelings exist *only inside the brand's own mobile app*, where the only
programmatic exit is the user choosing to receive an **email receipt** (or
exporting/screenshotting one). There is no documented OAuth-style endpoint a
third-party app can call to enumerate a consumer's fuel transactions.

Where rich, *itemized* fuel data **is** available by API, it is **B2B and
account-gated**: it belongs to the **fleet/business fuel-card** programs (Shell
Fleet, the WEX-administered Chevron & Texaco Business Card, etc.). These return
exactly the Level III detail we want — gallons, price/gal, fuel grade, station,
driver/vehicle — but require a *signed commercial relationship and a business
card account*. They are not available to an individual reimbursing personal
fuel purchases.

The realistic paths for **this** app, in priority order:

1. **(A) Inbound email / IMAP ingestion → existing pipeline [RECOMMENDED].**
   Every brand can email a receipt. Pull those emails (IMAP poll or a dedicated
   forwarding address), extract the receipt body/attachment, and run it through
   the OCR+LLM pipeline we already have. Universal, brand-agnostic, fits the
   local-first privacy model.
2. **(B) Fleet/business transaction-data API connector** (Shell Fleet API /
   WEX) — *only* for users who hold a business fuel card. Highest-fidelity data,
   but real onboarding friction and not relevant to most consumers. Build behind
   an opt-in setting.
3. **(C) Third-party email-receipt parsing services** (Veryfi, Taggun, etc.) —
   technically easy but **sends receipts to a cloud OCR vendor**, which
   contradicts the app's local-first stance. We already *have* OCR+LLM, so this
   is redundant; only the *email-collector* idea is worth borrowing, not the
   outsourced OCR.

**Web scraping the brand sites is not recommended** (login/MFA walls, anti-bot,
ToS prohibitions, brittle) and is covered in §6.

A financial-aggregator angle (Plaid / Knot / Stripe) can tell you *that* a fuel
purchase happened and enrich the merchant — but **none of them return the
itemized fuel receipt** (gallons, price/gal, grade). See §7.

---

## 2. Per-brand findings

| Brand | Consumer receipt API? | Fleet / business data API? | Auth / access | Notes + sources |
|---|---|---|---|---|
| **Chevron / Texaco** | **No.** Digital receipt lives in the Chevron/Texaco mobile app for mobile-pay fuelings; user can opt into **email receipts** + printed pump receipts. No third-party API. | **Yes — via WEX.** The Chevron & Texaco **Business Card** program is *administered by WEX*; Level III transaction detail (gallons, price/gal, fuel grade, location, driver) is available through the WEX platform/portal, with API options. | Consumer: app only (manual email/export). Business: WEX account + credentials; verified commercial relationship. | App receipts: [mobile-app FAQs](https://dev.chevronwithtechron.com/content/cwt/en_us/home/mobile-app-faqs.html), [rewards mobile-apps](https://www.chevrontexacorewards.com/en_us/home/mobile-apps.html). Business card admin by WEX *(snapshot)*: [Ramp explainer](https://ramp.com/blog/fleet-fuel-cards/what-is-chevron-texaco), [Chevron Texaco Business Card — fleet data](https://www.chevrontexacobusinesscard.com/fuel-card-data-for-fleet-management/). |
| **Shell** | **No** consumer receipt API. Shell app issues receipts to the user. | **Yes — documented.** Shell **Developer Portal** publishes a REST **Fleet Transaction Data API** and **SmartPay** / Mobility Card Transaction APIs returning full transaction detail (products, location, driver/vehicle, discounts, tax, invoicing). | OAuth 2.0 *client-credentials* (most APIs); some use Basic + ApiKey. Onboarding requires a **verified Shell mobility/fleet customer relationship**, signed pricing/contract, and account setup by Shell's API team. | *(snapshot)* [API catalog](https://developer.shell.com/api-catalog), [Transaction Data straight into your mobility system](https://developer.shell.com/use-cases/transaction-data-straight-your-mobility-system), [B2B Mobility Card Transaction Data](https://developer.shell.com/api-catalog/v2.1.0/b2b-mobility-card-transaction-data), [Authentication](https://developer.shell.com/docs/authentication), [Fleet Transaction Data API detail](https://apiportal.shell.com/apidetail/fleettransaction-0). |
| **ExxonMobil (Exxon/Mobil)** | **No** consumer receipt API. Rewards+ app shows loyalty/transaction *history* in-app; opt-in email receipts. | **Partial / B2B.** ExxonMobil runs a general **API Hub** and "business system integration" APIs, but **no public fleet-receipt API** is documented for consumers. Fleet fuel cards exist (often WEX/FLEETCOR-administered) with portal/Level III data. | Consumer: app only. Business: B2B onboarding; **UNVERIFIED** whether a self-serve fleet-receipt API exists. | [Rewards+ app FAQs](https://www.exxonmobilfuels.com/en/rewards/faqs/app-faqs), [ExxonMobil API Hub](https://apihub.exxonmobil.com/hub), [business system integration](https://www.exxonmobilchemical.com/en/resources/ebusiness-services/business-system-integration). |
| **BP / Amoco** | **No** consumer receipt API. Migrated BPme/ampm → **earnify** app; "save fuel receipts automatically by going paperless" is an in-app feature, not an external API. | Fleet cards exist (WEX/FLEETCOR-administered) — no public BP fleet-receipt API found. | Consumer: app only. | [earnify (bp America)](https://www.bp.com/en_us/united-states/home/products-and-services/earnify.html), [BP launches earnify](https://www.cstoredive.com/news/bp-launches-earnify-app-rewards-program/729124/). |
| **Marathon** | **No** consumer receipt API found. MarathonARKID/loyalty app + email receipts. | Marathon fleet cards exist (third-party-administered). No public fleet-receipt API found. | Consumer: app only. | **UNVERIFIED** — no authoritative developer page located. General fuel-loyalty context: [The Points Guy — fuel rewards 2026](https://thepointsguy.com/loyalty-programs/fuel-loyalty-programs/). |
| **Circle K** | **No** consumer receipt API. "Inner Circle" rewards in-app. | No public fleet-receipt API found. | Consumer: app only. | [The Points Guy — fuel rewards 2026](https://thepointsguy.com/loyalty-programs/fuel-loyalty-programs/). |
| **7-Eleven (incl. Speedway)** | **No** consumer receipt API. 7Rewards/Speedy Rewards app; email receipts. | No public fleet-receipt API found. | Consumer: app only. | **UNVERIFIED** — no developer portal located. |
| **Costco** | **No** receipt API. Fuel is members-only; receipts via pump/email, and warehouse purchases appear in order history in-app/online. | No public API. | Consumer: app/account only. | General: [The Points Guy — fuel rewards 2026](https://thepointsguy.com/loyalty-programs/fuel-loyalty-programs/). |
| **Sam's Club** | **No** receipt API. Fuel pay is **app-only "Scan & Go"** (scan pump QR, pay in app); receipt is in the app/order history. | No public consumer API. (Walmart/Sam's *is* covered by Knot TransactionLink — see §7 — but that is SKU data, not a fuel receipt.) | Consumer: app only. | [The Points Guy — fuel rewards 2026](https://thepointsguy.com/loyalty-programs/fuel-loyalty-programs/); Knot merchant coverage: [KnotAPI](https://www.knotapi.com/). |
| **GasBuddy / Pay with GasBuddy** | **No** public *consumer transaction/receipt* API. GasBuddy's public-facing data is gas *prices/stations* (and only via unofficial scrapers/data resellers). "Pay with GasBuddy" payment-card transactions are not exposed via a documented third-party receipt API. | n/a | None public; price data only via third-party scrapers. | [GasBuddy on Datarade](https://datarade.ai/data-providers/gasbuddy/profile), [gas-buddy GitHub](https://github.com/gas-buddy/). |
| **Upside** | **No** public consumer receipt/transaction API. Upside is a cash-back app; users *submit* receipts to Upside, it does not expose them outward via an API. | n/a | None public. | **UNVERIFIED** — no developer/API documentation located. |

**Cross-cutting takeaway:** the *consumer* column is uniformly "No." Every brand
keeps the receipt inside its app and the only sanctioned export is **email**.
The *only* documented, structured fuel-data APIs are the **fleet/business**
programs (Shell Fleet, WEX-administered Chevron/Texaco Business Card), which are
B2B and contract-gated.

---

## 3. Strategy A — Inbound email / IMAP ingestion *(RECOMMENDED)*

**Idea.** Don't fight the brands' walls. Let them do what they already do — email
a receipt — and ingest those emails into the pipeline we already own.

Every brand app and most pump POS systems can send an **email receipt**
(Chevron/Texaco explicitly offer "email receipts in your settings"; BP's earnify
saves receipts "paperless"; Exxon Rewards+ supports email receipts). That makes
email the one **universal, brand-agnostic, sanctioned** export channel.

### Two ingestion patterns

| Pattern | How it works | Pros | Cons |
|---|---|---|---|
| **IMAP polling** (recommended for local-first) | App holds IMAP credentials (or an app-password) for a mailbox/folder the user forwards fuel receipts to; polls every N minutes for unseen messages, pulls body + attachments, hands them to the pipeline. | Fully **local** — no third-party relay ever touches the receipt; works with any mailbox (Gmail, iCloud, self-hosted); no public endpoint to expose. | App stores mail credentials (mitigate with app-passwords / OAuth device flow + the existing `app_secrets.py` store); polling latency; must avoid re-processing (track UID/Message-ID). |
| **Dedicated forwarding address + inbound webhook** | A provider (or self-hosted relay) receives mail at e.g. `receipts@…` and POSTs parsed mail to the app. | Near-real-time; no stored mailbox creds; clean separation. | Requires a public, reachable endpoint (at odds with a desktop/local app); a relay provider sees the receipt unless self-hosted (e.g. `EmailEngine`, `imap-to-webhook`, Forward Email self-hosted). Better suited to the app's *hosted* deploy than the local one. |

For a **local-first desktop app, IMAP polling wins**: nothing leaves the
machine, no inbound port, and it slots directly in front of the current
`watch_mode.py` / queue worker.

### Identifying a gas receipt in a mailbox

Use layered, cheap rules *before* spending an OCR/LLM pass:

- **Sender allow-list / domain match** — `@chevron.com`, `@texaco.com`,
  `@shell.com`, `@exxonmobil*.com`, `@bp.com` / `@earnify*`, `@circlek.com`,
  `@7-eleven.com`, `@costco.com`, `@samsclub.com`, plus payment-platform senders
  (P97, Stuzo) and common receipt relays. Keep this list user-editable.
- **Subject heuristics** — `receipt`, `fuel`, `your purchase`, `pump`, brand
  name, station number.
- **Body/attachment signals** — presence of `$/gal`, `gallons`, `unleaded`,
  `regular/plus/premium/diesel`, a station address. The existing offline regex
  parser (`_local_distill_from_ocr`, money/date/vendor matchers) is already good
  at this and can act as the classifier *and* the extractor.
- **Attachment handling** — if the email carries a PDF/image receipt, feed the
  attachment straight into the pipeline (it already handles PDF + image). If the
  receipt is **HTML-in-body** (common), render/normalize the HTML to text or to
  an image and run the same path.

### Why this fits the app

- It reuses the entire existing pipeline (`process_receipts._extract_receipt_with_status`)
  and `watch_mode` daemon — email becomes just another **intake source**
  alongside the watched folder.
- It keeps the **privacy model intact**: with the local OCR/LLM path, the
  receipt never leaves the machine. (If the user has opted into the OpenRouter
  cloud LLM, the same `LLM_ALLOW_IMAGE` gate already governs whether the image
  is sent — no new privacy surface.)
- It is **brand-agnostic**: one connector covers all gas brands *and* incidental
  non-fuel receipts, because the classifier is content-based, not API-specific.

**Verdict:** lowest cost, highest coverage, best privacy fit. Build this first.

---

## 4. Strategy B — Fleet / business transaction-data API connector

For the subset of users who hold a **business fuel card**, real itemized data is
available by API — this is the *only* path that yields structured Level III
fields without OCR at all.

### Shell Fleet / SmartPay (documented)

- Shell's **Developer Portal** exposes a REST **Fleet Transaction Data API** and
  **SmartPay** / Mobility Card transaction APIs. The transaction payload
  reportedly includes *full detail*: products purchased, location, driver/vehicle
  details, discount info, tax info, and invoicing status *(snapshot)*.
- **Auth:** OAuth 2.0 client-credentials grant for most APIs; some endpoints use
  **Basic + ApiKey** *(snapshot — re-verify per-API)*.
- **Access:** *not self-serve.* Onboarding requires a **verified Shell mobility
  customer relationship**, the Shell API support team to create/customize a
  Mobility Customer account, and an agreed/signed pricing & contract before
  production access.
- **Sandbox:** the portal references testing before go-live; **UNVERIFIED**
  whether an open sandbox with synthetic data is available pre-contract — assume
  not without a signed relationship.

### WEX-administered cards (Chevron & Texaco Business Card, and others)

- The **Chevron & Texaco Business Card program is administered by WEX**. WEX
  captures **Level III** itemized fuel data — *driver ID, fuel grade, cost per
  gallon, gallons purchased, station location, timestamp* — exactly the fields a
  fuel reimbursement needs *(snapshot)*.
- **WEX Developer Portal** advertises "no-cost" payment/data APIs that push fleet
  data (JSON/XML) into ERP/CRM/back-office systems, with claims of **real-time
  Level III sync** (no manual CSV/batch) *(snapshot)*.
- **Auth / access:** credentials are tied to a **WEX fleet/business account and
  partner onboarding**; this is a B2B relationship, not an individual sign-up.
  Exact auth mechanics are **UNVERIFIED** from the public portal (403 to
  automated fetch) — confirm directly with WEX before scoping.

### Tradeoffs

- **Pro:** gold-standard fidelity (no OCR error; native gallons/grade/price-per-gal),
  near-real-time, structured.
- **Con:** applies only to **business-card holders**; meaningful onboarding
  friction (contracts, account provisioning); per-brand connectors (Shell ≠ WEX
  ≠ FLEETCOR), so maintenance cost scales with coverage; almost certainly
  **irrelevant to most individual reimbursers**.
- **Fit:** appropriate as an **optional, opt-in connector behind a setting**, not
  a default. Most value for an organization standardizing on one fuel-card
  program.

---

## 5. Strategy C — Third-party email-receipt parsing services

Services that accept receipts (often **by email collector**) and return
structured JSON:

- **Veryfi** — Receipts OCR API; extracts every line item, tax, tip, vendor into
  JSON; supports an **Email Collector** (send receipts to a dedicated Veryfi
  address). 150+ fields. ([Receipt OCR API](https://www.veryfi.com/receipt-ocr-api/),
  [Process a Document](https://docs.veryfi.com/api/receipts-invoices/process-a-document/),
  [field reference](https://faq.veryfi.com/en/articles/5571268-data-extraction-fields-explained-for-receipts-invoices-api))
- **Taggun** — real-time receipt OCR → JSON (merchant, date, line items, totals).
  ([Taggun](https://www.taggun.io/))
- **DigiParser, Tabscanner, FormX, Extracta** — comparable receipt-to-JSON
  parsers with email/upload intake.

**Why this is the *wrong* layer for this app.** Reimbursements **already has** a
local OCR (RapidOCR) + vision-LLM + offline-regex pipeline. Routing receipts to
Veryfi/Taggun would **send the receipt to a cloud OCR vendor** — squarely against
the local-first privacy promise — to do a job the app already does. The *only*
transferable idea is the **email-collector pattern** (a dedicated address that
collects emailed receipts), which Strategy A implements **locally** without
handing data to a third party.

**Verdict:** do not outsource OCR. Borrow the email-collector concept, keep
extraction local.

---

## 6. Why direct website scraping is **not** recommended

Scraping the gas brands' consumer sites/apps to harvest receipts is a poor
engineering and legal bet:

- **Authentication walls.** Receipt/transaction history sits behind a logged-in
  account, frequently with **MFA/OTP** and, for Sam's Club/Costco, **app-only**
  flows (Scan & Go). A scraper must store and replay user credentials and defeat
  MFA — fragile and a serious security/liability surface.
- **Anti-bot defenses.** Modern retail/loyalty sites deploy bot management
  (device fingerprinting, CAPTCHAs, rate limits). Note: even our *research*
  fetches to `developer.shell.com`, `developer.wexinc.com`, and
  `chevrontexacobusinesscard.com` returned **HTTP 403** to an automated client —
  a concrete sign these properties block non-browser traffic.
- **Terms of Service.** Brand ToS routinely prohibit automated access/scraping
  and credential sharing. On the law: the Ninth Circuit's *hiQ v. LinkedIn*
  line (post-*Van Buren*) suggests scraping **public** data is unlikely to
  violate the CFAA — but that protection is for *public* pages with "no gates."
  Receipt data is the **opposite**: behind a login ("gates down"), so the CFAA
  analysis is far less friendly, and **breach-of-contract / ToS, trespass to
  chattels, and other claims remain available** regardless. ([White & Case](https://www.whitecase.com/insight-our-thinking/web-scraping-website-terms-and-cfaa-hiqs-preliminary-injunction-affirmed-again),
  [EFF](https://www.eff.org/deeplinks/2022/04/scraping-public-websites-still-isnt-crime-court-appeals-declares),
  [Goodwin](https://www.goodwinlaw.com/en/insights/blogs/2022/04/ninth-circuit-web-scraping-does-not-violate-cfaa))
- **Brittleness.** Unannounced DOM/app changes break scrapers continuously; each
  brand needs its own scraper and ongoing maintenance.

**Recommendation:** do not build scrapers against authenticated gas-brand
properties. The sanctioned email channel (Strategy A) provides the same receipts
without the legal, security, and maintenance load.

> **One legitimate adjacent right:** users can file a **CCPA/GDPR data-portability
> request** ("download my data") with a brand and receive their personal data in
> a *structured, machine-readable* format. This is user-initiated, lawful, and
> occasionally yields transaction history — but it is **manual, slow** (CCPA up to
> 45 days; GDPR ~1 month), **inconsistently formatted**, and **not an automatable
> import channel**. Worth documenting for users; not a build target.
> ([CCPA data portability](https://www.clarip.com/data-privacy/ccpa-data-portable/),
> [data portability overview](https://www.techtarget.com/searchcloudcomputing/definition/data-portability))

---

## 7. Financial-aggregator / card-enrichment angle (Plaid, Knot, Stripe)

A tempting shortcut is "read the user's card transactions." It identifies the
*purchase* but **not the itemized fuel receipt**:

- **Plaid Transactions + Enrich** — returns bank/card transactions enriched with
  **merchant name, category, location** (US/CA; ≤100 txns/request). It can tell
  you "$62.41 at a Chevron on 6/14" and categorize it as fuel — but it has **no
  gallons, no price/gal, no fuel grade, no pump-level line items**. Card networks
  simply don't carry Level III detail to a personal-banking aggregator.
  ([Plaid Transactions](https://plaid.com/docs/api/products/transactions/),
  [Plaid Enrich](https://plaid.com/docs/enrich/))
- **Knot "TransactionLink"** — retrieves **SKU/item-level** data by linking a
  user's *merchant account* (Amazon, Walmart, Uber, etc.) via webhooks
  (`NEW_TRANSACTIONS_AVAILABLE` → Sync). It is genuinely item-level **where a
  merchant is supported** — but the published coverage centers on large
  e-commerce/rideshare merchants; **no gas brand is documented as supported**,
  and even Walmart/Sam's coverage would surface store SKUs, not a pump fuel
  receipt with gallons/grade. Coverage is dynamic — confirm via the List
  Merchants endpoint. ([Knot TransactionLink](https://www.knotapi.com/tx-link/),
  [transaction object](https://docs.knotapi.com/api-reference/products/transaction-link/transaction-object),
  [merchants](https://docs.knotapi.com/docs/merchants))
- **Stripe** — relevant only if *you* are the merchant processing the charge;
  irrelevant for reading a consumer's fuel purchases at third-party stations.

**Limitation to state plainly:** aggregators give you the **transaction
envelope** (who/when/how much), which is useful for *reconciliation and
de-duplication* against receipts the pipeline already extracted — but they
**cannot replace the receipt**. The gallons/price-per-gal/grade/station detail a
fuel reimbursement needs comes only from the **receipt itself** (Strategy A) or a
**fleet API** (Strategy B). At best, Plaid could be a *future* corroboration/auto-
match feature, not a receipt source.

---

## 8. Recommended roadmap for *this* app

A phased plan that respects the existing local pipeline and privacy model:

**Phase 1 — Email/IMAP ingestion connector *(do this first)***
- Add an **email intake source** parallel to the watched folder: an IMAP
  poller that pulls unseen messages from a user-designated mailbox/folder,
  extracts attachments + (rendered) body, and enqueues them through the existing
  worker → `_extract_receipt_with_status`.
- Store mailbox credentials/app-password via the existing **`app_secrets.py`**
  store; prefer **OAuth/app-passwords** over raw passwords.
- Add a **content-based gas-receipt classifier** reusing the offline regex
  matchers (sender allow-list + subject/body fuel signals), so non-receipt mail
  is skipped cheaply.
- Track processed messages by **UID + Message-ID** to avoid reprocessing
  (mirror the existing dedup logic).
- Surface it as a **Settings → Intake → Email** card (server URL, folder, poll
  interval, sender allow-list), consistent with current settings UX, and run it
  inside `watch_mode` / the queue worker.
- **Privacy:** purely local; with the local LLM nothing leaves the machine, and
  the existing `LLM_ALLOW_IMAGE` gate already governs the cloud case.

**Phase 2 — Optional fleet connector behind a setting *(only if demand)***
- Add an opt-in **"Business fuel card" connector** with a provider choice
  (**Shell Fleet API** and/or **WEX**). Pull Level III transactions directly and
  map them onto the receipt record (vendor/date/amount/gallons/grade/station),
  **bypassing OCR** for those rows.
- Gate behind explicit user-supplied credentials; document the onboarding
  reality (contract/account required) so expectations are set.
- Keep each provider an isolated adapter — they share no auth model.

**Phase 3 — (Stretch) card-transaction reconciliation, not import**
- Optionally integrate Plaid/Knot to **auto-match** extracted receipts against
  card transactions for de-dup and "missing receipt" detection — explicitly *not*
  as a receipt source, given the itemization limitation in §7.

**Explicitly out of scope:** scraping authenticated brand sites/apps (§6);
outsourcing OCR to Veryfi/Taggun (§5) — redundant with the local pipeline and
contrary to the privacy model.

---

## 9. Open questions / things to verify before building

- **Shell & WEX auth specifics and sandbox availability** — re-verify against the
  live portals (both 403'd automated fetch here): exact OAuth scopes, whether a
  pre-contract sandbox exists, and the minimum account type that can mint
  credentials.
- **WEX self-serve vs. partner-only** — confirm whether an individual business
  cardholder can get API credentials or only a fleet/partner can.
- **Brand email-receipt formats** — collect real samples per brand (HTML body vs.
  PDF attachment vs. image) to tune the classifier/extractor.
- **Knot merchant coverage** — call List Merchants to confirm whether *any* fuel
  brand is supported (none documented as of this writing).
- **ExxonMobil API Hub** — determine whether anything fleet-receipt-relevant is
  actually exposed there (UNVERIFIED).

---

## 10. Sources

**Chevron / Texaco**
- [Chevron Texaco Rewards & Mobile App FAQs (dev.chevronwithtechron.com)](https://dev.chevronwithtechron.com/content/cwt/en_us/home/mobile-app-faqs.html)
- [Mobile Apps: Find Gas & Save (Chevron Texaco Rewards)](https://www.chevrontexacorewards.com/en_us/home/mobile-apps.html)
- [Chevron & Texaco Business Card: explainer (Ramp)](https://ramp.com/blog/fleet-fuel-cards/what-is-chevron-texaco)
- [Chevron Texaco Business Card — fuel-card data for fleet management](https://www.chevrontexacobusinesscard.com/fuel-card-data-for-fleet-management/)

**Shell**
- [Shell Developer Portal — API Catalog](https://developer.shell.com/api-catalog)
- [Transaction Data straight into your mobility system](https://developer.shell.com/use-cases/transaction-data-straight-your-mobility-system)
- [B2B Mobility Card Transaction Data](https://developer.shell.com/api-catalog/v2.1.0/b2b-mobility-card-transaction-data)
- [Shell Developer Portal — Authentication](https://developer.shell.com/docs/authentication)
- [Shell Fleet Transaction Data API detail (apiportal.shell.com)](https://apiportal.shell.com/apidetail/fleettransaction-0)

**WEX**
- [WEX Developer Portal](https://developer.wexinc.com/)
- [WEX APIs for developers (business payments)](https://www.wexinc.com/products/business-payments/for-developers/)
- [WEX fleet card payment management](https://www.wexinc.com/products/fuel-cards-fleet/large-fleets/fleet-card-payment-management/)

**ExxonMobil**
- [Exxon Mobil Rewards+ App FAQs](https://www.exxonmobilfuels.com/en/rewards/faqs/app-faqs)
- [ExxonMobil API Hub](https://apihub.exxonmobil.com/hub)
- [ExxonMobil business system integration](https://www.exxonmobilchemical.com/en/resources/ebusiness-services/business-system-integration)

**BP / Amoco / Circle K / warehouse clubs**
- [earnify (bp America)](https://www.bp.com/en_us/united-states/home/products-and-services/earnify.html)
- [BP launches earnify app & rewards program (C-Store Dive)](https://www.cstoredive.com/news/bp-launches-earnify-app-rewards-program/729124/)
- [Best fuel rewards programs in the US in 2026 (The Points Guy)](https://thepointsguy.com/loyalty-programs/fuel-loyalty-programs/)

**Pay-at-pump / mobile-commerce platforms**
- [Stuzo launches Open Commerce platform (PR Newswire)](https://www.prnewswire.com/news-releases/stuzo-launches-open-commerce-platform-the-infrastructure-standard-for-digital-services-and-experiences-in-fuel-and-convenience-retail-300664214.html)
- [P97 + Verifone certification (Fuels Market News)](https://fuelsmarketnews.com/p97-networks-accelerates-mobile-commerce-for-retail-fuel-and-convenience-stores-with-verifone-certification/)
- [Stuzo: open platforms at the pump (PYMNTS)](https://www.pymnts.com/digital-payments/2018/digital-consumers-convenience-stores-data)

**GasBuddy / Upside**
- [GasBuddy on Datarade](https://datarade.ai/data-providers/gasbuddy/profile)
- [gas-buddy GitHub org](https://github.com/gas-buddy/)

**Third-party receipt OCR / email parsers**
- [Veryfi Receipt OCR API](https://www.veryfi.com/receipt-ocr-api/)
- [Veryfi — Process a Document (docs)](https://docs.veryfi.com/api/receipts-invoices/process-a-document/)
- [Veryfi — extraction fields reference](https://faq.veryfi.com/en/articles/5571268-data-extraction-fields-explained-for-receipts-invoices-api)
- [Taggun Receipt OCR API](https://www.taggun.io/)

**Email ingestion / IMAP-to-webhook**
- [EmailEngine (self-hosted email API)](https://emailengine.app/)
- [imap-to-webhook (GitHub)](https://github.com/watchdogpolska/imap-to-webhook)
- [Forward Email FAQ (self-hosted option)](https://forwardemail.net/en/faq)
- [Email-to-webhook guide (MailSlurp)](https://www.mailslurp.com/guides/email-webhooks/)

**Financial aggregators / enrichment**
- [Plaid Transactions API](https://plaid.com/docs/api/products/transactions/)
- [Plaid Enrich](https://plaid.com/docs/enrich/)
- [Knot TransactionLink](https://www.knotapi.com/tx-link/)
- [Knot transaction object (docs)](https://docs.knotapi.com/api-reference/products/transaction-link/transaction-object)
- [Knot — retrieving/listing merchants](https://docs.knotapi.com/docs/merchants)

**Scraping legality**
- [White & Case — web scraping, ToS & the CFAA (hiQ/Van Buren)](https://www.whitecase.com/insight-our-thinking/web-scraping-website-terms-and-cfaa-hiqs-preliminary-injunction-affirmed-again)
- [EFF — scraping public websites still isn't a crime](https://www.eff.org/deeplinks/2022/04/scraping-public-websites-still-isnt-crime-court-appeals-declares)
- [Goodwin — Ninth Circuit: scraping does not violate CFAA](https://www.goodwinlaw.com/en/insights/blogs/2022/04/ninth-circuit-web-scraping-does-not-violate-cfaa)

**Data portability (CCPA / GDPR)**
- [Data portability for CCPA DSAR responses (Clarip)](https://www.clarip.com/data-privacy/ccpa-data-portable/)
- [What is data portability? (TechTarget)](https://www.techtarget.com/searchcloudcomputing/definition/data-portability)
