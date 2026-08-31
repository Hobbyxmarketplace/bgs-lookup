"""
Flask app: serves the BGS lookup playground (static frontend) and proxies
submission/job lookups to beckett.com by replaying a session cookie jar
server-side, so the browser never sees credentials.

Session comes from the BECKETT_COOKIES env var (a "name=value; name2=value2"
Cookie header string). Generate/refresh it with:
    python3 scripts/auto_login.py
    python3 scripts/export_session_env.py

Deliberately uses Python's `requests` rather than a Node-based backend --
beckett.com's CDN (CloudFront) fingerprints and blocks Node's TLS client but
not requests/curl, confirmed by interleaved live testing.
"""
import os
import time

import requests
from flask import Flask, jsonify, request

BASE = "https://www.beckett.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

app = Flask(__name__, static_folder=".", static_url_path="")


def build_session():
    cookie_string = os.environ.get("BECKETT_COOKIES")
    if not cookie_string:
        raise RuntimeError("Missing BECKETT_COOKIES environment variable")

    session = requests.Session()
    csrf_token = None
    for pair in cookie_string.split(";"):
        if "=" not in pair:
            continue
        name, _, value = pair.strip().partition("=")
        session.cookies.set(name, value, domain=".beckett.com")
        if name == "csrf-token":
            csrf_token = value

    session.headers.update(
        {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
        }
    )
    if csrf_token:
        session.headers["csrf-token"] = csrf_token
    return session


def get_user_id(session):
    mmr = session.cookies.get("mmr", domain=".beckett.com")
    resp = session.post(f"{BASE}/api/account/data", json={"mmr": mmr})
    resp.raise_for_status()
    return resp.json()["user_data"]["user_id"]


def fetch_grading_order(session, job_id):
    session.headers["user-id"] = get_user_id(session)
    resp = session.get(f"{BASE}/api/account/grading_order/{job_id}")
    resp.raise_for_status()
    return resp.json()


def job_id_for_submission(session, submission_id, limit=50):
    session.headers["user-id"] = get_user_id(session)
    page = 1
    while True:
        resp = None
        for _ in range(3):
            resp = session.get(
                f"{BASE}/api/account/grading_orders",
                params={"page": page, "limit": limit, "sort_by": "invoice_id", "order_by": "desc"},
            )
            if resp.status_code == 502:
                time.sleep(1.5)
                continue
            break
        resp.raise_for_status()

        data = resp.json()
        for order in data.get("data", []):
            if str(order.get("bgs_online_sub_id")) == str(submission_id):
                return order["job_id"]

        total_pages = data["meta"]["pagination"]["total_pages"]
        if page >= total_pages:
            return None
        page += 1


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.after_request
def disable_static_caching(response):
    if request.path in ("/", "/index.html", "/style.css", "/app.js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/api/lookup")
def lookup():
    submission_id = request.args.get("submissionId")
    job_id = request.args.get("jobId")

    if not submission_id and not job_id:
        return jsonify({"error": "Provide ?submissionId= or ?jobId="}), 400

    try:
        session = build_session()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        resolved_job_id = job_id
        if not resolved_job_id:
            resolved_job_id = job_id_for_submission(session, submission_id)
            if resolved_job_id is None:
                return jsonify({"error": f"No order found for submission {submission_id}"}), 404
        data = fetch_grading_order(session, resolved_job_id)
        return jsonify(data)
    except requests.HTTPError as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
