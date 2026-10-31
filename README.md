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
  It sends FROM Minimo's own Meta-direct number (+34 656 48 88 97, ch30, pinned with
  `sender` = phone_number_id `1434458863076380`) with a co25 key (`WHATSAPP_PROBE_API_KEY`,
  else `AI_PROBE_API_KEY`), so it measures the platform on a number Minimo owns, not a
  customer's number (until 2026-10-31 it used BeautyGo's 360dialog key, and BeautyGo's own
  migration turned the page red). Template `minimo_status_check` (en_US, UTILITY, no
  params) must stay approved on Minimo's WABA `1819617479061624`; overrides:
  `WHATSAPP_TEMPLATE_NAME`, `_LANG`, `_PARAMS`, `WHATSAPP_PROBE_SENDER`.
  **Debounce:** a single paid send can fail for a transient Meta/billing blip rather
  than a Minimo outage. The WhatsApp component
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

**2. Pipeline probe — `pipeline_probe.py` (DEV only, prod-gated, NOT on the public page).**
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

## AI on every channel (PROD, on the public page)

People reach Minimo's AI on WhatsApp, Telegram and Slack. The engine probe above
can't see any of those channels, so each one has its own row under **AI**:

- **AI on WhatsApp** (`probe_ai_whatsapp`, hourly with the engine probe) — the real
  end-to-end. It posts a synthetic Meta-shaped inbound message to
  `api.minimo.it/public/v1/webhooks/whatsapp` for Minimo's own AI number
  (+34 656 48 88 97, The Bare OÜ co25, channel 30, `minimo_ai`), then reads the
  conversation back through the public API until the AI reply shows up. That's the
  whole prod pipeline from the table above (ingest → queue → worker → AI → dispatch),
  the same path every customer's support bot uses, so it catches the 2026-09-22/23
  class of outage in prod. Two attempts per run, plus `AI_WHATSAPP_FAIL_THRESHOLD`
  (default 2) consecutive failed runs before the row goes red and Telegram fires.
  - Sender: non-routable `15550001111`. The reply's final send can't reach a person.
  - Read-back key: `AI_PROBE_API_KEY`, a co25 key with **only** the `whatsapp`
    permission (`api_key` id `50066a78511b`). No database access.
  - Side effect: a "Minimo Status Probe" conversation in co25's inbox that gets one
    exchange per hour. That's expected.
- **AI on Telegram** (`probe_ai_telegram`, every run) — no company of ours has a
  Telegram chat linked to Mo, so this checks the connection: the `@minimoaibot`
  token works, Telegram's webhook points at api.minimo.it with no fresh delivery
  errors piling up, and our endpoint accepts an update on that exact URL (route up,
  path secret matches prod). The update comes "from a bot", which Minimo ignores, so
  no AI runs and nobody gets a message. Secret: `TELEGRAM_MO_BOT_TOKEN`.
- **AI on Slack** (`probe_ai_slack`, every run) — no workspace has Mo installed yet,
  so this sends Slack's `url_verification` handshake, signed like Slack signs it, to
  `api.minimo.it/public/v1/webhooks/slack/events` and expects the challenge back.
  That proves the endpoint is up and prod's signing secret matches the Slack app
  "Mo". Nothing is posted. Secret: `SLACK_SIGNING_SECRET`.

Each probe leaves its row off the page until its secret is set, so the code is inert
until the workflow passes the secret in.

**Next step for Telegram and Slack:** once a Minimo-owned Telegram chat / Slack
workspace is linked to co25, these can become full round-trips like WhatsApp (send a
message, wait for Mo's reply).

## Secrets (GitHub → repo settings → Secrets)
Reused from `web-app-v2`'s existing `transactional-prod-test` action:
`MINIMO_PROD_API_KEY`, `MAILOSAUR_SERVER_ID`, `MAILOSAUR_APIKEY`,
`TRANSACTIONAL_TEST_UID_PROD`, `TELEGRAM_BOT_TOKEN`,
`TRANSACTIONAL_TEST_TELEGRAM_CHANNEL`. Optional: `WHATSAPP_TEST_RECIPIENT`
(defaults to the Andrea test number).

For **AI on every channel**: `AI_PROBE_API_KEY` (co25 key, `whatsapp` permission only),
`TELEGRAM_MO_BOT_TOKEN` (the prod `@minimoaibot` token), `SLACK_SIGNING_SECRET` (the
prod Slack app "Mo"). Keep the last two in sync with backendProd if they're rotated.

For the **dev pipeline probe** (`dev-pipeline-monitor.yaml`): `PIPELINE_SUPABASE_KEY`
(required — the **dev** Supabase `service_role` key, used read-only to assert the reply
row). Optional alerting (off by default): repo variable `PIPELINE_ALERT=true` plus
secrets `PIPELINE_TELEGRAM_BOT_TOKEN` + `PIPELINE_TELEGRAM_CHAT` (point at a **dev**
channel, never the prod ops group, until a separate GO).

## Deploy
1. Push this repo to `thebareminimo/minimo-status`.
2. Settings → Pages → **Deploy from branch** → `main` / root.
3. Add the secrets above.
4. Point `status.minimo.it` (CNAME → GitHub Pages) — repointing from the current
   incident.io page — and add a `CNAME` file / the custom-domain in Pages settings.
5. Run the workflow once (`workflow_dispatch`) to seed real data.
