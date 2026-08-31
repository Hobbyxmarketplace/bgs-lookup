"""
Calls beckett.com's internal APIs using the session captured by probe_login.py.

Beckett's auth is cookie + CSRF-token based (PHPSESSID / bk_session cookies,
plus a `csrf-token` header that must match the `csrf-token` cookie, plus a
`user-id` header identifying the account). There's no separate bearer token
to copy around -- we just replay the cookie jar Playwright captured.

Usage:
    python3 scripts/fetch_beckett.py jobId 2103631
    python3 scripts/fetch_beckett.py submission 7169728
    python3 scripts/fetch_beckett.py lookup BGS 18431357

Session cookies expire / rotate periodically. If you get a 401/403, rerun
probe_login.py to log in again and refresh output/storage_state.json.
"""
import json
import sys
import time
from pathlib import Path

import requests

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
STORAGE_STATE = OUTPUT_DIR / "storage_state.json"
LAST_RESPONSE = OUTPUT_DIR / "last_response.json"
BASE = "https://www.beckett.com"


def load_session():
    state = json.loads(STORAGE_STATE.read_text())
    session = requests.Session()
    csrf_token = None
    for cookie in state.get("cookies", []):
        if "beckett.com" in cookie["domain"]:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
            if cookie["name"] == "csrf-token":
                csrf_token = cookie["value"]

    session.headers.update(
        {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
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


def fetch_grading_order(session, order_id):
    session.headers["user-id"] = get_user_id(session)
    resp = session.get(f"{BASE}/api/account/grading_order/{order_id}")
    resp.raise_for_status()
    return resp.json()


def job_id_for_submission(session, submission_id, limit=50, max_pages=None):
    """The account only has a job_id-keyed detail endpoint; submission_id
    (aka bgs_online_sub_id) only appears in the paginated order listing, so
    we page through it looking for a match."""
    session.headers["user-id"] = get_user_id(session)
    page = 1
    while True:
        for attempt in range(3):
            resp = session.get(
                f"{BASE}/api/account/grading_orders",
                params={"page": page, "limit": limit, "sort_by": "invoice_id", "order_by": "desc"},
            )
            if resp.status_code == 502:
                time.sleep(1.5)
                continue
            resp.raise_for_status()
            break
        else:
            page += 1
            continue

        data = resp.json()
        for order in data.get("data", []):
            if str(order.get("bgs_online_sub_id")) == str(submission_id):
                return order["job_id"]

        total_pages = data["meta"]["pagination"]["total_pages"]
        if page >= total_pages or (max_pages and page >= max_pages):
            return None
        page += 1


def fetch_grading_submission(session, submission_id):
    job_id = job_id_for_submission(session, submission_id)
    if job_id is None:
        raise LookupError(f"No order found with submission_id={submission_id}")
    return fetch_grading_order(session, job_id)


def fetch_grading_lookup(session, category, serial_number):
    resp = session.get(
        f"{BASE}/api/grading/lookup",
        params={"category": category, "serialNumber": serial_number},
    )
    resp.raise_for_status()
    return resp.json()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if not STORAGE_STATE.exists():
        print(f"No session found at {STORAGE_STATE}. Run probe_login.py first.")
        sys.exit(1)

    session = load_session()
    args = sys.argv[1:]
    cmd = args[0]

    # Bare `fetch_beckett.py <id>` defaults to a submission lookup
    if cmd not in ("order", "jobId", "submission", "lookup"):
        cmd, args = "submission", ["submission", cmd]

    if cmd in ("order", "jobId"):
        job_id = args[1]
        data = fetch_grading_order(session, job_id)
    elif cmd == "submission":
        submission_id = args[1]
        data = fetch_grading_submission(session, submission_id)
    elif cmd == "lookup":
        category, serial_number = args[1], args[2]
        data = fetch_grading_lookup(session, category, serial_number)

    output = json.dumps(data, indent=2)
    print(output)
    LAST_RESPONSE.write_text(output)


if __name__ == "__main__":
    main()
