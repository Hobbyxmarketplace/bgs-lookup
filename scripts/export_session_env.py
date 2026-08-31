"""
Turns output/storage_state.json into a single Cookie-header string -- the
value used for the BECKETT_COOKIES environment variable read by
web/app.py.

Beckett sessions expire/rotate, so re-run this (after re-running
auto_login.py) whenever the deployed playground starts returning 401/403s.
scripts/push_session_to_render.py does this automatically on a schedule.

Usage:
    python3 scripts/export_session_env.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = ROOT / "output" / "storage_state.json"


def build_cookie_string(storage_state_path=STORAGE_STATE):
    if not storage_state_path.exists():
        raise SystemExit(f"No session found at {storage_state_path}. Run auto_login.py first.")

    state = json.loads(storage_state_path.read_text())
    pairs = [
        f"{c['name']}={c['value']}"
        for c in state.get("cookies", [])
        if "beckett.com" in c["domain"]
    ]
    return "; ".join(pairs)


def main():
    cookie_string = build_cookie_string()
    print("Set this as the BECKETT_COOKIES environment variable on your Render service")
    print("(Environment tab), then it'll pick it up on the next deploy:\n")
    print(cookie_string)


if __name__ == "__main__":
    main()
