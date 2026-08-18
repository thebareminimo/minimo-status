# minimo-status

Public status page for Minimo — **built in-house, hosted independently** of the
infrastructure it monitors, so it stays up (and honest) even during a full
Minimo/Dokploy outage.

## How it works

```
GitHub Actions (independent of Minimo)                     GitHub Pages (static, independent)
────────────────────────────────────────                  ─────────────────────────────────
check.py every 15 min:                                     index.html  ← fetch status.json
  • email    → send transactional → verify on Mailosaur    (auto-refresh, stale-detection)
  • whatsapp → send hello_world (every 4h) → send accepted
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
- `check.py` — the two synthetic probes + history/uptime rollup (stdlib only).
- `.github/workflows/status-monitor.yaml` — schedule + commit + Telegram.
- `index.html` — the static page (vanilla JS, light/dark, no deps).
- `status.json` — regenerated every run; the page fetches it.
- `data/history.json` — per-component daily status (90-day bars + uptime %).
- `data/incidents.json` — hand-editable incident list shown on the page.

## Probes
- **Email** — real end-to-end: send via `app.minimo.it/api/transactionals` to a
  Mailosaur inbox, confirm arrival. True delivery, not just an API 200.
- **WhatsApp** — send `hello_world` via `api.minimo.it/public/v1/templates/whatsapp/send`
  to a test number; v1 confirms the send is **accepted** (whole auth→template→dispatch
  pipeline). Confirming the Meta `delivered` webhook is a planned v1.1 upgrade.
  Runs every 4h (not every 15 min) so it doesn't buzz a real phone constantly.

## Secrets (GitHub → repo settings → Secrets)
Reused from `web-app-v2`'s existing `transactional-prod-test` action:
`MINIMO_PROD_API_KEY`, `MAILOSAUR_SERVER_ID`, `MAILOSAUR_APIKEY`,
`TRANSACTIONAL_TEST_UID_PROD`, `TELEGRAM_BOT_TOKEN`,
`TRANSACTIONAL_TEST_TELEGRAM_CHANNEL`. Optional: `WHATSAPP_TEST_RECIPIENT`
(defaults to the Andrea test number).

## Deploy
1. Push this repo to `thebareminimo/minimo-status`.
2. Settings → Pages → **Deploy from branch** → `main` / root.
3. Add the secrets above.
4. Point `status.minimo.it` (CNAME → GitHub Pages) — repointing from the current
   incident.io page — and add a `CNAME` file / the custom-domain in Pages settings.
5. Run the workflow once (`workflow_dispatch`) to seed real data.
