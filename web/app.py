"""
Flask app: serves the BGS lookup playground (static frontend) and proxies
submission/job lookups to beckett.com.

Calls run through a real headless Chromium page (Playwright), executing
fetch() inside the authenticated page's own JS context. beckett.com's WAF
blocks plain HTTP clients (Node's https, Python's requests), and separately
CloudFront IP-blocks some cloud regions outright regardless of client --
confirmed Render's Singapore region gets a flat 403 on every request (even
real Chromium, even an already-authenticated session), while Render's
Oregon region works fine. Deploy this to a US region if you ever recreate
the service.

A session is seeded at boot from BECKETT_SESSION_STATE (the full contents
of output/storage_state.json, produced by auto_login.py) so most requests
don't need to log in first. When that session goes stale, POST
/api/refresh-session (guarded by REFRESH_TOKEN) drives a real login through
the same browser and swaps in the fresh session in memory -- no redeploy
needed. That in-memory session is lost on the next cold start (Render's
free tier sleeps after ~15min idle), so the seed env var still matters for
a good first request; refresh scripts/push_session_to_render.py by hand
occasionally, or just click "Refresh session" in the UI when needed.
"""
import concurrent.futures
import json
import os
import threading
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright

BASE = "https://www.beckett.com"
LOGIN_URL = f"{BASE}/login"

app = Flask(__name__, static_folder=".", static_url_path="")

# Playwright's sync API is pinned to whichever OS thread started it, but
# Flask's request threads aren't guaranteed stable across requests -- so
# every Playwright call is funneled through this one dedicated thread.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

_playwright = None
_browser = None
_browser_lock = threading.Lock()

_session_state = None
_session_state_lock = threading.Lock()

# A lookup page is expensive to set up (new context + navigate to establish
# the beckett.com origin) but cheap to reuse -- fetch() calls don't need a
# fresh page each time, so one authenticated page is kept warm across
# requests instead of rebuilding it per lookup.
_lookup_context = None
_lookup_page = None
_lookup_lock = threading.Lock()

FETCH_SCRIPT = """
async ({ url, method, body, headers }) => {
    const resp = await fetch(url, {
        method,
        headers: Object.assign(
            { 'content-type': 'application/json', accept: 'application/json, text/plain, */*' },
            headers || {}
        ),
        body: body !== undefined && body !== null ? JSON.stringify(body) : undefined,
        credentials: 'include',
    });
    const text = await resp.text();
    return { status: resp.status, text };
}
"""


def get_browser():
    global _playwright, _browser
    with _browser_lock:
        if _browser is None:
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(headless=True)
    return _browser


def get_session_state():
    global _session_state
    with _session_state_lock:
        if _session_state is None:
            raw = os.environ.get("BECKETT_SESSION_STATE")
            if not raw:
                raise RuntimeError(
                    'No session available -- click "Refresh session", or set BECKETT_SESSION_STATE.'
                )
            _session_state = json.loads(raw)
        return _session_state


def set_session_state(state):
    global _session_state
    with _session_state_lock:
        _session_state = state
    reset_lookup_page()


def block_heavy_resources(context, block_scripts=False, allow_scripts_from=None):
    blocked_types = {"image", "font", "stylesheet", "media"}
    if block_scripts:
        blocked_types.add("script")

    def handler(route):
        req = route.request
        if req.resource_type in blocked_types:
            route.abort()
            return
        if allow_scripts_from and req.resource_type == "script":
            host = urlparse(req.url).hostname or ""
            if host != allow_scripts_from and not host.endswith("." + allow_scripts_from):
                route.abort()
                return
        route.continue_()

    context.route("**/*", handler)


def perform_login(browser, email, password):
    context = browser.new_context()
    # Only Beckett's own JS is needed (it enables the submit button once
    # the form validates) -- third-party trackers (HubSpot, GTM, Facebook
    # Pixel, Google Ads) are dead weight on a CPU-starved instance.
    block_heavy_resources(context, allow_scripts_from="beckett.com")
    page = context.new_page()
    page.set_default_timeout(45000)
    t0 = time.time()

    def checkpoint(label):
        print(f"[perform_login] {label}: {time.time() - t0:.2f}s", flush=True)

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        checkpoint("goto complete")

        for selector in ["text=Accept All Cookies", "text=Allow All"]:
            try:
                page.click(selector, timeout=3000)
                break
            except Exception:
                pass
        checkpoint("cookie consent handled")

        try:
            page.click("#loginEmail")
        except Exception as e:
            try:
                snippet = page.inner_text("body")[:400]
            except Exception:
                snippet = "<could not read body>"
            raise RuntimeError(f"{e} | url={page.url} | body_snippet={snippet!r}") from e
        checkpoint("clicked #loginEmail")
        page.type("#loginEmail", email, delay=30)
        page.click("#loginPassword")
        page.type("#loginPassword", password, delay=30)
        checkpoint("typed credentials")
        page.wait_for_selector("#btn_login:not([disabled])", timeout=20000)
        checkpoint("submit button enabled")

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("#btn_login")
        checkpoint("post-submit navigation complete")

        if "/login" in page.url:
            raise RuntimeError("Login failed -- check BECKETT_EMAIL / BECKETT_PASSWORD")

        return context.storage_state()
    finally:
        context.close()


def get_lookup_page(browser):
    global _lookup_context, _lookup_page
    with _lookup_lock:
        if _lookup_page is None:
            context = browser.new_context(storage_state=get_session_state())
            block_heavy_resources(context, block_scripts=True)
            page = context.new_page()
            page.set_default_timeout(45000)
            # Any same-origin page works to establish cookies/CORS for our
            # fetch() calls -- robots.txt is tiny and has zero third-party
            # tracker scripts, unlike the homepage.
            page.goto(f"{BASE}/robots.txt", wait_until="domcontentloaded")
            _lookup_context, _lookup_page = context, page
        return _lookup_page


def reset_lookup_page():
    global _lookup_context, _lookup_page
    with _lookup_lock:
        if _lookup_context is not None:
            try:
                _lookup_context.close()
            except Exception:
                pass
        _lookup_context = None
        _lookup_page = None


def api_call(page, method, url, body=None, headers=None):
    result = page.evaluate(
        FETCH_SCRIPT, {"url": url, "method": method, "body": body, "headers": headers or {}}
    )
    if result["status"] >= 400:
        raise RuntimeError(f"{method} {url} failed: HTTP {result['status']}")
    return json.loads(result["text"])


def csrf_header(page):
    token = next((c["value"] for c in page.context.cookies() if c["name"] == "csrf-token"), None)
    return {"csrf-token": token} if token else {}


def get_user_id(page):
    mmr = next((c["value"] for c in page.context.cookies() if c["name"] == "mmr"), "")
    data = api_call(page, "POST", f"{BASE}/api/account/data", {"mmr": mmr}, headers=csrf_header(page))
    return data["user_data"]["user_id"]


def fetch_grading_order(page, job_id, user_id):
    headers = {"user-id": user_id, **csrf_header(page)}
    return api_call(page, "GET", f"{BASE}/api/account/grading_order/{job_id}", headers=headers)


def job_id_for_submission(page, submission_id, user_id, limit=100):
    page_num = 1
    headers = {"user-id": user_id, **csrf_header(page)}
    while True:
        data = None
        for _ in range(3):
            try:
                data = api_call(
                    page,
                    "GET",
                    f"{BASE}/api/account/grading_orders?page={page_num}&limit={limit}&sort_by=invoice_id&order_by=desc",
                    headers=headers,
                )
                break
            except RuntimeError as e:
                if "HTTP 502" in str(e):
                    continue
                raise
        if data is None:
            raise RuntimeError("grading_orders failed after retries")

        for order in data.get("data", []):
            if str(order.get("bgs_online_sub_id")) == str(submission_id):
                return order["job_id"]

        total_pages = data["meta"]["pagination"]["total_pages"]
        if page_num >= total_pages:
            return None
        page_num += 1


@app.route("/")
def index():
    return app.send_static_file("index.html")


def _do_lookup(submission_id, job_id):
    browser = get_browser()
    page = get_lookup_page(browser)
    try:
        user_id = get_user_id(page)
        resolved_job_id = job_id
        if not resolved_job_id:
            resolved_job_id = job_id_for_submission(page, submission_id, user_id)
            if resolved_job_id is None:
                return {"error": f"No order found for submission {submission_id}"}, 404
        return fetch_grading_order(page, resolved_job_id, user_id), 200
    except Exception:
        # Drop the warm page on any failure so the next request starts
        # clean instead of repeating whatever went wrong.
        reset_lookup_page()
        raise


@app.route("/api/lookup")
def lookup():
    submission_id = request.args.get("submissionId")
    job_id = request.args.get("jobId")

    if not submission_id and not job_id:
        return jsonify({"error": "Provide ?submissionId= or ?jobId="}), 400

    try:
        data, status = _executor.submit(_do_lookup, submission_id, job_id).result()
        return jsonify(data), status
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _do_refresh(email, password):
    browser = get_browser()
    state = perform_login(browser, email, password)
    set_session_state(state)
    return {"ok": True, "message": "Session refreshed."}, 200


@app.route("/api/refresh-session", methods=["POST"])
def refresh_session():
    expected_token = os.environ.get("REFRESH_TOKEN")
    if not expected_token:
        return jsonify({"error": "Server isn't configured with REFRESH_TOKEN"}), 500

    given_token = request.headers.get("x-refresh-token")
    if given_token != expected_token:
        return jsonify({"error": "Invalid refresh token"}), 403

    email = os.environ.get("BECKETT_EMAIL")
    password = os.environ.get("BECKETT_PASSWORD")
    if not email or not password:
        return jsonify({"error": "Server isn't configured with BECKETT_EMAIL / BECKETT_PASSWORD"}), 500

    try:
        data, status = _executor.submit(_do_refresh, email, password).result()
        return jsonify(data), status
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
