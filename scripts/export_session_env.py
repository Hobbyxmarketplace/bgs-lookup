"""
Turns output/storage_state.json into a compact JSON string -- the value used
for the BECKETT_SESSION_STATE environment variable read by web/app.py. The
deployed app loads this into a real Playwright browser context (a full
cookie jar, not just a header string, since it needs the browser's own
networking stack -- see web/app.py's docstring for why).

Beckett sessions expire/rotate, so re-run this (after re-running
auto_login.py) whenever the deployed playground starts erroring out.
scripts/push_session_to_render.py does this automatically on a schedule.

Usage:
    python3 scripts/export_session_env.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = ROOT / "output" / "storage_state.json"


def build_session_state_json(storage_state_path=STORAGE_STATE):
    if not storage_state_path.exists():
        raise SystemExit(f"No session found at {storage_state_path}. Run auto_login.py first.")

    state = json.loads(storage_state_path.read_text())
    return json.dumps(state, separators=(",", ":"))


def main():
    session_state = build_session_state_json()
    print("Set this as the BECKETT_SESSION_STATE environment variable on your Render service")
    print("(Environment tab), then it'll pick it up on the next deploy:\n")
    print(session_state)


if __name__ == "__main__":
    main()
