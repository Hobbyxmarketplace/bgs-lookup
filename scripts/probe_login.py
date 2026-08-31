"""
Reconnaissance script: opens a real Chromium window pointed at beckett.com so
YOU can log in manually with your own credentials (nothing is typed or stored
by this script). While you do that, it records every network request/response
so we can see exactly how the auth token is issued and where it's stored
afterward (cookie, localStorage, Authorization header, etc).

Non-interactive by design (no input() prompts) -- it just watches for up to
WAIT_SECONDS while you log in in the visible browser window, snapshotting
storage state every few seconds so we capture the token as soon as it appears.

Usage:
    python3 scripts/probe_login.py [WAIT_SECONDS]

Output (in output/):
    network_log.jsonl   -- every request/response seen during the session
    storage_state.json  -- latest cookies + localStorage/sessionStorage snapshot
    local_storage.json  -- raw localStorage dump from the live page
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
NETWORK_LOG = OUTPUT_DIR / "network_log.jsonl"
STORAGE_STATE = OUTPUT_DIR / "storage_state.json"
LOCAL_STORAGE = OUTPUT_DIR / "local_storage.json"

INTERESTING_KEYWORDS = ("login", "auth", "token", "session", "account", "grading")
WAIT_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 240
POLL_INTERVAL = 5


def safe_headers(obj):
    try:
        return obj.headers
    except Exception:
        return None


SENSITIVE_FIELD_MARKERS = ("password", "login_token", "pwd", "passwd")


def safe_post_data(request):
    try:
        data = request.post_data
    except Exception:
        return "<binary or undecodable>"
    if data and any(marker in data.lower() for marker in SENSITIVE_FIELD_MARKERS):
        return "<REDACTED - contained credentials>"
    return data


def main():
    with sync_playwright() as p, NETWORK_LOG.open("w") as log_f:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def log_request(request):
            entry = {
                "type": "request",
                "method": request.method,
                "url": request.url,
                "headers": safe_headers(request),
                "post_data": safe_post_data(request),
            }
            log_f.write(json.dumps(entry, default=str) + "\n")
            log_f.flush()

        def log_response(response):
            url = response.url
            if any(k in url.lower() for k in INTERESTING_KEYWORDS):
                try:
                    body = response.text()
                except Exception:
                    body = None
                entry = {
                    "type": "response",
                    "status": response.status,
                    "url": url,
                    "headers": safe_headers(response),
                    "body": body[:5000] if body else None,
                }
                log_f.write(json.dumps(entry, default=str) + "\n")
                log_f.flush()

        page.on("request", log_request)
        page.on("response", log_response)
        # Don't let a listener exception kill the whole run
        page.on("crash", lambda: print("page crashed"))

        page.goto("https://www.beckett.com/")

        print(
            f"\nBrowser is open. You have {WAIT_SECONDS}s to log in manually with "
            "your own credentials and navigate to your account/orders page.\n"
            "Snapshotting storage state every "
            f"{POLL_INTERVAL}s so we capture the token as soon as it appears...\n",
            flush=True,
        )

        elapsed = 0
        while elapsed < WAIT_SECONDS:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            try:
                state = context.storage_state()
                STORAGE_STATE.write_text(json.dumps(state, indent=2))
                local_storage = page.evaluate("() => JSON.stringify(window.localStorage)")
                LOCAL_STORAGE.write_text(local_storage)
            except Exception as e:
                print(f"[{elapsed}s] snapshot error: {e}", flush=True)
                continue
            print(f"[{elapsed}s] snapshot saved (url={page.url})", flush=True)

        print("\nDone waiting. Final snapshot saved.")
        print(f"  {NETWORK_LOG}")
        print(f"  {STORAGE_STATE}")
        print(f"  {LOCAL_STORAGE}")

        browser.close()


if __name__ == "__main__":
    main()
