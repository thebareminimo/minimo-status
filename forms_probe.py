#!/usr/bin/env python3
"""Public forms render probe (anonymous, read-only, never submits).

Why: the Cityfy "Ottieni l'accesso" popup (old embed script) rendered nothing from
2026-06-17 to 2026-11-06 and the shareable /public-forms/<code> page said "Form not
found" from 2026-08-01, while the script URL itself kept answering 200: the widget
was simply empty. So this probe opens each public surface in a real headless
Chromium and asserts the form FIELDS are in the DOM, not just a status code.

Surfaces, for one synthetic form code (Sendflows co22, no list, no transactional):
  lookup      GET <app>/api/form/internal?code=     -> 200 with the form definition
  page        <app>/public-forms/<code>             -> the fields render
  old-inline  <app>/api/form script, inline widget  -> the fields render in its iframe
  old-popup   <app>/api/form script, popup widget   -> idem
  new-inline  <api>/v1/api/form script, inline      -> idem
  new-popup   <api>/v1/api/form script, popup       -> idem

Nothing is typed or submitted: no contact is ever created.

CLI:  python3 forms_probe.py --code <code> [--app https://app.minimo.it] [--api https://api.minimo.it]
Exit 0 = every surface renders, 1 = at least one does not.
Needs `pip install playwright && python -m playwright install --with-deps chromium`.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# Fake host for the synthetic customer page: Playwright serves it from memory
# (page.route), so the embed scripts run cross-origin, like on a customer site.
HOST_PAGE = "https://forms-probe.minimo-status.invalid/"
FIELD_SELECTOR = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea"


def _lookup(app, code, timeout):
    url = f"{app}/api/form/internal?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "minimo-status forms probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # network
        return False, f"unreachable: {e}", None
    if status != 200:
        return False, f"HTTP {status}: {body[:120]}", None
    try:
        data = json.loads(body)
        fields = [f for f in data["form"]["fields"] if f.get("isVisible", True)]
    except Exception as e:
        return False, f"HTTP 200 but unexpected body ({e}): {body[:120]}", None
    if not data.get("apiKey"):
        return False, "HTTP 200 but no apiKey (the widget could not submit)", None
    return True, f"HTTP 200, {len(fields)} visible fields", [f["name"] for f in fields]


def _host_html(script_url, code, mode):
    widget = f'<div id="minimo-inline-widget" data-code="{code}"></div>' if mode == "inline" else ""
    return (f"<!doctype html><html><head><title>forms probe</title></head><body>"
            f"<h1>Minimo forms probe</h1>{widget}<script src=\"{script_url}\"></script></body></html>")


def _fields_in_widget_iframe(page, timeout_ms):
    """Field count inside the widget iframe once #minimo-form shows up (0 if it never does)."""
    for _ in range(max(1, int(timeout_ms / 500))):
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                if fr.query_selector("#minimo-form"):
                    page.wait_for_timeout(300)  # let the field loop finish
                    return len(fr.query_selector_all(f"#minimo-form {FIELD_SELECTOR}"))
            except Exception:
                pass
        page.wait_for_timeout(500)
    return 0


def _render_script(browser, script_url, code, mode, expected, timeout_ms):
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:120]))
    page.route(HOST_PAGE, lambda route: route.fulfill(status=200, content_type="text/html",
                                                      body=_host_html(script_url, code, mode)))
    try:
        page.goto(HOST_PAGE, wait_until="load", timeout=timeout_ms)
        if mode == "popup":
            page.wait_for_function("() => window.Minimo && window.Minimo.loadPopupWidget", timeout=timeout_ms)
            page.evaluate("code => { window.Minimo.loadPopupWidget({ code }); }", code)
        n = _fields_in_widget_iframe(page, timeout_ms)
    except Exception as e:
        ctx.close()
        return False, f"error: {str(e).splitlines()[0][:140]}"
    ctx.close()
    if n >= expected:
        return True, f"{n} fields rendered"
    err = f"; page error: {errors[0]}" if errors else ""
    return False, f"{n}/{expected} fields rendered{err}"


def _render_page(browser, app, code, expected, timeout_ms):
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{app}/public-forms/{code}", wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector(f"form {FIELD_SELECTOR}", timeout=timeout_ms)
        except Exception:
            pass
        n = len(page.query_selector_all(f"form {FIELD_SELECTOR}"))
        text = page.inner_text("body")[:80].replace("\n", " ")
    except Exception as e:
        ctx.close()
        return False, f"error: {str(e).splitlines()[0][:140]}"
    ctx.close()
    if n >= expected:
        return True, f"{n} fields rendered"
    return False, f"{n}/{expected} fields rendered (page says: {text!r})"


def run(code, app="https://app.minimo.it", api="https://api.minimo.it", timeout=25):
    """Return [(surface, ok, detail)]. A probe failure never raises."""
    app, api = app.rstrip("/"), api.rstrip("/")
    ok, detail, names = _lookup(app, code, timeout)
    results = [("lookup", ok, detail)]
    # Without the definition we still render, expecting at least one field.
    expected = max(1, len(names or []))
    from playwright.sync_api import sync_playwright  # lazy: only where the browser is installed
    t = timeout * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            results.append(("page",) + _render_page(browser, app, code, expected, t))
            for label, url in (("old", f"{app}/api/form"), ("new", f"{api}/v1/api/form")):
                for mode in ("inline", "popup"):
                    results.append((f"{label}-{mode}",) + _render_script(browser, url, code, mode, expected, t))
        finally:
            browser.close()
    return results


def summarize(results):
    bad = [f"{s}: {d}" for s, ok, d in results if not ok]
    if bad:
        return "down", "; ".join(bad)
    return "operational", f"{len(results)} surfaces render (lookup, page, old+new script inline+popup)"


def main():
    ap = argparse.ArgumentParser(description="Public forms render probe (read-only)")
    ap.add_argument("--code", required=True)
    ap.add_argument("--app", default="https://app.minimo.it")
    ap.add_argument("--api", default="https://api.minimo.it")
    ap.add_argument("--timeout", type=int, default=25)
    a = ap.parse_args()
    results = run(a.code, a.app, a.api, a.timeout)
    for s, ok, d in results:
        print(f"{'OK  ' if ok else 'FAIL'} {s:<11} {d}")
    status, detail = summarize(results)
    print(f"{status.upper()}: {detail}")
    return 0 if status == "operational" else 1


if __name__ == "__main__":
    sys.exit(main())
