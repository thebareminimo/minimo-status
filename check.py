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
  - whatsapp: send the `minimo_status_check` template via Minimo's public API →
              confirm the send is accepted (200/201 + success). NOTE: this is a
              *liveness* probe (the whole auth→template→dispatch pipeline).
              Confirming the Meta `delivered` webhook status is a v1.1 upgrade.

  AI — deep end-to-end probe (real LLM reply):
  - assistant: ask a dedicated probe assistant on The Bare OÜ (company 25) a
              knowledge-base question and assert it replies with the KB canary.
              This exercises BOTH the OpenAI-backed knowledge retrieval
              (embeddings — searchKnowledge → embedQuery runs before the LLM on
              every reply) AND the language-model generation. Added after the
              2026-09-23 outage where the OpenAI account ran out of credits and
              every company's assistant (incl. Claude-based ones, which use
              OpenAI for KB embeddings) silently stopped replying, with no alert.

Data model (committed to the repo, independent of Minimo):
  data/history.json    { "<component>": { "YYYY-MM-DD": "operational|degraded|down" } }
  data/incidents.json  [ {title,date,status,impact,body}, ... ]  (hand-editable)
  status.json          the file the page fetches (regenerated every run)

No third-party deps — stdlib only.
"""
import json, os, sys, time, base64, urllib.request, urllib.error, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
HISTORY_FILE = os.path.join(DATA, "history.json")
INCIDENTS_FILE = os.path.join(DATA, "incidents.json")
STATUS_FILE = os.path.join(ROOT, "status.json")

RANK = {"operational": 0, "degraded": 1, "down": 2}
HISTORY_DAYS = 90

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
def _poll_mailosaur(server, mkey, addr, poll_secs):
    """Poll a Mailosaur inbox for a message sent to `addr`. Returns True once it
    arrives within `poll_secs`, else False. This is the GROUND TRUTH for email
    delivery — independent of what the send endpoint returned."""
    search_url = f"https://mailosaur.com/api/messages/search?server={server}"
    auth = "Basic " + base64.b64encode(f"{mkey}:".encode()).decode()
    deadline = time.time() + poll_secs
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
        time.sleep(10)
    return False

def _email_attempt(api_key, server, mkey, uid, tag, poll_secs):
    """One send + verify. Returns (ok: bool, detail: str).

    The send POST to app.minimo.it/api/transactionals intermittently takes
    >30s to RETURN even though the send actually COMPLETES server-side (the
    probe email still lands, real customer email keeps flowing). A client
    read-timeout (HTTP 0) must therefore NEVER by itself mean "down": on a
    timeout we fall back to the ground truth — did the email actually arrive in
    Mailosaur? If yes → operational (the endpoint was just slow to respond)."""
    addr = f"status-email-{tag}.{server}@mailosaur.net"
    t0 = time.time()
    # 60s (was 30s): give the endpoint more room to return before we give up on
    # the response and switch to delivery-based verification.
    st, body = http("POST", "https://app.minimo.it/api/transactionals",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    body=json.dumps({"recipient": addr, "uid": uid}), timeout=60)
    send_dt = time.time() - t0
    if st == 0:
        # Send response timed out / connection dropped — but the send usually
        # went through. Verify against Mailosaur before declaring anything down.
        if _poll_mailosaur(server, mkey, addr, poll_secs):
            return True, f"delivered despite slow send response ({send_dt:.0f}s, {body[:70]})"
        return False, f"send timed out ({send_dt:.0f}s, {body[:90]}) AND email not received within {poll_secs}s"
    # Genuine HTTP error (4xx/5xx) → real failure, keep old behavior.
    if st != 200:
        return False, f"send HTTP {st} ({send_dt:.1f}s): {body[:150]}"
    try:
        sent = json.loads(body).get("sent")
    except Exception:
        sent = None
    if sent in (False, 0):
        return False, "API returned sent:false"
    if _poll_mailosaur(server, mkey, addr, poll_secs):
        return True, f"delivered in Mailosaur (send {st} in {send_dt:.1f}s)"
    return False, f"not received within {poll_secs}s (send {st} in {send_dt:.1f}s)"

def probe_email():
    api_key = env("MINIMO_PROD_API_KEY", required=True)
    server  = env("MAILOSAUR_SERVER_ID", required=True)
    mkey    = env("MAILOSAUR_APIKEY", required=True)
    uid     = env("TRANSACTIONAL_TEST_UID_PROD", required=True)
    run_id  = env("GITHUB_RUN_ID", str(int(time.time())))
    poll    = int(env("EMAIL_POLL_SECS", "150"))
    # First attempt with a generous window. A single slow delivery (SES/Mailosaur
    # latency > window) OR a slow send RESPONSE is a false alarm, so RETRY once
    # before declaring down — only two failures in a row flip the component red.
    # A real outage still shows within one run (~5 min).
    ok, detail = _email_attempt(api_key, server, mkey, uid, f"{run_id}-a", poll)
    if ok:
        return "operational", detail
    ok2, detail2 = _email_attempt(api_key, server, mkey, uid, f"{run_id}-b", 120)
    if ok2:
        return "operational", f"delivered on retry ({detail2}); first: {detail}"
    return "down", f"failed twice — attempt1: {detail}; attempt2: {detail2}"

def _whatsapp_attempt():
    api_key   = env("MINIMO_PROD_API_KEY", required=True)
    recipient = env("WHATSAPP_TEST_RECIPIENT", "+393886543634")
    tpl_name  = env("WHATSAPP_TEMPLATE_NAME", "minimo_status_check")
    tpl_lang  = env("WHATSAPP_TEMPLATE_LANG", "en_US")
    # Comma-separated BODY params the approved template expects. The dedicated
    # health-check template `minimo_status_check` (UTILITY, en_US) takes 0 params.
    params    = [p for p in env("WHATSAPP_TEMPLATE_PARAMS", "").split(",")]
    template  = {"name": tpl_name, "languageCode": tpl_lang}
    if params and params != [""]:
        template["components"] = [{"type": "BODY",
                                   "parameters": [{"type": "text", "text": p} for p in params]}]
    payload = {"recipient": recipient, "type": "template", "template": template}
    st, body = http("POST", "https://api.minimo.it/public/v1/templates/whatsapp/send",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    body=json.dumps(payload))
    ok = False
    try:
        j = json.loads(body)
        success = j.get("success", (j.get("data") or {}).get("success"))
        ok = st in (200, 201) and bool(success)
    except Exception:
        ok = False
    return ("operational", f"send accepted (HTTP {st})") if ok else ("down", f"send HTTP {st}: {body[:200]}")

def probe_whatsapp():
    # Retry-then-confirm: a single rejected/slow send (transient 5xx, blip) must
    # not flip WhatsApp red. Note: retries only happen on FAILURE — a successful
    # send returns 'operational' on attempt 1, so exactly ONE real message reaches
    # the test phone in the normal case (no extra buzz). A genuine outage (e.g.
    # 360dialog credit negative → 422 rejected, undelivered) still confirms 'down'
    # after 3 consecutive failures. Slightly longer delay since each try hits the
    # real send pipeline.
    return retry_until_ok(_whatsapp_attempt, attempts=3, delay=5.0)

def _assistant_attempt():
    """One real assistant reply via the playground endpoint. Returns (status, detail).

    POST <base>/v1/assistants/<id>/test with `x-company-id` — the reply engine
    runs exactly like production: `searchKnowledge` embeds the query via OpenAI
    (KB retrieval) and THEN the language model generates the answer, so a single
    call covers both halves that broke on 2026-09-23. Failure modes → DOWN:
      - HTTP 0 (client timeout / connection dropped) — the reply never came back.
      - HTTP non-2xx (e.g. 500 when OpenAI embeddings or the LLM throw).
      - empty / whitespace-only reply.
      - ASSISTANT_PROBE_EXPECT set but absent from the reply — the LLM answered
        but the KB canary wasn't retrieved (embeddings/retrieval degraded).
    The endpoint currently needs no auth (only `x-company-id`); we still send the
    public API key as Bearer when available so the probe keeps working if the
    route is guarded later — harmless today."""
    base    = env("ASSISTANT_PROBE_BASE", "https://api.minimo.it").rstrip("/")
    company = env("ASSISTANT_PROBE_COMPANY_ID", "25")
    aid     = env("ASSISTANT_PROBE_ID", "ac50a817-1540-4002-b82b-c7912c6b4d7e")
    question = env("ASSISTANT_PROBE_QUESTION",
                   "What is the status probe canary code? Reply with the exact code.")
    # Canary substring the reply MUST contain (proves KB retrieval worked, not
    # just that the LLM produced words). Set empty to only assert a non-empty
    # reply. Case-insensitive.
    expect  = (env("ASSISTANT_PROBE_EXPECT", "ZEBRA-STATUS-7788") or "").strip()
    timeout = int(env("ASSISTANT_PROBE_TIMEOUT", "60"))
    api_key = env("MINIMO_PROD_API_KEY", "")

    headers = {"x-company-id": str(company), "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base}/v1/assistants/{aid}/test"
    t0 = time.time()
    st, body = http("POST", url, headers=headers,
                    body=json.dumps({"message": question, "messages": []}),
                    timeout=timeout)
    dt = time.time() - t0
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

def probe_assistant():
    # Retry-then-confirm: a single slow/failed reply (transient 5xx, a redeploy
    # mid-flight, a cold model) must not flip the assistant red. Retries fire
    # only on FAILURE — a good reply returns 'operational' on attempt 1, so
    # exactly ONE LLM call (a few tenths of a cent of OpenAI) happens per run in
    # the normal case. A genuine outage (OpenAI out of credits → embeddings/LLM
    # throw → 500, or an empty reply) still confirms 'down' after 3 tries.
    return retry_until_ok(_assistant_attempt, attempts=3, delay=6.0)

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
]

def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def main():
    os.makedirs(DATA, exist_ok=True)
    history = load_json(HISTORY_FILE, {})
    incidents = load_json(INCIDENTS_FILE, [])
    prev = {c["id"]: c for c in load_json(STATUS_FILE, {}).get("components", [])}
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
    gates = {"whatsapp": truthy(env("PROBE_WHATSAPP", "true")),
             "assistant": truthy(env("PROBE_ASSISTANT", "false"))}

    comps_out, overall, failures = [], "operational", []
    for cid, name, desc, probe, category in COMPONENTS:
        if gates.get(cid, True):
            status, detail = probe()
            print(f"[{cid}] {status} — {detail}")
            if status != "operational":
                failures.append(f"{name}: {detail}")
            days = history.setdefault(cid, {})
            days[today] = worst(days.get(today, "operational"), status)  # worst-of-day
            last_check = now_iso
        else:
            status = (prev.get(cid) or {}).get("status", "unknown")
            last_check = (prev.get(cid) or {}).get("last_check", now_iso)
            print(f"[{cid}] skipped (gate off) — carrying over '{status}'")
        hist_arr, uptime = build_history_array(history.get(cid, {}))
        comps_out.append({"id": cid, "name": name, "description": desc,
                          "category": category, "status": status, "uptime_90d": uptime,
                          "last_check": last_check, "history": hist_arr})
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
            f.write(f"failed={'true' if failures else 'false'}\n")
            f.write("summary=" + " | ".join(failures).replace("\n", " ")[:300] + "\n")
    print(f"OVERALL: {overall}")
    # never fail the job on a probe-down (we still want status.json committed);
    # the Telegram step keys off the `failed` output instead.
    return 0

if __name__ == "__main__":
    sys.exit(main())
