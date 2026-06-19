# ROADMAP.md — Forward-looking plan

> **Past changes live elsewhere.** The historical changelog is the
> **"Recent changes"** log at the bottom of [`CLAUDE.md`](CLAUDE.md) (append-newest-
> at-top). This file is the *forward* view: what's planned and in progress.
>
> **GitHub-native tracking is also an option.** These items can equally be tracked
> with a **GitHub Projects** board (kanban over issues), **Milestones** (group by
> release), and **Issues** (one per item, with labels). This file is the lightweight,
> in-repo mirror; if/when we adopt a Projects board, keep the two in rough sync or
> let the board become the source of truth and link it here.

Status key: 🟡 in progress · 🟢 planned · ✅ done (move to CLAUDE.md changelog)

---

## Planned / In progress

### LLM provider redesign — ✅ done
- [x] **Unified provider config** — `provider` (`local` / `openrouter`) dispatches in
      `_apply_llm_server_config`; the scattered `llm_server` / `llm_model_config`
      keys are migrated, not competing. Client construction centralized in
      `process_receipts.make_client()`.
- [x] **Fix the "stuck on Docker URL" bug** — an explicit `server_type: "custom"` is
      always honoured (blank URL → localhost, never the docker fall-through); GET
      `/settings/llm-server` reports the *configured* URL plus the effective one.
- [x] **Kill the silent autodetect overwrite** — the frontend no longer silently
      POSTs `/llm-server/autodetect`; recovery is the explicit "Auto-detect" button.

### OpenRouter provider (+ privacy mode) — ✅ done
- [x] **OpenRouter as a selectable provider** alongside the local LLM (opt-in, off by
      default; key stored as a secret).
- [x] **Auto-select the best FREE vision-capable model** — filters on zero token
      price + image input modality, ranks by family/context (`/models/openrouter`,
      `_openrouter_autopick`).
- [x] **Privacy mode toggle** — "send receipt image" vs "send OCR text only"
      (`LLM_ALLOW_IMAGE` gates the LLM-OCR + vision-rescue image passes). Cloud use
      is explicit, opt-in, and warned in the UI.

### Surface env/constant-only tunables in Settings — 🟡 in progress
Many knobs existed only as env vars / module constants. Now surfaced under
Settings → Image Processing → *Advanced tuning* (persisted in `processing`):
- [x] LLM timeout / retries
- [x] Image store max-px (downscale ceiling)
- [x] PDF max pages
- [x] Max upload size (`MAX_UPLOAD_BYTES`)
Still env-only by design (internal/rarely-tuned — expose later if asked):
- [ ] Orientation thresholds (`ORIENT_MIN_SCORE` / `ORIENT_IMPROVE_RATIO`, etc.)
- [ ] SSE intervals (`SSE_POLL_SECS` / `SSE_HEARTBEAT_SECS`)
- [ ] Stall timeouts (stall-checker thresholds)
- [ ] Folder / archive caps

### Multi-user support — 🟢 planned
- [ ] Per-user isolation of state, data dirs, config/secrets, SSE, and reports.
      Design and phased migration are documented in **[`MULTIUSER.md`](MULTIUSER.md)**
      (plan only — single-user remains the default).

---

## Notes

- When an item ships, record it in the **CLAUDE.md "Recent changes"** log and tick
  (or remove) it here.
- Keep this file skimmable — link to a design doc (like `MULTIUSER.md`) for anything
  that needs more than a few bullets.
