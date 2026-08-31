"""
Fully automated login to beckett.com using Playwright, driven by credentials
in .env (BECKETT_EMAIL / BECKETT_PASSWORD). Fills the real login form
(id=loginEmail, id=loginPassword, id=btn_login) so any hidden CSRF/anti-bot
fields on the page are handled the same way a real browser session would.

Saves the resulting session to output/storage_state.json, which
fetch_beckett.py then reuses for API calls.

Usage:
    python3 scripts/auto_login.py
"""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
STORAGE_STATE = OUTPUT_DIR / "storage_state.json"
ENV_FILE = ROOT / ".env"


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main():
    env = load_env()
    email = env.get("BECKETT_EMAIL")
    password = env.get("BECKETT_PASSWORD")
    if not email or not password:
        raise SystemExit(f"BECKETT_EMAIL / BECKETT_PASSWORD not set in {ENV_FILE}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://www.beckett.com/login")
        page.wait_for_load_state("networkidle")

        # Dismiss cookie-consent overlay if present, it can block clicks
        for selector in ["text=Accept All Cookies", "text=Allow All"]:
            try:
                page.click(selector, timeout=3000)
                break
            except Exception:
                pass

        page.click("#loginEmail")
        page.type("#loginEmail", email, delay=30)
        page.click("#loginPassword")
        page.type("#loginPassword", password, delay=30)
        page.wait_for_selector("#btn_login:not([disabled])", timeout=10000)

        with page.expect_navigation(wait_until="networkidle"):
            page.click("#btn_login")

        if "/login" in page.url:
            error_text = page.inner_text("body")[:300]
            raise SystemExit(f"Login appears to have failed, still on {page.url}\n{error_text}")

        state = context.storage_state()
        STORAGE_STATE.write_text(json.dumps(state, indent=2))
        print(f"Logged in successfully. Session saved to {STORAGE_STATE}")
        print(f"Landed on: {page.url}")

        browser.close()


if __name__ == "__main__":
    main()
