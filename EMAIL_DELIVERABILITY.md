# Email deliverability — getting reports into an Outlook inbox (not Junk)

The office recipient uses Microsoft Outlook / Microsoft 365, which routinely sends
automated mail from a consumer `@gmail.com` address straight to **Junk**. The fix is
not "use a nicer Gmail" — it's sending from a **custom domain** whose mail is
cryptographically authenticated with **SPF + DKIM + DMARC**. That alignment, not the
message content, is what makes Outlook accept it.

This app sends every report through **one shared SMTP identity** (the per-user detail
lives in the templated subject/body, see below). Point that SMTP account at a free
transactional email service ("ESP") on your own domain and you get inbox delivery at
near-zero cost (just the domain, ~$10–12/yr; sending stays within the ESP free tier).

> Why not auto-forward from a work Outlook account? Most Microsoft 365 tenants block
> external auto-forwarding via DLP policy, so it silently fails or quarantines, and it
> can't be automated from outside the tenant. Don't build on it.

## One-time setup

1. **Buy a domain** (any registrar). Example: `processingreceiptapp.com`.
2. **Create a free ESP account.** Any of these support standard SMTP + domain DKIM
   (verify the current free-tier limits at signup — a receipt app's volume is tiny):
   - **Brevo** (~300 emails/day) — SMTP-native, guided DKIM/SPF, recommended.
   - **SMTP2GO** (~1,000/mo) — SMTP-first, solid reputation, recommended.
   - **MailerSend** (~3,000/mo), **Resend** (~3,000/mo), **Mailjet** (~6,000/mo).
   - **Amazon SES** — pay-as-you-go (~$0.10/1k) for scale; more setup.
3. **Verify the domain in the ESP.** It gives you exact DNS records — add them at your
   registrar / DNS host:
   - **SPF** (TXT): authorises the ESP's servers to send for your domain.
   - **DKIM** (TXT, often a CNAME the ESP manages): the ESP signs each message; Outlook
     verifies the signature → "really from this domain, untampered".
   - **DMARC** (TXT at `_dmarc.yourdomain`): start permissive, then tighten:
     ```
     _dmarc.yourdomain  TXT  "v=DMARC1; p=none; rua=mailto:dmarc@yourdomain"
     ```
     After a week or two of clean sends, raise the policy to `p=quarantine` then
     `p=reject` to maximise reputation.
4. **Point the app at the ESP.** In **Settings → Email Delivery** set:
   - SMTP host / port / username = the ESP's SMTP credentials (the API key is the
     password — stored in `.app_secrets.json`, never the synced config).
   - **From** = `receipts@yourdomain.com` (a friendly display name helps too).
   - Recipient = the office address.
5. **Send a test report** and confirm it lands in the Outlook **Inbox**.

## Per-report subject & body (templated)

Sending uses one shared identity, but the **subject and body are templated per user
and per report** so the recipient sees who/what each report is for. Configure the
templates in Settings (or leave blank for the built-in defaults). Placeholders:

| Placeholder | Renders |
|---|---|
| `{employee}` | the employee/user name |
| `{date}` | the report date |
| `{count}` | number of receipts |
| `{total}` | grand total, e.g. `$1,234.56` |
| `{job_name}` / `{job_number}` | job fields (blank when unset) |
| `{job_clause}` | a ready-made " for job NN (#123)" phrase, blank when none |
| `{report_name}` | the workbook filename |

A missing/unknown placeholder renders empty rather than erroring, so a hand-edited
template can never break a send. Defaults live in `email_template.py`.

## Belt-and-suspenders

Even with perfect auth, ask the recipient (or their IT) to add `receipts@yourdomain`
to **safe senders / the allowlist** once. Keep content clean: real display name, the
workbook as a normal attachment, a consistent sender — all of which the app already
does.
