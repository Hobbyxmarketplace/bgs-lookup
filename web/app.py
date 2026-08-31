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

A pre-captured session (BECKETT_SESSION_STATE -- the full contents of
output/storage_state.json, produced by auto_login.py and pushed by the
GitHub Actions refresh workflow) is loaded into the browser context so no
login happens per request.
"""
import json
import os
import threading

from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright

BASE = "https://www.beckett.com"

app = Flask(__name__, static_folder=".", static_url_path="")

_playwright = None
_browser = None
_browser_lock = threading.Lock()

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


def load_storage_state():
    raw = os.environ.get("BECKETT_SESSION_STATE")
    if not raw:
        raise RuntimeError("Missing BECKETT_SESSION_STATE environment variable")
    return json.loads(raw)


def new_authenticated_page(browser):
    context = browser.new_context(storage_state=load_storage_state())
    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "font", "stylesheet", "media")
        else route.continue_(),
    )
    page = context.new_page()
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


@app.route("/api/lookup")
def lookup():
    submission_id = request.args.get("submissionId")
    job_id = request.args.get("jobId")

    if not submission_id and not job_id:
        return jsonify({"error": "Provide ?submissionId= or ?jobId="}), 400

    context = None
    try:
        browser = get_browser()
        context, page = new_authenticated_page(browser)

        user_id = get_user_id(page)
        resolved_job_id = job_id
        if not resolved_job_id:
            resolved_job_id = job_id_for_submission(page, submission_id, user_id)
            if resolved_job_id is None:
                return jsonify({"error": f"No order found for submission {submission_id}"}), 404

        data = fetch_grading_order(page, resolved_job_id, user_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        if context is not None:
            context.close()


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
