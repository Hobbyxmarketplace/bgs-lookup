"""
Flask app: serves the BGS lookup playground (static frontend) and proxies
submission/job lookups to beckett.com.

Calls run through a real headless Chromium page (Playwright), executing
fetch() inside the authenticated page's own JS context. beckett.com's WAF
blocks plain HTTP clients (Node's https, Python's requests) when called from
cloud/datacenter IPs, but lets real-browser traffic through -- confirmed by
testing the same code from a residential IP (works) vs Render's IP (403),
and a scripted client (blocked) vs headless Chromium (works) from the same
datacenter IP.

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


def block_heavy_resources(context):
    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "font", "stylesheet", "media")
        else route.continue_(),
    )


def perform_login(browser, email, password):
    context = browser.new_context()
    block_heavy_resources(context)
    page = context.new_page()
    page.set_default_timeout(45000)
    try:
        print("[perform_login] launching goto...", flush=True)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print(f"[perform_login] landed on: {page.url}", flush=True)

        for selector in ["text=Accept All Cookies", "text=Allow All"]:
            try:
                page.click(selector, timeout=3000)
                break
            except Exception:
                pass

        try:
            page.click("#loginEmail", timeout=8000)
        except Exception as e:
            try:
                snippet = page.inner_text("body")[:400]
            except Exception:
                snippet = "<could not read body>"
            print(f"[perform_login] FAILED at #loginEmail | url={page.url} | body_snippet={snippet!r}", flush=True)
            raise RuntimeError(f"{e} | url={page.url} | body_snippet={snippet!r}") from e
        page.type("#loginEmail", email, delay=30)
        page.click("#loginPassword")
        page.type("#loginPassword", password, delay=30)
        page.wait_for_selector("#btn_login:not([disabled])", timeout=20000)

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("#btn_login")

        if "/login" in page.url:
            raise RuntimeError("Login failed -- check BECKETT_EMAIL / BECKETT_PASSWORD")

        return context.storage_state()
    finally:
        context.close()


def new_authenticated_page(browser):
    context = browser.new_context(storage_state=get_session_state())
    block_heavy_resources(context)
    page = context.new_page()
    page.set_default_timeout(45000)
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    return context, page


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


def job_id_for_submission(page, submission_id, user_id, limit=50):
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
    context, page = new_authenticated_page(browser)
    try:
        user_id = get_user_id(page)
        resolved_job_id = job_id
        if not resolved_job_id:
            resolved_job_id = job_id_for_submission(page, submission_id, user_id)
            if resolved_job_id is None:
                return {"error": f"No order found for submission {submission_id}"}, 404
        return fetch_grading_order(page, resolved_job_id, user_id), 200
    finally:
        context.close()


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
