#!/usr/bin/env python3
"""
Minimo AI-assistant PIPELINE probe — DEV/sandbox only.

WHY THIS EXISTS (the blind spot it closes)
-------------------------------------------
`check.py`'s `probe_assistant` hits `POST /v1/assistants/:id/test` SYNCHRONOUSLY.
That endpoint calls `handleSupportMessage` directly and returns the generated
text — it exercises ONLY the reply ENGINE (OpenAI KB embeddings + prompt compile
+ LLM). It NEVER enqueues onto BullMQ, never runs the worker, never persists a
`historymessage` row, never dispatches to the WhatsApp provider.

The real inbound→reply path is ASYNC and the `/test` probe is structurally blind
to it:

    360dialog inbound webhook
      → POST /public/v1/webhooks/whatsapp         (ingest, synchronous, returns 200)
      → enqueue BullMQ `whatsapp-webhook-queue`   (attempts:1, NO retry)
      → WhatsappWebhookProcessor.processChat      (worker, ~5s debounce)
      → assistant.service handleSupportMessage    (reply generation)
      → outbound-delivery-tracker.begin           (persist BOT row, status=pending)
      → whatsapp-outbound.dispatcher.dispatch     (send to provider → status sent/failed)

That async path is EXACTLY where the 2026-09-22→23 P0 lived: inbound arrived,
the assistant toggle was ON, and the bot produced ZERO replies on every prod chat
with NO error surfaced. A probe that only calls `/test` stays green through that
outage because the engine is fine — it's the plumbing that stalled.

WHAT THIS PROBE DOES
--------------------
It injects a SYNTHETIC inbound WhatsApp message (a Meta/360dialog-shaped webhook
payload) into the DEV backend and then asserts, via the dev database (PostgREST),
that the async pipeline WROTE AN ASSISTANT REPLY ROW for it within N seconds. If
no reply row appears, the pipeline has stalled and the probe fails.

It exercises: webhook ingest → queue → worker → reply generation → persist →
dispatch. That is the entire P0 failure surface. The old `/test` probe covers
none of those stages beyond generation.

SAFETY PERIMETER (hard, non-negotiable — Minimo send-safety rules)
------------------------------------------------------------------
* DEV backend only (`https://dev-api.minimo.it`), a dedicated TEST company
  (Company Test, co3), never a real paying company.
* The synthetic sender is a NON-ROUTABLE test MSISDN (`15550001111` — a US 555
  number whose `000` exchange cannot be assigned, so it can never be a real
  WhatsApp account). The pipeline's final dispatch step DOES fire a 360dialog
  API call toward that number, but it can NEVER reach a real person — the number
  does not exist. No real customer is ever messaged.
* The assertion is on REPLY GENERATION (the persisted `senderType=BOT` row), NOT
  on successful delivery. Whether the terminal provider send ends `sent` or
  `failed` is irrelevant to the probe — a reply ROW proves the pipeline ran all
  the way to dispatch, which is the thing that silently broke in the P0.

This probe is DELIBERATELY NOT a `check.py` component: `check.py` drives the
PUBLIC, prod-facing status.minimo.it page. This one targets DEV and must stay off
that page until a separate prod-rollout decision. It runs from its own workflow
(`.github/workflows/dev-pipeline-monitor.yaml`) and never writes status.json.

No third-party deps — stdlib only (same discipline as check.py).
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse, datetime


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"::error::missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def http(method, url, headers=None, body=None, timeout=30):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"__exception__ {e}"


# ---------- config (dev defaults; everything overridable via env) ----------
def _cfg():
    return {
        # Dev backend that receives the synthetic inbound webhook.
        "api_base": env("PIPELINE_API_BASE", "https://dev-api.minimo.it").rstrip("/"),
        # Dev Supabase PostgREST base + service_role key, used ONLY to read back the
        # persisted reply row (the assertion). Key is a secret (dev project).
        "rest_base": env("PIPELINE_REST_BASE",
                         "https://tphinerwkuwxtpercslk.supabase.co/rest/v1").rstrip("/"),
        "sb_key": env("PIPELINE_SUPABASE_KEY", required=True),
        # Dedicated DEV test company + its minimo_ai WhatsApp channel (co3 / ch5):
        #   responder=minimo_ai, agent bound, provider 360dialog, business number below.
        "company": env("PIPELINE_COMPANY", "3"),
        # The channel's business number — echoed as metadata.display_phone_number so
        # the backend resolves the inbound to co3/ch5 (authoritative routing key).
        "business": env("PIPELINE_BUSINESS_NUMBER", "15558971345"),
        # The channel's channel_id — used as phone_number_id so the fallback
        # resolver (findByChannelId) also matches.
        "channel_id": env("PIPELINE_CHANNEL_ID", "klm3OTCH"),
        # NON-ROUTABLE synthetic sender. 555-000-xxxx can never be a real number,
        # so the terminal send cannot reach a human. The persisted reply row carries
        # this as its recipient `phone`, which is what we assert on.
        "sender": env("PIPELINE_SENDER", "15550001111"),
        # How long to wait for the async reply to land (debounce ~5s + queue + LLM).
        "timeout": int(env("PIPELINE_TIMEOUT", "120")),
        "poll_interval": float(env("PIPELINE_POLL_INTERVAL", "6")),
        "ingest_timeout": int(env("PIPELINE_INGEST_TIMEOUT", "30")),
        # Retry-then-confirm: a single stall could be a one-off LLM hiccup. Re-inject
        # a fresh synthetic inbound before declaring the pipeline down. A genuine
        # stall persists across attempts and still confirms. Mirrors check.py.
        "attempts": max(1, int(env("PIPELINE_ATTEMPTS", "2"))),
    }


def _now_iso(offset_secs=0):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_secs)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _inject(c, attempt):
    """POST one synthetic Meta-shaped inbound. Returns (ingest_ok, detail, wamid)."""
    stamp = f"{int(time.time())}-{attempt}"
    wamid = f"wamid.MINIMOPIPELINEPROBE{stamp}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "MINIMO_PIPELINE_PROBE",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": c["business"],
                                 "phone_number_id": c["channel_id"]},
                    "contacts": [{"profile": {"name": "Minimo Pipeline Probe"},
                                  "wa_id": c["sender"]}],
                    "messages": [{
                        "from": c["sender"],
                        "id": wamid,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": "Ciao, messaggio automatico di monitoraggio "
                                         "della pipeline. Rispondi con un breve saluto."},
                    }],
                },
            }],
        }],
    }
    st, body = http("POST", f"{c['api_base']}/public/v1/webhooks/whatsapp",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload), timeout=c["ingest_timeout"])
    # The ingest endpoint returns 200 {received:true} even when it DROPS the message
    # (e.g. no channel match), so a 200 is necessary but not sufficient — the reply
    # assertion below is the real signal. Only a non-200 means ingest itself is down.
    if st != 200:
        return False, f"inbound webhook HTTP {st}: {body[:160]}", wamid
    return True, f"ingest 200 ({body[:60]})", wamid


def _poll_reply(c, since_iso):
    """Poll dev PostgREST for a BOT reply row to the test number after `since_iso`.
    Returns the row dict once it appears within the window, else None."""
    headers = {"apikey": c["sb_key"], "Authorization": f"Bearer {c['sb_key']}"}
    phone = urllib.parse.quote("+" + c["sender"])          # stored as +<digits>
    since = urllib.parse.quote(since_iso)
    q = (f"{c['rest_base']}/historymessage"
         f"?select=id,status,senderType,isIncoming,contentText,timestamp,phone,messageId"
         f"&company=eq.{c['company']}&senderType=eq.BOT&isIncoming=eq.false"
         f"&phone=eq.{phone}&timestamp=gt.{since}&order=timestamp.desc&limit=3")
    deadline = time.time() + c["timeout"]
    while time.time() < deadline:
        time.sleep(min(c["poll_interval"], max(1.0, deadline - time.time())))
        st, body = http("GET", q, headers=headers)
        try:
            rows = json.loads(body) if st == 200 else []
        except Exception:
            rows = []
        if rows:
            return rows[0]
        if st not in (200,):
            # surface a REST error immediately rather than silently polling to timeout
            print(f"  [assert] PostgREST HTTP {st}: {body[:120]}")
    return None


def _attempt(c, attempt):
    """One synthetic inbound + reply assertion. Returns (status, detail)."""
    since = _now_iso(offset_secs=-3)                       # catch replies from now on
    ok, detail, wamid = _inject(c, attempt)
    if not ok:
        return "down", detail                             # ingest itself is broken
    t0 = time.time()
    row = _poll_reply(c, since)
    dt = time.time() - t0
    if row:
        snippet = (row.get("contentText") or "")[:60].replace("\n", " ")
        return "operational", (f"assistant reply persisted in {dt:.0f}s "
                               f"(row {row.get('id')}, send-status={row.get('status')}): \"{snippet}\"")
    return "down", (f"NO assistant reply within {c['timeout']}s of a synthetic inbound "
                    f"(ingest OK, wamid {wamid}) — async pipeline stalled "
                    f"(webhook→queue→worker→reply-gen→dispatch)")


def probe_pipeline(c=None):
    c = c or _cfg()
    last = None
    for i in range(1, c["attempts"] + 1):
        status, detail = _attempt(c, i)
        if status == "operational":
            if i > 1:
                detail += f" (recovered on attempt {i}/{c['attempts']})"
            return status, detail
        last = (status, detail)
        print(f"  [pipeline] attempt {i}/{c['attempts']} → {status}: {detail}")
        if i < c["attempts"]:
            time.sleep(3)
    status, detail = last
    return status, f"{detail} — confirmed across {c['attempts']} attempts"


def _maybe_alert(status, detail):
    """Optional Telegram alert, OFF by default (prod-gating: this dev probe must not
    page the prod ops group until a separate GO). Enable by setting PIPELINE_ALERT=true
    and providing a bot token + chat id. Until then a confirmed stall simply fails the
    GitHub job (native failure notification to repo admins)."""
    if status == "operational" or not truthy(env("PIPELINE_ALERT", "false")):
        return
    token = env("PIPELINE_TELEGRAM_BOT_TOKEN", "")
    chat = env("PIPELINE_TELEGRAM_CHAT", "")
    if not (token and chat):
        return
    run = os.environ.get("GITHUB_SERVER_URL", "") and (
        f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY','')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID','')}")
    msg = f"🟠 [DEV] Minimo AI pipeline probe: {status} — {detail}"
    if run:
        msg += f"  Run: {run}"
    http("POST", f"https://api.telegram.org/bot{token}/sendMessage",
         headers={"Content-Type": "application/x-www-form-urlencoded"},
         body=urllib.parse.urlencode({"chat_id": chat, "text": msg}))


def main():
    c = _cfg()
    print(f"[pipeline-probe] DEV target={c['api_base']} company={c['company']} "
          f"channel_biz={c['business']} sender={c['sender']} timeout={c['timeout']}s")
    status, detail = probe_pipeline(c)
    print(f"[pipeline] {status} — {detail}")
    _maybe_alert(status, detail)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"status={status}\n")
            f.write("detail=" + detail.replace("\n", " ")[:300] + "\n")
    # Unlike check.py (which never fails its job so it can always commit status.json),
    # THIS probe writes nothing to the public page — so a confirmed stall should FAIL
    # the job, which is the dev alert channel (GitHub notifies repo admins).
    return 0 if status == "operational" else 1


if __name__ == "__main__":
    sys.exit(main())
