"""
Pushes a freshly captured beckett.com session (output/storage_state.json,
produced by auto_login.py) to Render as the BECKETT_SESSION_STATE env var
on the deployed lookup service, then triggers a redeploy so it takes
effect as the seed for the next cold start.

Optional, manual fallback: the deployed app can refresh its own in-memory
session on demand via the "Refresh session" button in the UI (POST
/api/refresh-session), which covers the common case without needing this
script or a redeploy. Run this by hand if you want to push a fresh
persisted seed anyway -- e.g. right before a period where the site will
sit idle and cold-start.

Requires env vars:
    RENDER_API_KEY     -- personal API key, from Render account settings
    RENDER_SERVICE_ID  -- the web service's id (starts with "srv-"),
                           visible in its Render dashboard URL

Usage:
    python3 scripts/auto_login.py
    python3 scripts/push_session_to_render.py
"""
import os

import requests

from export_session_env import build_session_state_json

API_BASE = "https://api.render.com/v1"


def main():
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if not api_key or not service_id:
        raise SystemExit("RENDER_API_KEY and RENDER_SERVICE_ID must be set")

    session_state = build_session_state_json()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    resp = requests.get(f"{API_BASE}/services/{service_id}/env-vars", headers=headers)
    resp.raise_for_status()
    env_vars = [{"key": item["envVar"]["key"], "value": item["envVar"]["value"]} for item in resp.json()]

    for item in env_vars:
        if item["key"] == "BECKETT_SESSION_STATE":
            item["value"] = session_state
            break
    else:
        env_vars.append({"key": "BECKETT_SESSION_STATE", "value": session_state})

    resp = requests.put(f"{API_BASE}/services/{service_id}/env-vars", headers=headers, json=env_vars)
    resp.raise_for_status()
    print("Updated BECKETT_SESSION_STATE on Render.")

    resp = requests.post(f"{API_BASE}/services/{service_id}/deploys", headers=headers, json={})
    resp.raise_for_status()
    print("Triggered new deploy:", resp.json().get("id"))


if __name__ == "__main__":
    main()
