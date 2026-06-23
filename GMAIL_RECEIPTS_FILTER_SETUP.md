# Gmail “Receipts” label + filter — setup

This is the **recommended** way to feed the app's Email Intake. Instead of pointing
the app at your whole inbox (which makes it try to read *every* email — security
alerts, newsletters, YouTube notifications — as a receipt and burn LLM quota), you
make Gmail label just the receipts, and the app polls **only that label**.

Why keyword matching instead of a sender allowlist? Most fuel brands don't email
per‑transaction receipts, the ones that do don't publish a stable sender address,
and **forwarding or a privacy relay (e.g. DuckDuckGo Email Protection) rewrites the
`From:` header** — so a sender list misses them. The subject/body survive all of
that, so a keyword filter is far more robust.

## 1. Import the filter

1. In the app: **Settings → Email Intake → “⬇ Download Gmail filter (.xml)”**
   (or grab `gmail_receipts_filter.xml` from the repo).
2. In Gmail (web): **Settings (⚙) → See all settings → Filters and Blocked
   Addresses → Import filters**.
3. Choose the downloaded `gmail_receipts_filter.xml`, click **Open file**, then
   **Create filters**.
   - Optionally tick **“Also apply filter to matching conversations”** to label mail
     already in your inbox.

This creates a filter that applies the **`Receipts`** label to any message matching
common receipt keywords (`"transaction total"`, `"your receipt"`, `gallons`,
`subject:receipt`, …) **or** a verified fuel‑receipt sender (Shell, Chevron,
GasBuddy, Sheetz, Upside, …), while excluding known noise (`google.com`,
`youtube.com`).

> The `Receipts` label is created automatically the first time the filter matches a
> message. To create it up front: **Labels → Create new label → `Receipts`**.

## 2. Point the app at the label

In **Settings → Email Intake**, set **Mailbox / folder** to:

```
Receipts
```

instead of `INBOX`. Save. The poller now only ever reads labelled receipts.

## 3. (Optional) Add the verified senders to the app too

The same Settings card has **“Add verified fuel‑receipt senders”**, which fills the
*Only accept from* box with the handful of confirmed brand domains as a secondary
net. This is optional — the Gmail filter is the primary mechanism.

## Tuning

- **Too much gets labelled?** Edit the filter in Gmail and tighten the keywords
  (e.g. drop the broad `subject:receipt` / `gallons`), or add more `-from:` excludes.
- **A receipt was missed?** Just apply the `Receipts` label to it by hand — the app
  picks it up on the next poll. Then add its sender/keyword to the filter so it's
  caught automatically next time.
- **Dedicated receipts Gmail?** Then you barely need keywords — forward everything in
  and either label all of it or just point the app at `INBOX`.

## How receipts get into the label

- **Forward / BCC** a receipt email to this Gmail.
- Point a loyalty app's receipt emails (Shell, Chevron via your relay, GasBuddy, …)
  at this address.
- Use a **photo of a paper receipt** as an attachment — the filter also matches
  by keyword in your forwarding note/subject.

Either way, the app reads the labelled message, distils the fields, and — for an
emailed (HTML) receipt with no photo — renders a **JPEG copy of the receipt** so the
report still contains the actual document your office requires.
