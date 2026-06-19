# MULTIUSER.md — Multi-user / Multi-tenant Design Plan

> **Status: PLAN ONLY.** No behavioral code changes are being made now. This
> document inventories the single-user assumptions baked into the current app and
> lays out an incremental, shippable path to supporting multiple isolated users
> *if and when we decide to.* Until then, **single-user remains the default and
> only supported mode.**
>
> Line numbers below are approximate (the source files move) — treat them as
> "where to look," not exact anchors.

---

## 1. Purpose & scope

The app today is a **local, single-user** receipt → reimbursement tool: one
person (or one office admin acting as one identity) drops in receipts and gets a
report. Everything — the queue, the board, results, config, secrets, folders, and
the active LLM selection — is process-global and assumes exactly one tenant.

This plan answers: *what would it take to let several users share one running
instance without seeing each other's receipts, results, or settings?* It is a
design study with a recommended approach and a phased migration, **not** an
implementation. Each phase is written to be independently shippable and to leave
single-user behavior unchanged when the multi-user flag is off.

Out of scope: changing the local-only privacy guarantee. Multi-user does **not**
mean cloud — it means more than one identity sharing one local box (e.g. a small
office, a shared workstation, or a self-hosted instance behind a reverse proxy).

---

## 2. Current architecture summary (why it's single-tenant)

- **One process, one queue, one worker.** A single background `_worker_thread`
  (`server.py` ~182) drains a single global `_work_queue` deque (~104) and writes
  to a single global `_results` list (~110). The kanban board (`_kanban`, ~107) is
  one shared dict.
- **One config / state / secrets file.** `output/.app_config.json`
  (`process_receipts.py` ~84), `output/.app_state.json` (`server.py` ~83), and
  `.app_secrets.json` (`app_secrets.py`) each describe a single tenant.
- **One set of working folders** under `output/` and `receipts/` (`server.py`
  ~71–79), shared by all work.
- **One LLM, mutated process-wide.** The active model + endpoint live as module
  globals in `process_receipts.py` and are changed at runtime by everyone.
- **SSE fans out to everybody.** `/events` (`server.py` ~1672) broadcasts every
  board/log event to every connected client via `_broadcast` (~504).

There is **no identity concept anywhere.** The only access control is an
*optional* shared-secret gate (`_auth_guard`, `server.py` ~1333; exempt paths
~1340) keyed off the `APP_AUTH_TOKEN` env var — a single token shared by all
clients, with no per-user notion, no session, no cookie/JWT, and no `user_id` on
any request. Either you have the token (you're "the user") or you don't.

---

## 3. Inventory of single-user assumptions

Every item below is a process-global that one tenant owns implicitly. Per-user
scoping means giving each a `user_id` dimension (a `dict[user_id, …]` in memory, a
`output/{user_id}/…` path on disk, or a `user_id` column/key in persisted state).

### 3.1 Runtime state — `server.py`

| Component | Current global | ~Line | What per-user scoping requires |
|---|---|---|---|
| Work queue | `_work_queue` (deque) | 104 | `dict[user_id, deque]` or one queue with `user_id`-tagged items + fair draining |
| Queue lock | `_work_lock` | 105 | Per-user lock (or one lock guarding the per-user map) |
| Kanban board | `_kanban` (dict) | 107 | `dict[user_id, dict]`; board reads/writes scoped to caller |
| Board lock | `_kanban_lock` | 107 | Per-user lock |
| Results store | `_results` (list) | 110 | `dict[user_id, list]`; every read/write/generate/clear scoped |
| Results lock | `_results_lock` | 110 | Per-user lock |
| Last batch context | `_last_context` | 113 | `dict[user_id, …]` (employee/job defaults are per user) |
| Benchmarks | `_benchmarks` | 117 | `dict[user_id, list]` (or keep global "instance" metrics, opt-in) |
| Seen-intake set | `_seen_intake` | 121 | Per-user intake → `dict[user_id, set]` keyed to per-user intake dir |
| Rejected reasons | `_rejected_reasons` | 127 | `dict[user_id, …]` |
| Worker thread | single `_worker_thread` | 182 | Keep ONE worker (LLM is the bottleneck) but iterate fairly across users — see §4 |
| Concurrency gate | `_ConcurrencyGate`, `CONCURRENCY_CEILING=8` | 177 | Gate stays global (shared VRAM); fairness layered on top |
| SSE subscribers | `_subscribers` (list) | 185 | Subscriber carries its `user_id`; broadcast filters by it — see §3.3 |
| Item cache | `_item_cache` | 189 | `dict[user_id, …]` (or key existing cache by `(user_id, …)`) |
| Status timestamps | `_status_timestamps` | 193 | `dict[user_id, …]` |
| Stall checker | stall loop | ~1190 | Iterate per-user boards instead of the one global board |

### 3.2 Pipeline / model globals — `process_receipts.py`

| Component | Current global | ~Line | What per-user scoping requires |
|---|---|---|---|
| Active distill model | `_active_distill_model` | 108 | Either keep global (one model in VRAM) or make it a per-user *preference* resolved against a shared loaded model |
| Active OCR model | `_active_ocr_model` | 109 | Same as above |
| LLM-OCR toggle | `_llm_ocr_enabled` | 110 | Per-user preference (`dict[user_id, bool]`) — cheap, no VRAM cost |
| LLM endpoint | `LMSTUDIO_BASE_URL` | 44 | **Stays global** — one local model server per box (see §8) |
| Amount limits | `AMOUNT_LIMITS` | 67 | Per-user audit config (`dict[user_id, …]`) |
| Max receipt age | `MAX_RECEIPT_AGE_DAYS` | 68 | Per-user audit config |

> **Key tension:** the per-receipt pipeline (`_extract_receipt_with_status`) reads
> these module globals directly. A multi-tenant in-process model must either (a)
> pass a per-user *settings bundle* down through the pipeline call, or (b) keep the
> heavy LLM bits global and scope only the cheap policy bits (audit limits, LLM-OCR
> toggle, prompt-affecting preferences). Option (b) is far less invasive.

### 3.3 SSE & persistence

- **SSE broadcast (`/events` ~1672, `_broadcast` ~504)** sends *all* events to
  *all* clients. Per-user: each subscriber records its `user_id`; `_broadcast`
  delivers an event only to subscribers whose `user_id` matches the event's owner.
  Per-user channels also keep heartbeat/poll behavior intact.
- **Persisted state** (`.app_state.json`) snapshots the one global `_results` +
  board. Per-user: one state file per user under `output/{user_id}/.app_state.json`,
  loaded/saved per tenant.
- **Config / secrets** (`.app_config.json`, `.app_secrets.json`) are single-tenant
  blobs. Per-user: per-user config/state/secrets directories.

### 3.4 Filesystem & naming

- Working dirs (`RECEIPTS_FOLDER`/`INTAKE_FOLDER`, `OUTPUT_FOLDER`,
  `IMAGES_FOLDER`, `PROCESSING_FOLDER`, `REJECTED_FOLDER`, `ARCHIVE_FOLDER`;
  `server.py` ~71–79) are shared. Per-user: rooted under `output/{user_id}/`.
- **File naming collisions.** `rename_receipt_image` (`process_receipts.py` ~2270)
  names by date + vendor with numbered-suffix collision resolution scoped to **one
  shared images dir**. With two users this means *cross-user* collisions and
  numbering races. Per-user image dirs make naming naturally per-tenant.
- **Reports.** Spreadsheet generation, `/reports`, `/reports/clear`, and
  `finish_batch` all glob the one global output folder. Per-user: glob the user's
  own `output/{user_id}/` only.

---

## 4. Target architecture options

### Option A — Process-per-user / container-per-user

Run **one independent app instance per user**, each with its own config/state/
folders and its own port, behind a reverse proxy that maps an authenticated
identity to the right backend.

- **Pros:** near-zero code change — each instance *is* the current single-user
  app. Perfect isolation (separate processes, separate disk roots). Trivial to
  reason about and to roll back. Reuses the existing `APP_AUTH_TOKEN` gate
  per-instance.
- **Cons:** N copies of everything (RAM/disk footprint scales with users). The
  reverse proxy + per-user routing/lifecycle (start/stop/health) is new ops
  surface. Each instance still talks to the *same* local LLM, so you do not escape
  the VRAM bottleneck (see §8) — you just add scheduling contention the LLM server
  has to absorb.
- **Best for:** a handful of users on a beefy box, or a self-hoster who wants the
  simplest possible isolation and is comfortable with `docker compose` per user.

### Option B — In-process multi-tenant

One process, but every global from §3 is scoped by `user_id`: queues, board,
results, caches, timestamps become `dict[user_id, …]`; data dirs become
`output/{user_id}/…`; config/state/secrets become per-user files; SSE becomes
per-user channels; auth middleware injects `user_id` into request scope; the
single worker drains **fairly** across users (round-robin / weighted) under the
*shared* global concurrency gate.

- **Pros:** one process, one model load, one ops surface. Memory shared
  efficiently. Fair scheduling can be made explicit and tunable.
- **Cons:** touches nearly every module — high blast radius, large test
  expansion, real risk of a scoping bug leaking one user's receipts to another.
  Path-traversal safety on `user_id` becomes load-bearing for isolation (§5).
- **Best for:** many lightweight users, or when per-instance overhead is
  unacceptable.

### Recommendation

**Start with Option A** for any near-term real need: it ships the isolation
guarantee with minimal risk and no rewrite, and it composes with the existing
auth token. **Treat Option B as the long-term target** only if per-instance
overhead becomes a problem or we need >~5–10 concurrent tenants. If we pursue B,
do it via the phased plan in §7 so single-user installs keep working untouched.

#### The shared local-LLM bottleneck (applies to BOTH options)

There is **one** local model in VRAM, and inference is serialized by the
`_ConcurrencyGate` (`server.py` ~177, ceiling 8). Adding users does **not** add
throughput — it adds *contention* for the same scarce resource. Consequences:

- The global gate must stay **global** (per-user gates would oversubscribe VRAM
  and degrade everyone to request timeouts → the offline parser, exactly what the
  ceiling exists to prevent — see CLAUDE.md concurrency notes).
- **Fairness** matters: a single user dropping 500 receipts must not starve
  another user's 3. The worker should drain by **fair scheduling** across
  per-user queues (round-robin one item per user per cycle, or weighted by
  outstanding count) rather than FIFO over a single shared queue.
- In Option A, fairness is whatever the LLM server does with concurrent
  connections (typically FIFO) — less controllable. In Option B, we own the
  scheduler and can be explicit. This is a point in B's favor for many-user cases.

---

## 5. Authentication & identity design

Today: a single optional shared secret (`APP_AUTH_TOKEN`, `_auth_guard`
~1333). Multi-user needs a real identity per request.

**Options (pick one; all keep inference local):**

- **Session cookies + local user store.** Username/password (hashed, e.g. argon2)
  in a small local users file/db; login sets a signed, HttpOnly session cookie;
  middleware resolves the cookie → `user_id`. Simplest fit for a self-hosted,
  local app. *Recommended default for Option B.*
- **JWT (stateless).** Signed bearer token carrying `user_id`; good if a reverse
  proxy already issues tokens. No server-side session store.
- **OAuth2 / reverse-proxy header.** Delegate auth to an upstream proxy
  (e.g. an SSO/identity-aware proxy) that injects a trusted `X-Forwarded-User`
  header; the app trusts it only on a loopback/socket binding. Pairs naturally
  with **Option A** routing.

**How `user_id` flows into request scope.** Add a FastAPI dependency /
middleware that resolves identity once (cookie/JWT/header), rejects unauthenticated
requests (except the existing exempt paths ~1340 plus a `/login`), and attaches a
canonical `user_id` to the request. Every state accessor (queue, board, results,
folders, config) then takes `user_id` from request scope instead of reaching for a
global. The existing `APP_AUTH_TOKEN` gate remains as a coarse instance-level lock
in front of all of this (or is subsumed by the login flow).

**Path-traversal safety (load-bearing for isolation).** Because `user_id` becomes
part of a filesystem path (`output/{user_id}/…`), it MUST be validated/normalized
before use: allow only a strict charset (e.g. `[a-z0-9_-]`), reject `.`/`..`/
separators, and resolve the final path with the same `_serveable`-style
containment check already used for `GET /receipt-image` (must `resolve()` inside
the per-user root). A bad `user_id` must never escape its directory or read
another user's files.

---

## 6. Data isolation

Per-user directory layout (Option B; Option A uses the same shape, one root per
instance):

```
output/
  {user_id}/
    .app_config.json        # per-user settings (models pref, audit limits, …)
    .app_state.json         # per-user crash-safe results + board snapshot
    .app_secrets.json       # per-user SMTP creds etc.
    intake/                 # per-user watched intake folder
    processing/             # in-flight images
    images/ (receipts/)     # renamed completed images, dated subfolders
    rejected/
    archive/                # finish-batch archive (outside scanned dirs)
    Reimbursements_*.xlsx   # generated reports + CSVs
```

- **Config/state/secrets** become per-user files at the paths above. The loaders
  (`_load_config`/`_save_config`, `STATE_FILE` persist/restore, `app_secrets.py`)
  take a `user_id` (or a resolved per-user root) instead of module-level constants.
- **File naming** (`rename_receipt_image` ~2270) operates within the user's own
  `images/` dir, so date+vendor numbered-suffix collision resolution is naturally
  per-tenant — no cross-user collisions or numbering races.
- **Reports** (`/reports`, `/reports/download`, `/reports/clear`, spreadsheet
  generation, `finish_batch`) glob and write only within `output/{user_id}/`.
- **Orphan/maintenance scans** (`_collect_orphans`) scan only the caller's root;
  the archive-skip safeguard stays.

A global "instance admin" view (all users' benchmarks, disk usage, model status)
can read across roots but is the only thing allowed to.

---

## 7. Migration phases (incremental, each shippable)

Each phase is gated so that with the multi-user flag **off**, behavior is byte-for-
byte the current single-user app. The default `user_id` for single-user mode is a
constant (e.g. `"default"`), so the per-user map degenerates to a single entry and
`output/default/…` can be aliased to today's `output/…` for backward compat (§8).

### Phase 0 — Identity seam (no behavior change)
Introduce a `current_user()` request dependency that always returns
`"default"` today. Thread it through accessors as an argument *without* changing
storage yet. **Touch-points:** `server.py` (add the dependency; pass `user_id` into
queue/board/results helpers as a param defaulting to `"default"`).
**Testing:** existing 434 tests must pass unchanged; add tests asserting the
default identity resolves and that accessors accept the new param.

### Phase 1 — Per-user data dirs behind a flag
Add `MULTIUSER_ENABLED` (env, default off) and a `user_root(user_id)` helper that
resolves `output/{user_id}/…` (with the §5 traversal guard). When off, all roots
collapse to today's paths. **Touch-points:** `server.py` folder constants (~71–79),
`process_receipts.py` `CONFIG_FILE`/naming (~84, ~2270), `STATE_FILE` (~83),
`app_secrets.py`. **Testing:** parametrize the existing path-isolation conftest
fixture over `user_id`; new tests for traversal rejection and per-user dir
creation.

### Phase 2 — Per-user runtime state
Convert the in-memory globals from §3.1/§3.2 to `dict[user_id, …]` (queue, board,
results, caches, timestamps, last-context, benchmarks, rejected, seen-intake) and
per-user locks. Keep the LLM endpoint + concurrency gate global. Scope per-user
the cheap policy bits (audit limits, LLM-OCR toggle). **Touch-points:** `server.py`
state block (~104–193), `process_receipts.py` audit/toggle globals (~67–68, ~110).
**Testing:** new suite asserting two `user_id`s never see each other's queue/board/
results; persistence round-trips per user.

### Phase 3 — Per-user SSE + worker fairness
Tag each SSE subscriber with `user_id`; `_broadcast` (~504) delivers only matching
events. Replace the single shared FIFO drain with a **fair scheduler** across
per-user queues under the global gate; the stall checker (~1190) iterates per-user
boards. **Touch-points:** `/events` (~1672), `_broadcast` (~504), worker drain
loop, `_ConcurrencyGate` consumers, stall loop. **Testing:** assert a client gets
only its own events; assert a flooding user can't starve another (fair-drain
ordering test).

### Phase 4 — Auth + admin
Add the chosen auth scheme (§5), the `current_user()` dependency resolving real
identity, a `/login` (+ logout), a local user store, and an instance-admin view
(cross-user status/usage). Flip `MULTIUSER_ENABLED` to a supported mode.
**Touch-points:** new auth middleware/dependency, `_auth_guard` interplay (~1333),
exempt paths (~1340), new routes, users store. **Testing:** auth happy/failure
paths, traversal/identity-spoofing attempts, session lifecycle, admin-only access.

---

## 8. Risks & open questions

- **LLM/VRAM bottleneck is fundamental.** Multi-user adds users, not throughput.
  Decide the fairness policy (round-robin vs weighted) and document that latency
  scales with concurrent tenants. Per-user model *preferences* are fine, but
  loading multiple distinct models into one box's VRAM is generally not — likely a
  per-instance "this box runs model X" constraint, with preference resolved against
  the loaded model.
- **Secrets handling.** Per-user SMTP/credentials multiply the secret surface;
  ensure per-user `.app_secrets.json` files inherit the same out-of-band handling
  and never land in the per-user config blob or in SSE/state snapshots the browser
  reads.
- **Backward compatibility.** Existing single-user installs have data at today's
  `output/…`. The migration must either alias `output/default/…` → `output/…` or
  ship a one-time, reversible mover. Single-user (`MULTIUSER_ENABLED` off) must
  stay the zero-config default forever.
- **Isolation bugs are privacy bugs.** A missed `user_id` scope = one user's
  receipts visible to another. This raises the bar on review/tests; the
  path-traversal guard (§5) and a cross-user leak test suite are non-negotiable
  before Phase 4 ships.
- **Test-suite expansion.** The current 434 tests assume a single tenant and
  module-global model state (some rely on `_active_ocr_model == ""`). Multi-user
  needs a per-user fixture layer and explicit leak/fairness tests; budget for a
  meaningful suite growth and for keeping the single-user path green throughout.
- **Open questions.** Do we ever need cross-user reports (an admin assembling one
  workbook for several employees) — and does that reintroduce shared state we just
  split apart? Is identity self-service (sign-up) or admin-provisioned only? Does
  watch-mode / the scheduler (`watch_mode.py`, `scheduler.py`) become per-user, and
  if so how do per-user watched folders + email schedules coexist on one box?
