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

# ---------- probes ----------
def _email_attempt(api_key, server, mkey, uid, tag, poll_secs):
    """One send + verify. Returns (ok: bool, detail: str)."""
    addr = f"status-email-{tag}.{server}@mailosaur.net"
    st, body = http("POST", "https://app.minimo.it/api/transactionals",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    body=json.dumps({"recipient": addr, "uid": uid}))
    if st != 200:
        return False, f"send HTTP {st}: {body[:150]}"
    try:
        sent = json.loads(body).get("sent")
    except Exception:
        sent = None
    if sent in (False, 0):
        return False, "API returned sent:false"
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
            return True, f"delivered in Mailosaur (send {st})"
        time.sleep(10)
    return False, f"not received within {poll_secs}s"

def probe_email():
    api_key = env("MINIMO_PROD_API_KEY", required=True)
    server  = env("MAILOSAUR_SERVER_ID", required=True)
    mkey    = env("MAILOSAUR_APIKEY", required=True)
    uid     = env("TRANSACTIONAL_TEST_UID_PROD", required=True)
    run_id  = env("GITHUB_RUN_ID", str(int(time.time())))
    poll    = int(env("EMAIL_POLL_SECS", "150"))
    # First attempt with a generous window. A single slow delivery (SES/Mailosaur
    # latency > window) is a false alarm, so RETRY once before declaring down —
    # only two failures in a row flip the component red. A real outage still
    # shows within one run (~5 min).
    ok, detail = _email_attempt(api_key, server, mkey, uid, f"{run_id}-a", poll)
    if ok:
        return "operational", detail
    ok2, detail2 = _email_attempt(api_key, server, mkey, uid, f"{run_id}-b", 120)
    if ok2:
        return "operational", f"delivered on retry ({detail2}); first: {detail}"
    return "down", f"failed twice — attempt1: {detail}; attempt2: {detail2}"

def probe_whatsapp():
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

# ---------- reachability probes (Tier 1: shallow GET, no side effects) ----------
def _http_up(url, headers=None, ok=None, timeout=10, slow=5.0):
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
    return _http_up(env("API_HEALTH_URL", "https://api.minimo.it/docs"))

def probe_webapp():
    # Unauthenticated root 307-redirects to /api/auth/signin (→200): a served
    # response means the Next.js app server is up.
    return _http_up(env("WEBAPP_HEALTH_URL", "https://app.minimo.it/"))

def probe_auth():
    # Supabase GoTrue health for the prod project. Needs the (public) anon key as
    # apikey; without it we'd get a 401 and false-red, so skip cleanly instead.
    base = env("SUPABASE_URL", "https://nelourjougsxqvekwfqt.supabase.co").rstrip("/")
    key  = env("SUPABASE_ANON_KEY", "")
    if not key:
        return "operational", "skipped (SUPABASE_ANON_KEY not set)"
    return _http_up(base + "/auth/v1/health", headers={"apikey": key})

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
    gates = {"whatsapp": truthy(env("PROBE_WHATSAPP", "true"))}

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
