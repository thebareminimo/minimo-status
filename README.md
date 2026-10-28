# minimo-status

Public status page for Minimo — **built in-house, hosted independently** of the
infrastructure it monitors, so it stays up (and honest) even during a full
Minimo/Dokploy outage.

## How it works

```
GitHub Actions (independent of Minimo)                     GitHub Pages (static, independent)
────────────────────────────────────────                  ─────────────────────────────────
check.py every 15 min:                                     index.html  ← fetch status.json
  Core services (shallow GET, every run):                  (auto-refresh, stale-detection,
    • api    → api.minimo.it/docs 200                        grouped by Core services / Delivery)
    • webapp → app.minimo.it serving
    • auth   → Supabase /auth/v1/health
  Delivery (deep, real messages):
    • email    → send transactional → verify on Mailosaur
    • whatsapp → send an approved UTILITY template on BeautyGo's active account (every 4h) → accepted
        │  folds results into ↓
        └──────────────► status.json + data/history.json  ──(commit)──► published
                          Telegram alert only on failure
```

- **Independence rule #1** — the page is a static file on GitHub Pages, never on
  Minimo's own servers. Up when Minimo is down.
- **Independence rule #2** — the checker runs on GitHub Actions, not Minimo infra,
  so a full outage doesn't also kill detection. The page shows a *stale* warning
  if the checker itself stops.

## Files
- `check.py` — the public-page probes (3 reachability + 2 delivery + 1 AI engine probe) + history/uptime rollup (stdlib only).
- `pipeline_probe.py` — **DEV-only** AI-assistant end-to-end *pipeline* probe (synthetic inbound → assert async reply). Not on the public page. See "AI assistant — two complementary probes" below.
- `.github/workflows/status-monitor.yaml` — public-page schedule + commit + Telegram.
- `.github/workflows/dev-pipeline-monitor.yaml` — dev pipeline-probe schedule (separate, prod-gated, no page writes).
- `index.html` — the static page (vanilla JS, light/dark, no deps).
- `status.json` — regenerated every run; the page fetches it.
- `data/history.json` — per-component daily status (90-day bars + uptime %).
- `data/incidents.json` — hand-editable incident list shown on the page.

## Probes
**Core services** (shallow reachability GET — cheap, no side effects, runs every 15 min):
- **API** — `api.minimo.it/docs` returns 2xx/3xx.
- **Web app** — `app.minimo.it` serves (unauth root redirects to signin → still "up").
- **Authentication** — Supabase GoTrue `/auth/v1/health` for the prod project (uses the
  public anon key as `apikey`; skips cleanly if `SUPABASE_ANON_KEY` is unset — never false-red).

**Delivery** (deep end-to-end — real messages):
- **Email** — real end-to-end: send via `app.minimo.it/api/transactionals` to a
  Mailosaur inbox, confirm arrival. True delivery, not just an API 200.
- **WhatsApp** — send an approved UTILITY template via `api.minimo.it/public/v1/templates/whatsapp/send`
  to a test number; v1 confirms the send is **accepted** (whole auth→template→dispatch
  pipeline). Confirming the Meta `delivered` webhook is a planned v1.1 upgrade.
  Runs every 4h (not every 15 min) so it doesn't buzz a real phone constantly.
  The template name/lang/params default in `check.py` (`WHATSAPP_TEMPLATE_NAME`,
  `_LANG`, `_PARAMS`, `_BUTTON_URL_PARAM` override them). It MUST be an approved
  template on BeautyGo co36's *active* 360dialog account — when BeautyGo swaps its
  WABA, re-point the name to a template approved on the new account (the API key is
  company-scoped and already routes to the active channel, so it needs no change).
  **Debounce:** because this send goes through a third-party (BeautyGo) 360dialog
  account, a single run can fail for the *customer's* prepaid-credit balance
  (`422 … "lack of payment"`) rather than a Minimo outage. The WhatsApp component
  therefore only flips to `down` / alerts after **N consecutive** failed probes
  (`WHATSAPP_FAIL_THRESHOLD`, default 3); a transient failure stays green. A real,
  sustained Minimo-pipeline outage still trips the threshold and goes red (and the
  15-min `api`/`webapp`/`auth` probes catch an infra outage immediately regardless).
  The streak persists in the component's `fail_streak` field in `status.json`. When
  a Minimo-owned, billing-stable WABA exists (Twilio migration), re-point the probe
  there and this debounce can be relaxed.

## AI assistant — two complementary probes (engine vs pipeline)

The WhatsApp-AI support bot is Minimo's core wedge, and it can break in **two
structurally different places**. We watch both:

**1. Engine probe — `probe_assistant` in `check.py` (PROD, hourly, on the public page).**
Hits `POST /v1/assistants/:id/test` synchronously on The Bare OÜ (co25) and asserts
the reply contains a KB canary. That endpoint calls `handleSupportMessage` directly,
so it exercises the **reply engine only**: OpenAI KB embeddings + prompt compile +
LLM generation (the 2026-09-23 OpenAI-out-of-credits failure). It never touches the
async plumbing — no webhook, no queue, no worker, no persisted row, no dispatch.

**2. Pipeline probe — `pipeline_probe.py` (standalone DEV workflow today; can now fold
into the public page as the `assistant_pipeline` component in `check.py` — see "Promote
to the public page" below).**
`/test` is structurally blind to the real inbound→reply path, which is **async**:

```
360dialog inbound  →  POST /public/v1/webhooks/whatsapp  (ingest, returns 200)
                   →  BullMQ whatsapp-webhook-queue       (attempts:1, NO retry)
                   →  WhatsappWebhookProcessor            (worker, ~5s debounce)
                   →  assistant.service handleSupportMessage (reply generation)
                   →  outbound-delivery-tracker.begin     (persist BOT historymessage row)
                   →  whatsapp-outbound.dispatcher        (send to provider)
```

That async path is exactly where the **2026-09-22→23 P0** lived: inbound arrived,
the toggle was ON, and the bot produced **zero replies on every chat with no error**.
The engine probe stays green through that outage; this one catches it.

`pipeline_probe.py` injects a **synthetic Meta/360dialog-shaped inbound** into the
**dev** backend for a dedicated test company and then asserts, via the dev database
(PostgREST), that the async pipeline **persisted an assistant reply row** within N
seconds. If no reply row appears → the pipeline stalled → the probe fails (exit 1).

**Safety perimeter (hard):**
- Dev backend (`dev-api.minimo.it`) + the **Company Test (co3)** `minimo_ai` channel only. Never a paying company.
- Synthetic sender is a **non-routable** MSISDN (`15550001111` — a US `555-000` number that can never be a real WhatsApp account). The pipeline's terminal dispatch does fire a 360dialog call toward it, but it **can never reach a human**.
- The assertion is on **reply generation** (the persisted `senderType=BOT` row), not on delivery — `sent` vs `failed` at the provider is irrelevant. A reply row proves the pipeline ran all the way to dispatch, which is what silently broke in the P0.
- **Prod-gated:** targets dev, never writes `status.json`, and does **not** page the prod ops Telegram group. A confirmed stall just fails the GitHub job (native notification). Pointing it at prod / enabling Telegram is a separate later decision.

Coverage summary:

| Stage | engine probe (`/test`) | pipeline probe |
|-------|:----------------------:|:--------------:|
| webhook ingest | ✗ | ✓ |
| BullMQ queue   | ✗ | ✓ |
| worker drains  | ✗ | ✓ |
| reply generation (LLM/KB) | ✓ | ✓ |
| persist `historymessage` row | ✗ | ✓ |
| dispatch to provider | ✗ | ✓ |
| **would have caught the 2026-09-22→23 P0** | **no** | **yes** |

Config is env-driven (dev defaults baked in): `PIPELINE_API_BASE`, `PIPELINE_REST_BASE`,
`PIPELINE_SUPABASE_KEY` (required — dev service_role), `PIPELINE_COMPANY`,
`PIPELINE_BUSINESS_NUMBER`, `PIPELINE_CHANNEL_ID`, `PIPELINE_SENDER`, `PIPELINE_TIMEOUT`,
`PIPELINE_ATTEMPTS`, and `PIPELINE_ALERT`/`PIPELINE_TELEGRAM_*` (alerting, off by default).
Driven by `.github/workflows/dev-pipeline-monitor.yaml` (every 30 min + manual dispatch),
which is fully separate from `status-monitor.yaml` and never touches the public page.

> **Note:** workflow files can't be pushed by the repo's `repo`-scoped PAT, so the
> live `.github/workflows/dev-pipeline-monitor.yaml` is added via the GitHub web UI
> (same mechanism as `status-monitor.yaml`). Its verbatim, reviewable copy lives at
> **`ops/dev-pipeline-monitor.yaml`** — activation is just copying it into
> `.github/workflows/` and setting the `PIPELINE_SUPABASE_KEY` secret.

### Promote the pipeline probe to the public page (prod)

`check.py` has a `probe_assistant_pipeline()` that reuses `pipeline_probe.py` as the
`assistant_pipeline` component ("AI Assistant (end-to-end)", AI category). It is
**conditional**: the component is appended to `COMPONENTS` **only when
`PIPELINE_SUPABASE_KEY` is set**, so with no secret the page is byte-identical — merging
this integration is completely inert. Once active it folds into the SAME `failed` /
`summary` workflow outputs as every other component, so the existing **"Notify Telegram
on failure"** step in `status-monitor.yaml` alerts on a confirmed stall with **no new
Telegram wiring** (same bot token + `TRANSACTIONAL_TEST_TELEGRAM_CHANNEL` ops channel).
A cross-run debounce (`PIPELINE_FAIL_THRESHOLD`, default **2**) means one transient
async hiccup never flaps the public AI row — only a sustained stall reds it + alerts.

**Blocked on one prerequisite (prod has no safe injection target).** The synthetic
inbound must resolve to an **active, non-deleted, non-Twilio `minimo_ai` channel**
(`findActiveChannelByPhoneNumber` requires `deleted_at IS NULL`). On prod the only such
channels belong to **paying companies** (AD Productions co34, BeautyGo co36, FreedHome
co58) — which the hard safety perimeter forbids. There is no prod equivalent of dev's
dedicated Company Test (co3); co25 (Minimo / The Bare OÜ, the engine-probe company) has
the probe assistant `ac50a817` bound but its `minimo_ai` channels are all
DISCONNECTED/deleted. **Activation therefore needs a dedicated test `minimo_ai` channel
on a non-paying company first** (recommended: co25, Minimo's own entity).

**Activation checklist (once the test channel exists):**
1. Provision a dedicated `minimo_ai` channel on a non-paying company (e.g. co25): a
   synthetic unique business number + `channel_id`, `agent_id = ac50a817…`, provider
   `360dialog` (a dummy D360 key is fine — the probe asserts on the persisted reply row,
   not on delivery), `status ACTIVE`, `deleted_at NULL`.
2. Set the prod secrets/vars (see Secrets below): `PIPELINE_SUPABASE_KEY_PROD`,
   `PIPELINE_BUSINESS_NUMBER`, `PIPELINE_CHANNEL_ID`, var `PIPELINE_COMPANY`.
3. Add the env + `PROBE_PIPELINE` gate to `status-monitor.yaml` (workflow file → GitHub
   web UI or a `workflow`-scoped PAT). The exact additions are in
   **`ops/status-monitor.prod-pipeline.snippet.yaml`**.
4. Run `workflow_dispatch` once; confirm the new `assistant_pipeline` row is
   `operational` and a simulated stall reds it + fires the Telegram alert.

## Secrets (GitHub → repo settings → Secrets)
Reused from `web-app-v2`'s existing `transactional-prod-test` action:
`MINIMO_PROD_API_KEY`, `MAILOSAUR_SERVER_ID`, `MAILOSAUR_APIKEY`,
`TRANSACTIONAL_TEST_UID_PROD`, `TELEGRAM_BOT_TOKEN`,
`TRANSACTIONAL_TEST_TELEGRAM_CHANNEL`. Optional: `WHATSAPP_TEST_RECIPIENT`
(defaults to the Andrea test number).

For the **dev pipeline probe** (`dev-pipeline-monitor.yaml`): `PIPELINE_SUPABASE_KEY`
(required — the **dev** Supabase `service_role` key, used read-only to assert the reply
row). Optional alerting (off by default): repo variable `PIPELINE_ALERT=true` plus
secrets `PIPELINE_TELEGRAM_BOT_TOKEN` + `PIPELINE_TELEGRAM_CHAT` (point at a **dev**
channel, never the prod ops group, until a separate GO).

For the **public-page pipeline component** (`check.py` `assistant_pipeline`, prod):
`PIPELINE_SUPABASE_KEY_PROD` (required — the **prod** Supabase `service_role` key, used
read-only to assert the reply row), `PIPELINE_BUSINESS_NUMBER` + `PIPELINE_CHANNEL_ID`
(the dedicated prod test channel's number + id), and the repo variable `PIPELINE_COMPANY`
(the test company id, e.g. `25`). `PIPELINE_API_BASE` (`https://api.minimo.it`) and
`PIPELINE_REST_BASE` (`https://nelourjougsxqvekwfqt.supabase.co/rest/v1`) are plain
(non-secret) env in the workflow. Telegram reuses the existing `TELEGRAM_BOT_TOKEN` +
`TRANSACTIONAL_TEST_TELEGRAM_CHANNEL` — no new alert secrets.

## Deploy
1. Push this repo to `thebareminimo/minimo-status`.
2. Settings → Pages → **Deploy from branch** → `main` / root.
3. Add the secrets above.
4. Point `status.minimo.it` (CNAME → GitHub Pages) — repointing from the current
   incident.io page — and add a `CNAME` file / the custom-domain in Pages settings.
5. Run the workflow once (`workflow_dispatch`) to seed real data.
