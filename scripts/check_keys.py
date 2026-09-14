#!/usr/bin/env python3
"""
Flow Channel DRM Key Watchdog & Tracker-First Auto-Updater for LeichTV
Runs autonomously on Raspberry Pi.
- Architecture: TRACKER-FIRST (Zero traffic to Flow in standard operation).
- Checks active community trackers on GitHub every 15 minutes.
- If and ONLY if a tracker publishes a new/different DRM key for a channel:
  Makes a single surgical verification request to Flow CDN to confirm the new KID
  matches the live stream before applying the change.
- Updates channels.json and pushes automatically to GitHub.
"""

import os
import sys
import json
import re
import time
import base64
import subprocess
from datetime import datetime
import requests

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_JSON_PATH = os.path.join(REPO_DIR, "channels.json")

# Community trackers cascade (queried on GitHub, zero Flow traffic)
COMMUNITY_TRACKER_URLS = [
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/tv1.json",
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/canales.json",
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/cvn.json",
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/fieratv.json",
]

# Priority sports channels to check during high-frequency daytime runs
PRIORITY_CHANNEL_IDS = ["tnt_sports_1", "espn_premium_1"]

DEFAULT_EDGE_TOKEN = (
    "tok_eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOiIxNzg5NDY2Nzc1Iiwic2lwIjoiMzcuMjMwLjU2LjE5OSIsInBhdGgiOiIvbGl2ZS9jNmVkcy9FbmN1ZW50cm8vU0FfTGl2ZV9kYXNoX2NlbmMvIiwic2Vzc2lvbl9jZG5faWQiOiIwZTJjM2ZmNDg4M2IxNjQ1Iiwic2Vzc2lvbl9pZCI6IiIsImNsaWVudF9pZCI6IiIsImRldmljZV9pZCI6IiIsIm1heF9zZXNzaW9ucyI6MCwic2Vzc2lvbl9kdXJhdGlvbiI6MCwidXJsIjoiaHR0cHM6Ly8yMDEuMjM1LjY2LjEyMyIsImF1ZCI6Ijc3Iiwic291cmNlcyI6Wzg1LDE0NCwyMTAsODYsODhdfQ==."
    "IxcgdM-JrDwj1xdoSehi7QJLsSP3xinoZliCofcr_RE6L7rtpNCupKv9q5acWqPQTssYQUVhQkRXa94aJiwNqw=="
)

def normalize_path(path_str: str) -> str:
    """Normalize stream path removing _wl and converting to lowercase."""
    return path_str.replace("_wl", "").strip().lower()

def raw_to_hex(val: str) -> str:
    """Converts 32-char hex or Base64url key/KID into lowercase 32-char hex."""
    val = val.strip()
    if len(val) == 32 and all(c in "0123456789abcdefABCDEF" for c in val):
        return val.lower()
    try:
        pad = "=" * ((4 - len(val) % 4) % 4)
        b = base64.urlsafe_b64decode(val + pad)
        if len(b) == 16:
            return b.hex().lower()
    except Exception:
        pass
    return val.lower().replace("-", "")

def load_trackers_key_map() -> tuple:
    """
    Downloads community trackers from GitHub (cache-busted).
    Returns (by_path, by_kid, fresh_edge_token)
    """
    by_path = {}
    by_kid = {}
    edge_token = DEFAULT_EDGE_TOKEN

    for url in COMMUNITY_TRACKER_URLS:
        t_name = url.split("/")[-1]
        try:
            bust_url = f"{url}?t={int(time.time())}"
            resp = requests.get(bust_url, timeout=10)
            if resp.status_code != 200:
                continue

            data = resp.json()
            for cat in data:
                for s in cat.get("samples", []):
                    u_str = s.get("url", "")
                    drm = s.get("drm_license_uri", "")

                    if edge_token == DEFAULT_EDGE_TOKEN and u_str:
                        m_tok = re.search(r"/(tok_[^/]+)/", u_str)
                        if m_tok:
                            edge_token = m_tok.group(1)

                    m = re.search(r"keyid=([a-zA-Z0-9_-]+)&(?:amp;)?key=([a-zA-Z0-9_-]+)", drm)
                    if not m:
                        continue

                    kid_hex = raw_to_hex(m.group(1))
                    key_hex = raw_to_hex(m.group(2))
                    combo = f"{kid_hex}:{key_hex}"

                    if kid_hex not in by_kid:
                        by_kid[kid_hex] = combo

                    if "/live/" in u_str:
                        p = normalize_path(u_str[u_str.find("/live/"):])
                        if p not in by_path:
                            by_path[p] = combo
        except Exception as e:
            print(f"[WARN] Error loading tracker {t_name}: {e}")

    return by_path, by_kid, edge_token

def verify_live_flow_kid(channel: dict, target_kid: str, edge_token: str) -> tuple:
    """
    Surgical verification: ONLY called when a tracker publishes a new key.
    Sends 1 single GET to Flow MPD manifest to check if live stream actually uses target_kid.
    Returns (is_verified: bool, live_kid: str)
    """
    source_url = channel.get("source_url", "")
    if not source_url or "/live/" not in source_url:
        return False, "NO_PATH"

    path_part = source_url[source_url.find("/live/"):]
    mpd_url = f"https://edge-live16-hr.cvattv.com.ar/{edge_token}{path_part}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Referer": "https://portal.app.flow.com.ar/",
        "Origin": "https://portal.app.flow.com.ar"
    }

    try:
        resp = requests.get(mpd_url, headers=headers, timeout=6)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        m = re.search(r'default_KID="([^"]+)"', resp.text)
        if not m:
            return False, "NO_KID"

        live_kid = m.group(1).replace("-", "").lower()
        return (live_kid == target_kid.lower()), live_kid
    except Exception as e:
        return False, str(e)

def main():
    priority_only = "--priority-only" in sys.argv
    mode_str = "PRIORITY (TNT Sports & ESPN Premium)" if priority_only else "FULL (All channels)"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tracker-First Watchdog ({mode_str})...")

    if not os.path.exists(CHANNELS_JSON_PATH):
        print(f"[ERROR] channels.json not found at {CHANNELS_JSON_PATH}")
        sys.exit(1)

    with open(CHANNELS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    channels = data.get("channels", [])
    dash_channels = [c for c in channels if c.get("source_type") == "dash"]

    if priority_only:
        test_channels = [c for c in dash_channels if c.get("id") in PRIORITY_CHANNEL_IDS]
    else:
        test_channels = dash_channels

    # Fetch community trackers from GitHub (0 traffic to Flow)
    by_path, by_kid, edge_token = load_trackers_key_map()
    print(f"Community trackers loaded. Total unique KIDs available: {len(by_kid)}.")

    updated_channels = []

    for ch in test_channels:
        cid = ch["id"]
        cname = ch["name"]
        src = ch.get("source_url", "")
        if not src or "/live/" not in src:
            continue

        path = normalize_path(src[src.find("/live/"):])
        local_drm = ch.get("drm_key", "").strip().lower()
        tracker_drm = by_path.get(path)

        if not tracker_drm:
            continue

        if tracker_drm == local_drm:
            # Matches perfectly! 0 requests to Flow.
            continue

        # Tracker has a different key!
        target_kid = tracker_drm.split(":")[0]
        print(f"[CHANGE DETECTED] Tracker published new key for {cname} ({cid}):")
        print(f"  Local:   {local_drm}")
        print(f"  Tracker: {tracker_drm}")
        print(f"  Verifying surgically with Flow manifest (1 check)...")

        is_verified, live_kid = verify_live_flow_kid(ch, target_kid, edge_token)
        if is_verified:
            print(f"  [VERIFIED] Flow stream confirmed live KID {live_kid}! Applying new key...")
            ch["drm_key"] = tracker_drm
            updated_channels.append(cid)
        else:
            print(f"  [REJECTED] Flow live stream KID is {live_kid} (expected {target_kid}). Preserving local key.")

    if not updated_channels:
        print("[SUCCESS] All channels are up-to-date with trackers. Zero requests sent to Flow.")
        return

    # Bump version and save
    data["version"] = data.get("version", 1) + 1
    with open(CHANNELS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"channels.json updated to version {data['version']}.")

    # Git commit and push to GitHub repository
    try:
        subprocess.run(["git", "add", "channels.json"], cwd=REPO_DIR, check=True)
        commit_msg = f"Auto-update Flow keys ({', '.join(updated_channels)}) [v{data['version']}]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, check=True)
        print(f"Committed: {commit_msg}")
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        if push_res.returncode == 0:
            print("[SUCCESS] Pushed to GitHub repository successfully!")
        else:
            print(f"[ERROR] git push failed:\n{push_res.stderr}")
    except Exception as e:
        print(f"[ERROR] Git operation failed: {e}")

if __name__ == "__main__":
    main()
