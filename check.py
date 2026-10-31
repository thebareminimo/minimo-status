#!/usr/bin/env python3
"""
Minimo status checker — runs OUTSIDE Minimo's infra (GitHub Actions), so it stays
up when Minimo is down. It runs synthetic probes and folds the results into
the static data the status page reads.

Probes are grouped into two tiers:

  Core services — shallow reachability GETs (cheap, no side effects, every run):
  - api:      api.minimo.it/docs responds 2xx/3xx.
  - webapp:   app.minimo.it serves (root redirects to signin → still "up").
  - auth:     Supabase GoTrue /auth/v1/health for the prod project.

  Delivery — deep end-to-end probes (real messages):
  - email:    send a real transactional via Minimo → verify it arrives in a
              Mailosaur inbox (true end-to-end delivery).
  - whatsapp: send the APPROVED UTILITY template `minimo_status_check` through
              Minimo's public API (co25 key), pinned with `sender` to Minimo's own
              Meta-direct number (+34 656 48 88 97, ch30), and confirm Minimo
              accepts it. Measures the platform on a number Minimo owns, not a
              customer's number (changed 2026-10-31).
              The template must exist + be approved on the active account
              (see _whatsapp_attempt). Tradeoff: this bypasses Minimo's own
              send pipeline, so a Minimo-internal routing break won't red this
              probe — the api/webapp/auth probes cover Minimo infra, and the
              pipeline routing-leak itself is a separate backend fix.

  AI — deep end-to-end probe (real LLM reply):
  - assistant: ask a dedicated probe assistant on The Bare OÜ (company 25) a
              knowledge-base question and assert it replies with the KB canary.
              This exercises BOTH the OpenAI-backed knowledge retrieval
              (embeddings — searchKnowledge → embedQuery runs before the LLM on
              every reply) AND the language-model generation. Added after the
              2026-09-23 outage where the OpenAI account ran out of credits and
              every company's assistant (incl. Claude-based ones, which use
              OpenAI for KB embeddings) silently stopped replying, with no alert.

  AI on every channel — where people actually talk to the AI:
  - ai_whatsapp: synthetic inbound to Minimo's own AI number (co25 ch30) → wait
              for the AI reply via the public API. Full prod pipeline, hourly.
  - ai_telegram: @minimoaibot token + webhook health + our endpoint accepts updates.
  - ai_slack: signed Slack url_verification handshake echoed by the prod endpoint.
  Each is left off the page until its secret is set.

Data model (committed to the repo, independent of Minimo):
  data/history.json    { "<component>": { "YYYY-MM-DD": "operational|degraded|down" } }
  data/incidents.json  [ {title,date,status,impact,body}, ... ]  (hand-editable)
  status.json          the file the page fetches (regenerated every run)

No third-party deps — stdlib only.
"""
import json, os, sys, time, base64, hmac, hashlib, urllib.request, urllib.error, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
HISTORY_FILE = os.path.join(DATA, "history.json")
INCIDENTS_FILE = os.path.join(DATA, "incidents.json")
STATUS_FILE = os.path.join(ROOT, "status.json")

RANK = {"operational": 0, "degraded": 1, "down": 2}
HISTORY_DAYS = 90

# Debounced components: don't flip the component to `down` (and don't alert) on
# the FIRST failed probe — only after N consecutive failed probes. Rationale: the
# WhatsApp probe sends a PAID template through a third-party (BeautyGo) 360dialog
# account, so a single run can fail for the CUSTOMER's prepaid-credit balance
# (`422 ... "lack of payment"`) rather than a Minimo-side outage — and that has
# flapped the status page 3x this month. A genuine Minimo-pipeline outage persists
# across runs and still trips the threshold -> red + alert (NOT a no-op-always-green;
# the fast api/webapp/auth probes still catch an infra outage within ~15 min). Until
# a Minimo-owned, billing-stable WABA exists (the Twilio migration is not send-ready),
# this debounce is the agreed mitigation. Threshold is env-overridable.
DEBOUNCE = {"whatsapp": int(os.environ.get("WHATSAPP_FAIL_THRESHOLD", "3") or "3"),
            # AI on WhatsApp runs the real LLM: one slow reply mustn't flip the
            # public row, so it needs 2 failed runs in a row (~2h at the hourly gate).
            "ai_whatsapp": int(os.environ.get("AI_WHATSAPP_FAIL_THRESHOLD", "2") or "2")}

def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"::error::missing env {name}", file=sys.stderr); sys.exit(2)
    return v

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

def retry_until_ok(attempt_fn, attempts=3, delay=4.0):
    """Retry-then-confirm wrapper shared by every single-sample probe.

    A single failed sample is almost never a real outage: a read-timeout, a
    response slower than the `slow` threshold, a redeploy mid-flight, or a
    network blip runner->prod all produce one bad reading. So before declaring a
    component 'down'/'degraded', re-run its probe up to `attempts` times, a few
    seconds apart. The FIRST 'operational' sample wins immediately (the earlier
    reading was a blip). Only if ALL attempts come back non-operational do we
    report a problem, surfacing the LAST (freshest) observation and how many
    tries it took. This is the same philosophy the email probe already uses
    (send + retry once, Mailosaur as ground truth) applied to the shallow probes,
    so one bad sample never flips a component red.

    `attempt_fn` is a zero-arg callable returning (status, detail)."""
    last = None
    for i in range(1, attempts + 1):
        status, detail = attempt_fn()
        if status == "operational":
            if i == 1:
                return status, detail
            return status, f"{detail} (recovered on attempt {i}/{attempts})"
        last = (status, detail)
        if i < attempts:
            time.sleep(delay)
    status, detail = last
    return status, f"{status} on all {attempts} attempts — last: {detail}"

# ---------- probes ----------
def _poll_mailosaur(server, mkey, addr, poll_secs, interval_start=8.0, interval_max=15.0):
    """Poll a Mailosaur inbox for a message sent to `addr`. Returns True once it
    arrives within `poll_secs`, else False. This is the GROUND TRUTH for email
    delivery — independent of what the send endpoint returned, and of how long it
    took: a send that lands after a slow SES/Mailosaur hop is still a success.

    Polls with a gentle backoff (interval_start -> interval_max seconds) so a long
    window costs few requests, and never sleeps past the deadline."""
    search_url = f"https://mailosaur.com/api/messages/search?server={server}"
    auth = "Basic " + base64.b64encode(f"{mkey}:".encode()).decode()
    deadline = time.time() + poll_secs
    interval = interval_start
    while time.time() < deadline:
        s, b = http("POST", search_url,
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                    body=json.dumps({"sentTo": addr}))
        try:
            items = json.loads(b).get("items", []) if s == 200 else []
        except Exception:
            items = []
        if items:
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, max(1.0, remaining)))
        interval = min(interval_max, interval + 2.0)
    return False

def _email_attempt(api_key, server, mkey, uid, tag, poll_secs, send_timeout):
    """One send + verify. Returns (ok: bool, detail: str).

    Mailosaur is the GROUND TRUTH for this probe. The send POST to
    app.minimo.it/api/transactionals can take a long time to RETURN — or even
    read-time-out (HTTP 0) or 5xx at a gateway — while the send actually
    COMPLETED server-side and the probe email lands (real customer email keeps
    flowing throughout). So the send response is NEVER allowed on its own to
    declare the component down: whatever the POST returns, we poll Mailosaur over
    a long window and, if the canary email physically ARRIVES, the component is
    UP. A failure is reported only when the email never shows up within the
    window — the one signal that actually means "email didn't go out"."""
    addr = f"status-email-{tag}.{server}@mailosaur.net"
    t0 = time.time()
    # send_timeout defaults to 120s (was 60s): give the endpoint plenty of room to
    # return before we stop waiting on the response — but even a timeout here is
    # not a failure on its own, delivery below is what decides.
    st, body = http("POST", "https://app.minimo.it/api/transactionals",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    body=json.dumps({"recipient": addr, "uid": uid}), timeout=send_timeout)
    send_dt = time.time() - t0
    try:
        sent = json.loads(body).get("sent") if st == 200 else None
    except Exception:
        sent = None
    # Delivery is the real signal. Poll Mailosaur regardless of what the send
    # response was — slow/timed-out/5xx send + email that still arrives = UP.
    if _poll_mailosaur(server, mkey, addr, poll_secs):
        return True, f"delivered in Mailosaur (send HTTP {st} in {send_dt:.0f}s)"
    # Email did NOT arrive within the long window → now explain why it failed.
    if st == 0:
        return False, f"send timed out ({send_dt:.0f}s, {body[:80]}) AND email not received within {poll_secs}s"
    if st != 200:
        return False, f"send HTTP {st} ({send_dt:.0f}s): {body[:120]} AND email not received within {poll_secs}s"
    if sent in (False, 0):
        return False, f"API returned sent:false AND email not received within {poll_secs}s"
    return False, f"not received within {poll_secs}s (send {st} in {send_dt:.0f}s)"

def probe_email():
    api_key = env("MINIMO_PROD_API_KEY", required=True)
    server  = env("MAILOSAUR_SERVER_ID", required=True)
    mkey    = env("MAILOSAUR_APIKEY", required=True)
    uid     = env("TRANSACTIONAL_TEST_UID_PROD", required=True)
    run_id  = env("GITHUB_RUN_ID", str(int(time.time())))
    # Long delivery window (was 150/120s). Mailosaur is the ground truth and a
    # slow SES/Mailosaur hop or a slow send RESPONSE is a false alarm, so we wait
    # up to ~300s for the email to actually arrive before giving up on an attempt.
    poll        = int(env("EMAIL_POLL_SECS", "300"))
    retry_poll  = int(env("EMAIL_RETRY_POLL_SECS", "240"))
    send_to     = int(env("EMAIL_SEND_TIMEOUT", "120"))   # send-response timeout (was 60)
    attempts    = max(1, int(env("EMAIL_ATTEMPTS", "2"))) # 2-3 full send+verify cycles
    delay       = float(env("EMAIL_RETRY_DELAY", "10"))
    # Retry-then-confirm, delivery-based — same philosophy as the shallow probes'
    # retry_until_ok, but the confirmation here is "did the canary email ARRIVE?".
    # Each attempt sends a fresh probe email and waits a long window for it. We only
    # flip the component red when the email fails to arrive across EVERY attempt; a
    # single slow/timed-out send never marks email down. A genuine outage (email
    # truly not going out) still confirms within one run.
    details = []
    for i in range(1, attempts + 1):
        window = poll if i == 1 else retry_poll
        ok, detail = _email_attempt(api_key, server, mkey, uid, f"{run_id}-{i}", window, send_to)
        if ok:
            if i == 1:
                return "operational", detail
            return "operational", f"delivered on attempt {i}/{attempts} ({detail})"
        details.append(f"attempt{i}: {detail}")
        if i < attempts:
            time.sleep(delay)
    return "down", f"email not delivered across {attempts} attempts — " + "; ".join(details)

def _whatsapp_attempt():
    # Send through MINIMO ITSELF (2026-10-31): POST /public/v1/templates/whatsapp/send
    # with a co25 (Minimo / The Bare OÜ) key, pinned with `sender` to Minimo's own
    # Meta-direct number (+34 656 48 88 97, ch30, phone_number_id 1434458863076380,
    # WABA 1819617479061624). This measures the PLATFORM (API key auth -> template
    # resolution -> channel -> Meta Cloud API) on a number Minimo owns and pays for,
    # instead of a customer's number: the old probe sent directly with BeautyGo's
    # 360dialog key, so BeautyGo's own migration (credit line, routing) turned the
    # page red on 30-31/10 while every other company kept sending.
    # `sender` makes the backend THROW WHATSAPP_SENDER_NOT_FOUND instead of falling
    # back to another number, so the probe can never land on a different channel.
    # Key: WHATSAPP_PROBE_API_KEY if set, else AI_PROBE_API_KEY (co25 key with the
    # whatsapp permission, already used by the AI-on-WhatsApp probe).
    api_key   = env("WHATSAPP_PROBE_API_KEY", "") or env("AI_PROBE_API_KEY", required=True)
    base      = env("WHATSAPP_PROBE_BASE", "https://api.minimo.it").rstrip("/")
    sender    = env("WHATSAPP_PROBE_SENDER", "1434458863076380")
    recipient = env("WHATSAPP_TEST_RECIPIENT", "+393886543634")
    # `minimo_status_check` (en_US, UTILITY, BODY-only, no params/buttons) is
    # approved on Minimo's WABA 1819617479061624 for this probe only.
    tpl_name  = env("WHATSAPP_TEMPLATE_NAME", "minimo_status_check")
    tpl_lang  = env("WHATSAPP_TEMPLATE_LANG", "en_US")
    template  = {"name": tpl_name, "languageCode": tpl_lang}
    params    = [p for p in env("WHATSAPP_TEMPLATE_PARAMS", "").split(",") if p]
    if params:
        template["components"] = [{"type": "BODY",
                                   "parameters": [{"type": "text", "text": p} for p in params]}]
    payload = {"recipient": recipient.strip().replace(" ", ""), "type": "template",
               "sender": sender, "template": template,
               "contactSource": "status-probe"}
    st, body = http("POST", f"{base}/public/v1/templates/whatsapp/send",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    body=json.dumps(payload), timeout=45)
    ok = False
    try:
        j = json.loads(body)
        # The public API wraps responses: {"data": {"success": true}}.
        ok = st in (200, 201) and (j.get("data") or j).get("success") is True
    except Exception:
        ok = False
    return ("operational", f"Minimo accepted the send (HTTP {st})") if ok else ("down", f"send HTTP {st}: {body[:200]}")

WA_PROBE_TEXT = "Minimo automated health check"

def _whatsapp_delivery_check(sent_after, api_key, base, recipient):
    """After Minimo accepted the send, confirm Meta did not reject it.

    Meta rejects some sends asynchronously (e.g. 131042 payment issue on
    2026-10-31): the public API answers 201 and the failure only lands a few
    seconds later as a status webhook, which flips the stored message to
    `failed`. The backend stores only sent/failed (no delivered), so the check
    is: find the probe message in the recipient's conversation through the
    public read API and make sure it does not turn `failed` within the window.
    Never sends anything. Returns (status, detail)."""
    window = int(env("WHATSAPP_DELIVERY_WINDOW", "45"))
    hdrs = {"Authorization": f"Bearer {api_key}"}
    digits = "".join(ch for ch in recipient if ch.isdigit())
    deadline = time.time() + window
    chat_id, msg, last_err = None, None, ""
    while True:
        if chat_id is None:
            st, body = http("GET", f"{base}/public/v1/whatsapp/conversations?page=1&pageSize=20",
                            headers=hdrs, timeout=20)
            if st == 200:
                try:
                    j = json.loads(body)
                    items = (j.get("data") or {}).get("data") if isinstance(j.get("data"), dict) else j.get("data")
                    for c in items or []:
                        ph = "".join(ch for ch in ((c.get("contact") or {}).get("phone") or "") if ch.isdigit())
                        if ph and ph == digits:
                            chat_id = c.get("chatId")
                            break
                except Exception as e:
                    last_err = f"conversations parse error: {e}"
            else:
                last_err = f"conversations HTTP {st}: {body[:120]}"
        if chat_id is not None:
            st, body = http("GET", f"{base}/public/v1/whatsapp/conversations/{chat_id}/messages?page=1&pageSize=10",
                            headers=hdrs, timeout=20)
            if st == 200:
                try:
                    j = json.loads(body)
                    items = (j.get("data") or {}).get("data") if isinstance(j.get("data"), dict) else j.get("data")
                    msg = next((m for m in items or []
                                if m.get("direction") == "outbound"
                                and (m.get("text") or "").startswith(WA_PROBE_TEXT)
                                and (m.get("timestamp") or 0) >= sent_after), None)
                except Exception as e:
                    last_err = f"messages parse error: {e}"
            else:
                last_err = f"messages HTTP {st}: {body[:120]}"
            if msg and msg.get("status") == "failed":
                return "down", f"Minimo accepted the send but Meta rejected it (message {msg.get('id')} status failed)"
        if time.time() >= deadline:
            break
        time.sleep(5)
    if msg:
        return "operational", f"sent, no rejection within {window}s (message {msg.get('id')} status {msg.get('status')})"
    # The read side is not the thing under test: if it can't find the message,
    # say so loudly in the detail but don't page on it.
    return "operational", f"sent; delivery NOT verified ({last_err or 'probe message not found'})"

def probe_whatsapp():
    # Retry-then-confirm on the SEND only: a rejected/slow API call (transient
    # 5xx, blip) is retried up to 3x; a successful send returns on attempt 1, so
    # exactly ONE real message goes out in the normal case. Once Minimo accepts
    # it, the delivery check runs ONCE (no re-send): if Meta rejects the message
    # asynchronously, the component is down.
    sent_after = int(time.time()) - 5
    status, detail = retry_until_ok(_whatsapp_attempt, attempts=3, delay=5.0)
    if status != "operational":
        return status, detail
    api_key = env("WHATSAPP_PROBE_API_KEY", "") or env("AI_PROBE_API_KEY", required=True)
    base = env("WHATSAPP_PROBE_BASE", "https://api.minimo.it").rstrip("/")
    recipient = env("WHATSAPP_TEST_RECIPIENT", "+393886543634")
    d_status, d_detail = _whatsapp_delivery_check(sent_after, api_key, base, recipient)
    return d_status, f"{detail}; {d_detail}"

def _assistant_attempt():
    """One real assistant reply. Returns (status, detail).

    Tries the API-key route first (POST <base>/public/v1/assistants/<id>/test,
    Authorization: Bearer <key>), with a fallback to the dashboard route
    (POST <base>/v1/assistants/<id>/test with `x-company-id`) while the
    API-key route is not deployed yet — i.e. only when it answers HTTP 404.
    Either way, the reply engine runs exactly like production: `searchKnowledge`
    embeds the query via OpenAI (KB retrieval) and THEN the language model
    generates the answer, so a single call covers both halves that broke on
    2026-09-23. Failure modes → DOWN:
      - HTTP 0 (client timeout / connection dropped) — the reply never came back.
      - HTTP non-2xx (e.g. 500 when OpenAI embeddings or the LLM throw).
      - empty / whitespace-only reply.
      - ASSISTANT_PROBE_EXPECT set but absent from the reply — the LLM answered
        but the KB canary wasn't retrieved (embeddings/retrieval degraded).
    Only a 404 from the API-key route triggers the fallback: any other status
    (200/201, 401, 403, 5xx, a timeout) is the final answer for this run."""
    base    = env("ASSISTANT_PROBE_BASE", "https://api.minimo.it").rstrip("/")
    aid     = env("ASSISTANT_PROBE_ID", "ac50a817-1540-4002-b82b-c7912c6b4d7e")
    question = env("ASSISTANT_PROBE_QUESTION",
                   "What is the status probe canary code? Reply with the exact code.")
    # Canary substring the reply MUST contain (proves KB retrieval worked, not
    # just that the LLM produced words). Set empty to only assert a non-empty
    # reply. Case-insensitive.
    expect  = (env("ASSISTANT_PROBE_EXPECT", "ZEBRA-STATUS-7788") or "").strip()
    timeout = int(env("ASSISTANT_PROBE_TIMEOUT", "60"))
    api_key = env("MINIMO_PROD_API_KEY", "")

    def evaluate(st, body, dt):
        if st == 0:
            return "down", f"no reply — request failed in {dt:.0f}s: {body[:120]}"
        if st not in (200, 201):
            return "down", f"reply HTTP {st} in {dt:.1f}s: {body[:180]}"
        try:
            data = json.loads(body)
            reply = ((data.get("data") or data).get("response") or "")
        except Exception:
            return "down", f"unparseable reply (HTTP {st}, {dt:.1f}s): {body[:150]}"
        if not reply.strip():
            return "down", f"empty reply (HTTP {st} in {dt:.1f}s)"
        if expect and expect.lower() not in reply.lower():
            # LLM answered but the KB canary was not retrieved — the OpenAI-backed
            # embedding/retrieval path is degraded even though generation works.
            return "down", (f"KB canary '{expect}' missing from reply "
                            f"(HTTP {st} in {dt:.1f}s): {reply[:120]}")
        return "operational", f"replied with canary in {dt:.1f}s (HTTP {st})"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base}/public/v1/assistants/{aid}/test"
    t0 = time.time()
    st, body = http("POST", url, headers=headers,
                    body=json.dumps({"message": question}),
                    timeout=timeout)
    dt = time.time() - t0

    if st == 404:
        # Not deployed yet on this environment — fall back to the current
        # dashboard route exactly as it works today.
        company = env("ASSISTANT_PROBE_COMPANY_ID", "25")
        fb_headers = {"x-company-id": str(company), "Content-Type": "application/json"}
        if api_key:
            fb_headers["Authorization"] = f"Bearer {api_key}"
        fb_url = f"{base}/v1/assistants/{aid}/test"
        t0 = time.time()
        st, body = http("POST", fb_url, headers=fb_headers,
                        body=json.dumps({"message": question, "messages": []}),
                        timeout=timeout)
        dt = time.time() - t0

    return evaluate(st, body, dt)

def probe_assistant():
    # Retry-then-confirm: a single slow/failed reply (transient 5xx, a redeploy
    # mid-flight, a cold model) must not flip the assistant red. Retries fire
    # only on FAILURE — a good reply returns 'operational' on attempt 1, so
    # exactly ONE LLM call (a few tenths of a cent of OpenAI) happens per run in
    # the normal case. A genuine outage (OpenAI out of credits → embeddings/LLM
    # throw → 500, or an empty reply) still confirms 'down' after 3 tries.
    return retry_until_ok(_assistant_attempt, attempts=3, delay=6.0)

# ---------- AI on every channel ----------
# `assistant` above calls the reply engine synchronously, so it can't see the
# channel plumbing in front of it. These probes check that the AI is reachable
# where people actually talk to it.
#
# A probe returns ("skip", reason) when it isn't configured (its secret isn't
# set). A skipped component is left off the page entirely, so merging this code
# changes nothing until each channel is wired up.

def _digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())

# Chat id of the probe conversation, cached across attempts within one run.
_AI_WA_CHAT = {}

def _api_list(body):
    """Items of a Minimo public list response. The API wraps payloads as
    {"data": {"data": [...], "pagination": {...}}}; tolerate the bare shape too."""
    d = json.loads(body)
    while isinstance(d, dict) and "data" in d:
        d = d["data"]
    return d if isinstance(d, list) else []

def _ai_wa_find_chat(base, headers, sender):
    """Return (chat_id | None, http_status) for the probe sender's conversation.
    Right after an inject the probe conversation is the most recently active
    one, so the first page is enough."""
    if sender in _AI_WA_CHAT:
        return _AI_WA_CHAT[sender], 200
    st, body = http("GET", f"{base}/public/v1/whatsapp/conversations?page=1&pageSize=50",
                    headers=headers, timeout=20)
    if st != 200:
        return None, st
    try:
        convs = _api_list(body)
    except Exception:
        return None, st
    for conv in convs:
        if _digits((conv.get("contact") or {}).get("phone")) == sender:
            _AI_WA_CHAT[sender] = conv.get("chatId")
            return conv.get("chatId"), 200
    return None, 200

def _ai_whatsapp_attempt(attempt):
    """Inject one synthetic inbound WhatsApp message into prod and wait for the
    AI reply. Returns (status, detail).

    Target: Minimo's own AI number on Meta-direct (The Bare OÜ, company 25,
    channel 30, responder minimo_ai). It runs the same path as every
    customer's AI support bot:
        POST /public/v1/webhooks/whatsapp → ingest + channel routing
        → whatsapp-webhook-queue → worker → assistant reply → outbound dispatch
    That async path is where the 2026-09-22/23 outage lived (inbound arrived,
    zero replies, no error), which the synchronous `assistant` probe can't see.

    Safety: the sender is a non-routable test number (555-000-xxxx), so the
    reply's final send can never reach a person. Meta rejects it and the row
    ends `failed`, which is expected. The check is that the AI reply was
    generated and handed to dispatch, not that it was delivered.

    Read-back uses Minimo's public WhatsApp conversations API with a key that
    only has the `whatsapp` permission (AI_PROBE_API_KEY). No database access."""
    base     = env("AI_WA_API_BASE", "https://api.minimo.it").rstrip("/")
    key      = env("AI_PROBE_API_KEY", "")
    business = _digits(env("AI_WA_BUSINESS_NUMBER", "34656488897"))
    pnid     = env("AI_WA_PHONE_NUMBER_ID", "1434458863076380")
    sender   = _digits(env("AI_WA_SENDER", "15550001111"))
    timeout  = int(env("AI_WA_TIMEOUT", "150"))
    if not key:
        return "skip", "AI_PROBE_API_KEY not set"
    headers = {"Authorization": f"Bearer {key}"}

    run_id = env("GITHUB_RUN_ID", str(int(time.time())))
    wamid  = f"wamid.MINIMOSTATUSPROBE{run_id}x{attempt}x{int(time.time())}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "MINIMO_STATUS_PROBE",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": business,
                                 "phone_number_id": pnid},
                    "contacts": [{"profile": {"name": "Minimo Status Probe"},
                                  "wa_id": sender}],
                    "messages": [{
                        "from": sender, "id": wamid,
                        "timestamp": str(int(time.time())), "type": "text",
                        "text": {"body": "Ciao! Questo è un messaggio automatico di "
                                         "controllo di status.minimo.it. Rispondi "
                                         "con un breve saluto."},
                    }],
                },
            }],
        }],
    }
    t0 = time.time()
    st, body = http("POST", f"{base}/public/v1/webhooks/whatsapp",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload), timeout=30)
    # 200 is necessary but not sufficient: ingest also answers 200 when it drops
    # a message (e.g. no channel matched). The read-back below is the real check.
    if st != 200:
        return "down", f"inbound webhook HTTP {st}: {body[:140]}"

    inbound_id, deadline = None, t0 + timeout
    while time.time() < deadline:
        time.sleep(6)
        chat_id, cst = _ai_wa_find_chat(base, headers, sender)
        if cst in (401, 403):
            return "skip", f"AI_PROBE_API_KEY rejected by the conversations API (HTTP {cst})"
        if not chat_id:
            continue
        mst, mbody = http("GET", f"{base}/public/v1/whatsapp/conversations/"
                                 f"{chat_id}/messages?page=1&pageSize=10",
                          headers=headers, timeout=20)
        if mst != 200:
            continue
        try:
            msgs = _api_list(mbody)
        except Exception:
            continue
        for m in msgs:
            if m.get("wamid") == wamid:
                inbound_id = m.get("id")
        if inbound_id is None:
            continue
        replies = [m for m in msgs
                   if m.get("direction") == "outbound" and (m.get("id") or 0) > inbound_id
                   and (m.get("text") or "").strip()]
        if replies:
            r = min(replies, key=lambda m: m.get("id") or 0)
            snippet = (r.get("text") or "")[:60].replace("\n", " ")
            return "operational", (f"AI replied in {time.time() - t0:.0f}s "
                                   f"(send status {r.get('status')}): \"{snippet}\"")
    if inbound_id is None:
        return "down", (f"synthetic inbound not stored within {timeout}s "
                        f"(webhook answered 200 but the message never reached the inbox)")
    return "down", (f"inbound stored but NO AI reply within {timeout}s "
                    f"(queue / worker / AI stalled)")

def probe_ai_whatsapp():
    # Each attempt injects a fresh message, so a retry also re-runs the AI once.
    # 2 attempts inside a run, plus the cross-run DEBOUNCE, keep a single slow
    # LLM call from flipping the public row.
    last = None
    for i in (1, 2):
        status, detail = _ai_whatsapp_attempt(i)
        if status in ("operational", "skip"):
            if i > 1 and status == "operational":
                detail += " (recovered on attempt 2/2)"
            return status, detail
        last = (status, detail)
        if i == 1:
            time.sleep(5)
    return last[0], f"{last[0]} on both attempts — last: {last[1]}"

def _ai_telegram_attempt():
    """Telegram → Minimo's AI bot. Returns (status, detail).

    No company of ours has a Telegram chat linked to Mo, so this can't make Mo
    answer a real message. It checks the connection instead, which is what
    breaks after a deploy or an env change:
      1. the bot token works (getMe);
      2. Telegram's webhook points at api.minimo.it, and Telegram has no fresh
         delivery error with updates piling up (getWebhookInfo);
      3. our endpoint accepts an update on that exact URL, which proves the
         route is up and the path secret matches TELEGRAM_WEBHOOK_SECRET in
         prod. The update comes "from a bot", which the service ignores, so
         nothing runs and nobody gets a message."""
    token = env("TELEGRAM_MO_BOT_TOKEN", "")
    if not token:
        return "skip", "TELEGRAM_MO_BOT_TOKEN not set"
    expect = env("AI_TELEGRAM_WEBHOOK_BASE",
                 "https://api.minimo.it/public/v1/webhooks/telegram/")
    tg = f"https://api.telegram.org/bot{token}"

    st, body = http("GET", f"{tg}/getMe", timeout=15)
    if st != 200:
        return "down", f"bot token rejected by Telegram (getMe HTTP {st})"
    username = (json.loads(body).get("result") or {}).get("username", "?")

    st, body = http("GET", f"{tg}/getWebhookInfo", timeout=15)
    if st != 200:
        return "down", f"getWebhookInfo HTTP {st}"
    info = json.loads(body).get("result") or {}
    url = info.get("url") or ""
    if not url.startswith(expect):
        where = url.split("/telegram/")[0] if url else "nowhere"
        return "down", f"@{username} webhook points to {where}, not Minimo"
    pending = int(info.get("pending_update_count") or 0)
    err_age = time.time() - float(info.get("last_error_date") or 0)
    if err_age < 1200 and pending > 0:
        return "down", (f"Telegram can't deliver to Minimo: {pending} updates pending, "
                        f"last error {err_age / 60:.0f} min ago: "
                        f"{(info.get('last_error_message') or '')[:100]}")

    ping = {"update_id": 0, "message": {
        "message_id": 0, "date": int(time.time()),
        "chat": {"id": 0, "type": "private"},
        "from": {"id": 0, "is_bot": True, "first_name": "status.minimo.it"},
        "text": "status probe"}}
    t = time.time()
    st, body = http("POST", url, headers={"Content-Type": "application/json"},
                    body=json.dumps(ping), timeout=20)
    dt = time.time() - t
    if st == 403:
        return "down", "Minimo rejected Telegram's webhook secret (403): secret drifted"
    if st != 200:
        return "down", f"Minimo's Telegram webhook answered HTTP {st} in {dt:.1f}s: {body[:100]}"
    if pending > 50:
        return "degraded", f"@{username} connected, but {pending} updates are queued at Telegram"
    return "operational", f"@{username} connected, webhook accepts updates ({dt:.1f}s)"

def probe_ai_telegram():
    return retry_until_ok(_ai_telegram_attempt, attempts=3, delay=5.0)

def _ai_slack_attempt():
    """Slack → Minimo's AI app ("Mo"). Returns (status, detail).

    No Slack workspace has Mo installed yet, so there's no conversation to test.
    It checks the connection instead: it sends Slack's own url_verification
    handshake, signed the way Slack signs it, to the prod events endpoint. A
    correct echo proves the endpoint is up and SLACK_SIGNING_SECRET in prod
    matches the Slack app. Nothing is posted anywhere."""
    secret = env("SLACK_SIGNING_SECRET", "")
    if not secret:
        return "skip", "SLACK_SIGNING_SECRET not set"
    url = env("AI_SLACK_EVENTS_URL", "https://api.minimo.it/public/v1/webhooks/slack/events")
    challenge = f"status-probe-{int(time.time())}"
    raw = json.dumps({"token": "status-probe", "challenge": challenge,
                      "type": "url_verification"})
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{raw}".encode(),
                           hashlib.sha256).hexdigest()
    t = time.time()
    st, body = http("POST", url, body=raw, timeout=20, headers={
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    dt = time.time() - t
    if st == 403:
        return "down", "Minimo rejected a correctly signed Slack event (403): signing secret drifted"
    if st != 200:
        return "down", f"Slack events endpoint HTTP {st} in {dt:.1f}s: {body[:100]}"
    if challenge not in body:
        return "down", f"Slack handshake not echoed (HTTP 200): {body[:100]}"
    return "operational", f"signed Slack handshake verified ({dt:.1f}s)"

def probe_ai_slack():
    return retry_until_ok(_ai_slack_attempt, attempts=3, delay=5.0)

# ---------- reachability probes (Tier 1: shallow GET, no side effects) ----------
def _http_up(url, headers=None, ok=None, timeout=15, slow=5.0):
    """GET a URL and classify reachability. urllib follows redirects, so a login
    redirect that lands on 200 still reads as 'up'. up→operational, slow→degraded,
    non-ok/unreachable→down."""
    ok = ok if ok is not None else set(range(200, 400))
    t = time.time()
    st, body = http("GET", url, headers=headers, timeout=timeout)
    dt = time.time() - t
    if st == 0:
        return "down", f"unreachable: {body[:120]}"
    if st not in ok:
        return "down", f"HTTP {st} in {dt:.2f}s"
    if dt > slow:
        return "degraded", f"slow — HTTP {st} in {dt:.2f}s"
    return "operational", f"HTTP {st} in {dt:.2f}s"

def probe_api():
    # /docs is a genuinely-200 liveness route (root 404s, health needs auth).
    # Retry-then-confirm so one slow/timed-out sample never flips the component.
    return retry_until_ok(lambda: _http_up(env("API_HEALTH_URL", "https://api.minimo.it/docs")))

def probe_webapp():
    # Unauthenticated root 307-redirects to /api/auth/signin (→200): a served
    # response means the Next.js app server is up.
    return retry_until_ok(lambda: _http_up(env("WEBAPP_HEALTH_URL", "https://app.minimo.it/")))

def probe_auth():
    # Supabase GoTrue health for the prod project. Needs the (public) anon key as
    # apikey; without it we'd get a 401 and false-red, so skip cleanly instead.
    base = env("SUPABASE_URL", "https://nelourjougsxqvekwfqt.supabase.co").rstrip("/")
    key  = env("SUPABASE_ANON_KEY", "")
    if not key:
        return "operational", "skipped (SUPABASE_ANON_KEY not set)"
    return retry_until_ok(lambda: _http_up(base + "/auth/v1/health", headers={"apikey": key}))

# ---------- history / rendering ----------
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def worst(a, b):
    return a if RANK.get(a, 0) >= RANK.get(b, 0) else b

def build_history_array(days_map):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    arr = []
    for i in range(HISTORY_DAYS - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        arr.append(days_map.get(d))  # None = no data (grey bar)
    known = [x for x in arr if x is not None]
    uptime = round(100.0 * sum(1 for x in known if x == "operational") / len(known), 2) if known else None
    return arr, uptime

# (id, name, description, probe, category)
COMPONENTS = [
    ("api",      "API",               "Minimo's public API responds.",                          probe_api,      "Core services"),
    ("webapp",   "Web app",           "The Minimo web app is reachable.",                       probe_webapp,   "Core services"),
    ("auth",     "Authentication",    "Login / session service (Supabase) is up.",              probe_auth,     "Core services"),
    ("email",    "Email delivery",    "Transactional email is sent and delivered end-to-end.",  probe_email,    "Delivery"),
    ("whatsapp", "WhatsApp delivery", "WhatsApp template messages are accepted and dispatched.", probe_whatsapp, "Delivery"),
    ("assistant","AI Assistant",      "The AI assistant retrieves its knowledge base and replies.", probe_assistant, "AI"),
    ("ai_whatsapp", "AI on WhatsApp", "A WhatsApp message to Minimo's AI number gets an AI reply, end to end.", probe_ai_whatsapp, "AI"),
    ("ai_telegram", "AI on Telegram", "Telegram messages reach Minimo's AI bot.",                 probe_ai_telegram, "AI"),
    ("ai_slack",    "AI on Slack",    "Slack events reach Minimo's AI app.",                      probe_ai_slack,    "AI"),
]

def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def main():
    os.makedirs(DATA, exist_ok=True)
    history = load_json(HISTORY_FILE, {})
    incidents = load_json(INCIDENTS_FILE, [])
    prev = {c["id"]: c for c in load_json(STATUS_FILE, {}).get("components", [])}
    # Consecutive-failure streaks for debounced components persist inside
    # status.json itself (the `fail_streak` field per component) — it is already
    # committed every run, so no extra file / workflow change is needed.
    streaks = {cid: (c.get("fail_streak") or 0) for cid, c in prev.items()}
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Per-probe gates: WhatsApp sends a real message to a phone, so it runs on a
    # sparser schedule (env PROBE_WHATSAPP, set by the 4h cron / manual run).
    # A skipped component carries over its previous state — no send, no buzz.
    # Both deep probes that cost money / buzz a device run on sparser schedules
    # (their own crons set PROBE_WHATSAPP / PROBE_ASSISTANT). A skipped component
    # carries over its previous state — no send, no LLM call, no buzz.
    # `assistant` defaults OFF: it costs an OpenAI call, and if this file lands
    # before the workflow's hourly gate is applied it must NOT fire on the every
    # 15-min email cron. The workflow turns it on explicitly (hourly cron /
    # manual run) via PROBE_ASSISTANT.
    # AI on WhatsApp runs the real LLM, so it shares the assistant's hourly gate.
    # AI on Telegram / Slack send nothing and cost nothing: they run every time.
    gates = {"whatsapp": truthy(env("PROBE_WHATSAPP", "true")),
             "assistant": truthy(env("PROBE_ASSISTANT", "false")),
             "ai_whatsapp": truthy(env("PROBE_ASSISTANT", "false"))}

    comps_out, overall, failures = [], "operational", []
    for cid, name, desc, probe, category in COMPONENTS:
        if gates.get(cid, True):
            status, detail = probe()
            if status == "skip":
                # Not configured: leave the component off the page entirely.
                print(f"[{cid}] not configured — {detail}")
                continue
            # Debounce: a component in DEBOUNCE only goes `down` (and alerts) after
            # N consecutive failed probes. A single transient failure (e.g. a
            # third-party prepaid-credit blip on the WhatsApp send) reports
            # `operational` with a "pending" note and does NOT alert, so the page
            # doesn't flap. A sustained outage trips the threshold and goes red.
            threshold = DEBOUNCE.get(cid)
            if threshold:
                if status == "operational":
                    streaks[cid] = 0
                else:
                    streaks[cid] = streaks.get(cid, 0) + 1
                    if streaks[cid] < threshold:
                        print(f"[{cid}] {status} — {detail} "
                              f"(debounced {streaks[cid]}/{threshold}, not alerting)")
                        detail = (f"transient failure, not yet confirmed "
                                  f"({streaks[cid]}/{threshold} consecutive): {detail}")
                        status = "operational"
            print(f"[{cid}] {status} — {detail}")
            if status != "operational":
                failures.append(f"{name}: {detail}")
            days = history.setdefault(cid, {})
            days[today] = worst(days.get(today, "operational"), status)  # worst-of-day
            last_check = now_iso
        else:
            if cid not in prev:
                # Never published yet (e.g. a new probe before its first gated run).
                continue
            status = (prev.get(cid) or {}).get("status", "unknown")
            last_check = (prev.get(cid) or {}).get("last_check", now_iso)
            print(f"[{cid}] skipped (gate off) — carrying over '{status}'")
        hist_arr, uptime = build_history_array(history.get(cid, {}))
        comp = {"id": cid, "name": name, "description": desc,
                "category": category, "status": status, "uptime_90d": uptime,
                "last_check": last_check, "history": hist_arr}
        # Persist the debounce streak on the component (carried over unchanged on a
        # skipped run, since `streaks` was seeded from the previous status.json).
        if cid in DEBOUNCE:
            comp["fail_streak"] = streaks.get(cid, 0)
        comps_out.append(comp)
        overall = worst(overall, status)

    status_doc = {"updated_at": now_iso, "overall": overall,
                  "components": comps_out, "incidents": incidents}
    with open(STATUS_FILE, "w") as f: json.dump(status_doc, f, indent=2)
    with open(HISTORY_FILE, "w") as f: json.dump(history, f, indent=2)

    # outputs for the workflow (notify only on failure)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"overall={overall}\n")
            # Only main alerts: a test run dispatched on a branch must never page.
            on_main = os.environ.get("GITHUB_REF", "refs/heads/main") == "refs/heads/main"
            f.write(f"failed={'true' if failures and on_main else 'false'}\n")
            f.write("summary=" + " | ".join(failures).replace("\n", " ")[:300] + "\n")
    print(f"OVERALL: {overall}")
    # never fail the job on a probe-down (we still want status.json committed);
    # the Telegram step keys off the `failed` output instead.
    return 0

if __name__ == "__main__":
    sys.exit(main())
