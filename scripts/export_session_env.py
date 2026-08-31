"""
Turns output/storage_state.json into a single Cookie-header string, ready to
paste into Netlify as the BECKETT_COOKIES environment variable used by
web/netlify/functions/lookup.js.

Beckett sessions expire/rotate, so re-run this (after re-running
auto_login.py) whenever the deployed playground starts returning 401/403s.

Usage:
    python3 scripts/export_session_env.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = ROOT / "output" / "storage_state.json"


def main():
    if not STORAGE_STATE.exists():
        raise SystemExit(f"No session found at {STORAGE_STATE}. Run auto_login.py first.")

    state = json.loads(STORAGE_STATE.read_text())
    pairs = [
        f"{c['name']}={c['value']}"
        for c in state.get("cookies", [])
        if "beckett.com" in c["domain"]
    ]
    cookie_string = "; ".join(pairs)

    print("Set this as the BECKETT_COOKIES environment variable in your Netlify site")
    print("(Site configuration -> Environment variables), then redeploy/restart functions:\n")
    print(cookie_string)


if __name__ == "__main__":
    main()
