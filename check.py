#!/usr/bin/env python3
"""
Minimo status checker — runs OUTSIDE Minimo's infra (GitHub Actions), so it stays
up when Minimo is down. It runs two synthetic probes and folds the results into
the static data the status page reads.

Probes:
  - email:    send a real transactional via Minimo → verify it arrives in a
              Mailosaur inbox (true end-to-end delivery).
  - whatsapp: send the `hello_world` template via Minimo's public API → confirm
              the send is accepted (200 + success). NOTE: this is a *liveness*
              probe (the whole auth→template→dispatch pipeline). Confirming the
              Meta `delivered` webhook status is a v1.1 upgrade.

Data model (committed to the repo, independent of Minimo):
  data/history.json    { "<component>": { "YYYY-MM-DD": "operational|degraded|down" } }
  data/incidents.json  [ {title,date,status,impact,body}, ... ]  (hand-editable)
  status.json          the file the page fetches (regenerated every run)

No third-party deps — stdlib only.
"""
import json, os, sys, time, urllib.request, urllib.error, datetime

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
def probe_email():
    api_key = env("MINIMO_PROD_API_KEY", required=True)
    server  = env("MAILOSAUR_SERVER_ID", required=True)
    mkey    = env("MAILOSAUR_APIKEY", required=True)
    uid     = env("TRANSACTIONAL_TEST_UID_PROD", required=True)
    run_id  = env("GITHUB_RUN_ID", str(int(time.time())))
    addr = f"status-email-{run_id}.{server}@mailosaur.net"

    st, body = http("POST", "https://app.minimo.it/api/transactionals",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    body=json.dumps({"recipient": addr, "uid": uid}))
    if st != 200:
        return "down", f"send HTTP {st}: {body[:200]}"

    # poll Mailosaur for arrival (up to ~90s)
    search_url = f"https://mailosaur.com/api/messages/search?server={server}"
    auth = "Basic " + __import__("base64").b64encode(f"{mkey}:".encode()).decode()
    deadline = time.time() + 90
    while time.time() < deadline:
        s, b = http("POST", search_url,
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                    body=json.dumps({"sentTo": addr}))
        try:
            items = json.loads(b).get("items", []) if s == 200 else []
        except Exception:
            items = []
        if items:
            return "operational", f"delivered in Mailosaur (send {st})"
        time.sleep(10)
    return "down", "email not received in Mailosaur within 90s"

def probe_whatsapp():
    api_key   = env("MINIMO_PROD_API_KEY", required=True)
    recipient = env("WHATSAPP_TEST_RECIPIENT", "+393886543634")
    tpl_name  = env("WHATSAPP_TEMPLATE_NAME", "finaliseaccount")
    tpl_lang  = env("WHATSAPP_TEMPLATE_LANG", "en_US")
    # Comma-separated BODY params the approved template expects (finaliseaccount = 2).
    params    = [p for p in env("WHATSAPP_TEMPLATE_PARAMS", "Minimo,status-check").split(",")]
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

COMPONENTS = [
    ("email",    "Email delivery",    "Transactional email is sent and delivered end-to-end.", probe_email),
    ("whatsapp", "WhatsApp delivery", "WhatsApp template messages are accepted and dispatched.", probe_whatsapp),
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
    for cid, name, desc, probe in COMPONENTS:
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
                          "status": status, "uptime_90d": uptime,
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
