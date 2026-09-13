#!/usr/bin/env python3
"""
Flow Channel DRM Key Watchdog & Auto-Updater for LeichTV
Runs autonomously on Raspberry Pi (Argentine residential IP).
- Checks live Flow DASH manifests for key rotation (KID change).
- If any key changes, fetches updated ClearKey from community tracker.
- Updates channels.json and pushes automatically to GitHub repository.
"""

import os
import sys
import json
import re
import base64
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import requests

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_JSON_PATH = os.path.join(REPO_DIR, "channels.json")
COMMUNITY_TRACKER_URL = "https://raw.githubusercontent.com/dxrioacxta/playprem/main/tv1.json"

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
    # Try base64url decode
    try:
        pad = "=" * ((4 - len(val) % 4) % 4)
        b = base64.urlsafe_b64decode(val + pad)
        if len(b) == 16:
            return b.hex().lower()
    except Exception:
        pass
    return val.lower().replace("-", "")

def fetch_live_token(tv1_data: list) -> str:
    """Extract freshest edge token found in tv1.json."""
    for cat in tv1_data:
        for s in cat.get("samples", []):
            url = s.get("url", "")
            m = re.search(r"/(tok_[^/]+)/", url)
            if m:
                return m.group(1)
    return DEFAULT_EDGE_TOKEN

def check_channel_manifest(channel: dict, edge_token: str) -> tuple:
    """
    Downloads MPD manifest from Flow edge CDN and extracts cenc:default_KID.
    Returns (channel_id, status, live_kid, error_msg)
    """
    cid = channel["id"]
    source_url = channel.get("source_url", "")
    current_drm = channel.get("drm_key", "")
    current_kid = current_drm.split(":")[0].replace("-", "").lower() if current_drm else ""

    if not source_url or "/live/" not in source_url:
        return cid, "SKIP", current_kid, None

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
            return cid, "HTTP_ERROR", current_kid, f"HTTP {resp.status_code}"

        m = re.search(r'default_KID="([^"]+)"', resp.text)
        if not m:
            return cid, "NO_KID", current_kid, "No default_KID found in MPD"

        live_kid = m.group(1).replace("-", "").lower()
        if live_kid == current_kid:
            return cid, "OK", current_kid, None
        else:
            return cid, "MISMATCH", live_kid, f"Live KID {live_kid} != Stored KID {current_kid}"

    except Exception as e:
        return cid, "EXCEPTION", current_kid, str(e)

def build_community_key_map(tv1_data: list) -> dict:
    """
    Builds a lookup map from tv1.json:
    - by normalized stream path
    - by normalized KID
    """
    by_path = {}
    by_kid = {}

    for cat in tv1_data:
        for s in cat.get("samples", []):
            stream_url = s.get("url", "")
            drm = s.get("drm_license_uri", "")
            m = re.search(r"keyid=([a-zA-Z0-9_-]+)&(?:amp;)?key=([a-zA-Z0-9_-]+)", drm)
            if not m:
                continue

            kid_hex = raw_to_hex(m.group(1))
            key_hex = raw_to_hex(m.group(2))
            combo = f"{kid_hex}:{key_hex}"

            by_kid[kid_hex] = combo

            if "/live/" in stream_url:
                path_part = stream_url[stream_url.find("/live/"):].strip()
                norm_p = normalize_path(path_part)
                by_path[norm_p] = combo

    return by_path, by_kid

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Flow Key Watchdog...")

    if not os.path.exists(CHANNELS_JSON_PATH):
        print(f"[ERROR] channels.json not found at {CHANNELS_JSON_PATH}")
        sys.exit(1)

    with open(CHANNELS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    channels = data.get("channels", [])
    dash_channels = [c for c in channels if c.get("source_type") == "dash"]
    print(f"Found {len(dash_channels)} DASH channels to verify.")

    # Fetch community tracker once to get fresh token and key table
    tv1_data = []
    try:
        resp = requests.get(COMMUNITY_TRACKER_URL, timeout=10)
        if resp.status_code == 200:
            tv1_data = resp.json()
    except Exception as e:
        print(f"[WARN] Could not fetch community tracker: {e}")

    edge_token = fetch_live_token(tv1_data) if tv1_data else DEFAULT_EDGE_TOKEN
    by_path, by_kid = build_community_key_map(tv1_data) if tv1_data else ({}, {})

    print(f"Edge token acquired. Testing channel manifests concurrently...")

    mismatches = []
    ok_count = 0
    err_count = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_channel_manifest, c, edge_token) for c in dash_channels]
        for f in futures:
            cid, status, live_kid, msg = f.result()
            if status == "OK":
                ok_count += 1
            elif status == "MISMATCH":
                print(f"[ALERT] {cid}: {msg}")
                mismatches.append((cid, live_kid))
            else:
                # Log minor network warning without failing
                # print(f"[WARN] {cid}: {status} - {msg}")
                err_count += 1

    print(f"Check results: {ok_count} OK, {len(mismatches)} KEY ROTATED, {err_count} network warnings.")

    if not mismatches:
        print("[SUCCESS] All channel keys are valid and matching live streams. No update needed.")
        return

    # Update mismatched channels
    updated_channels = []
    channel_map = {c["id"]: c for c in channels}

    for cid, live_kid in mismatches:
        ch = channel_map.get(cid)
        if not ch:
            continue

        src = ch.get("source_url", "")
        norm_p = normalize_path(src[src.find("/live/"):]) if "/live/" in src else ""

        new_combo = by_kid.get(live_kid) or by_path.get(norm_p)
        if new_combo:
            old_drm = ch.get("drm_key", "")
            ch["drm_key"] = new_combo
            print(f"[UPDATED] {cid}: {old_drm} -> {new_combo}")
            updated_channels.append(cid)
        else:
            print(f"[WARNING] No replacement key found in tracker for {cid} (KID: {live_kid})")

    if not updated_channels:
        print("[INFO] No keys could be automatically matched from tracker. Exiting.")
        return

    # Bump version and save
    data["version"] = data.get("version", 1) + 1
    with open(CHANNELS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"channels.json updated to version {data['version']}.")

    # Git commit and push if in git repo
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
