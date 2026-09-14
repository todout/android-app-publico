#!/usr/bin/env python3
"""
Flow Channel DRM Key Watchdog & Tracker-First Auto-Updater for LeichTV
Runs autonomously on Raspberry Pi.
- Architecture: TRACKER-FIRST (Zero traffic to Flow in standard operation).
- Checks active community trackers on GitHub every 15 minutes.
- Uses multi-author cascade: dxrioacxta (PlayPrem) and cheroga (CherogaTV), plus reserve mirrors.
- If and ONLY if a tracker publishes a new/different DRM key for a channel:
  Makes a single surgical verification request to Flow CDN to confirm the new KID
  matches the live stream before applying the change.
- Rejection Memory: Remembers previously rejected keys from flow_audit.log so Flow is
  never queried twice for the same known bad key.
- Updates channels.json and pushes automatically to GitHub.
- Run with --stats to display an audit and reliability report of community sources.
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
FLOW_AUDIT_LOG_PATH = os.path.join(REPO_DIR, "flow_audit.log")

# Multi-Author Community Trackers Cascade (Queried on GitHub, zero Flow traffic)
COMMUNITY_TRACKER_URLS = [
    # Author 1: dxrioacxta (PlayPrem / DxPanel - 5500+ commits, active 24/7)
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/tv1.json",
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/canales.json",
    # Author 2: cheroga (CherogaTV - Independent automated bot, updated daily)
    "https://raw.githubusercontent.com/cheroga/cheroga.github.io/master/canales_cache.json",
    # Additional fallback feeds from Author 1
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/cvn.json",
    "https://raw.githubusercontent.com/dxrioacxta/playprem/main/fieratv.json",
]

# Known reserve mirrors (available in GitHub code search if main authors rotate)
RESERVE_MIRRORS_INFO = [
    "https://raw.githubusercontent.com/zokerpunk/iptv2/main/digital.m3u",
    "https://raw.githubusercontent.com/elvioladordemark/cijefcji/main/fgerje9",
    "https://raw.githubusercontent.com/Er2334/Er1/main/04",
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
                # Support both samples and channels keys (used by different authors)
                items = cat.get("samples", []) or cat.get("channels", [])
                for s in items:
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
                            by_path[p] = {
                                "combo": combo,
                                "tracker": t_name,
                                "kid": kid_hex,
                                "key": key_hex
                            }
        except Exception as e:
            print(f"[WARN] Error loading tracker {t_name}: {e}")

    return by_path, by_kid, edge_token

def load_rejected_kids() -> set:
    """Loads previously rejected (channel_id, proposed_kid) from flow_audit.log to avoid re-querying Flow."""
    rejected = set()
    if not os.path.exists(FLOW_AUDIT_LOG_PATH):
        return rejected
    try:
        with open(FLOW_AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if "RESULT: REJECTED" in line:
                    m_cid = re.search(r"CHANNEL:\s*([^\s\(]+)", line)
                    m_kid = re.search(r"PROPOSED_KID:\s*([^\s\|]+)", line)
                    if m_cid and m_kid:
                        rejected.add((m_cid.group(1), m_kid.group(1).lower()))
    except Exception:
        pass
    return rejected

def log_flow_audit(cid: str, cname: str, tracker: str, proposed_kid: str, flow_kid: str, result: str, action: str):
    """Logs surgical Flow verification requests for reliability analytics."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] CHANNEL: {cid} ({cname}) | TRACKER: {tracker} | PROPOSED_KID: {proposed_kid} | FLOW_KID: {flow_kid} | RESULT: {result} | ACTION: {action}\n"
    try:
        with open(FLOW_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[ERROR] Could not write to audit log: {e}")

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

def print_audit_stats():
    """Reads flow_audit.log and prints community tracker reliability stats."""
    if not os.path.exists(FLOW_AUDIT_LOG_PATH):
        print("\n[FLOW AUDIT REPORT] No requests have been made to Flow yet (0 requests, perfect zero-traffic state).")
        return

    with open(FLOW_AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    if not lines:
        print("\n[FLOW AUDIT REPORT] Audit log is empty (0 requests made to Flow).")
        return

    print("\n" + "=" * 76)
    print(f" FLOW AUDIT & COMMUNITY TRACKERS RELIABILITY REPORT ({len(lines)} requests logged)")
    print("=" * 76)

    verified_count = 0
    rejected_count = 0
    tracker_stats = {}

    for line in lines:
        m_t = re.search(r"TRACKER:\s*([^\s\|]+)", line)
        m_res = re.search(r"RESULT:\s*([^\s\|]+)", line)
        t_name = m_t.group(1) if m_t else "unknown"
        res = m_res.group(1) if m_res else "unknown"

        if t_name not in tracker_stats:
            tracker_stats[t_name] = {"verified": 0, "rejected": 0, "total": 0}

        tracker_stats[t_name]["total"] += 1
        if res == "VERIFIED":
            verified_count += 1
            tracker_stats[t_name]["verified"] += 1
        else:
            rejected_count += 1
            tracker_stats[t_name]["rejected"] += 1

    print(f"Total Flow verification requests: {len(lines)}")
    print(f" - Verified & Applied:           {verified_count}")
    print(f" - Rejected (Prevented bad key): {rejected_count}")
    print("-" * 76)
    print("Tracker Reliability Breakdown:")
    for t_name, s in tracker_stats.items():
        acc = (s["verified"] / s["total"] * 100) if s["total"] > 0 else 0
        print(f" * {t_name:20}: {s['verified']} verified / {s['rejected']} rejected ({acc:.1f}% accuracy)")

    print("-" * 76)
    print("Recent Flow Requests History (last 5):")
    for l in lines[-5:]:
        print(f"  {l}")
    print("=" * 76 + "\n")

def main():
    if "--stats" in sys.argv:
        print_audit_stats()
        return

    priority_only = "--priority-only" in sys.argv
    mode_str = "PRIORITY (TNT Sports & ESPN Premium)" if priority_only else "FULL (All channels)"
    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if not os.path.exists(CHANNELS_JSON_PATH):
        print(f"[{timestamp_str}] [ERROR] channels.json not found at {CHANNELS_JSON_PATH}")
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
    rejected_kids = load_rejected_kids()

    updated_channels = []

    for ch in test_channels:
        cid = ch["id"]
        cname = ch["name"]
        src = ch.get("source_url", "")
        if not src or "/live/" not in src:
            continue

        path = normalize_path(src[src.find("/live/"):])
        local_drm = ch.get("drm_key", "").strip().lower()
        tracker_entry = by_path.get(path)

        if not tracker_entry:
            continue

        tracker_drm = tracker_entry["combo"]
        tracker_name = tracker_entry["tracker"]

        if tracker_drm == local_drm:
            # Matches perfectly! 0 requests to Flow.
            continue

        target_kid = tracker_entry["kid"]

        # Check rejection memory: if this tracker key was already tested and rejected by Flow, skip it
        if (cid, target_kid.lower()) in rejected_kids:
            continue

        # Tracker has a newly proposed key!
        print(f"[{timestamp_str}] [CHANGE DETECTED] Tracker '{tracker_name}' published new key for {cname} ({cid}):")
        print(f"  Local:   {local_drm}")
        print(f"  Tracker: {tracker_drm}")
        print(f"  Verifying surgically with Flow manifest (1 check)...")

        is_verified, live_kid = verify_live_flow_kid(ch, target_kid, edge_token)
        if is_verified:
            print(f"  [VERIFIED] Flow stream confirmed live KID {live_kid}! Applying new key...")
            log_flow_audit(cid, cname, tracker_name, target_kid, live_kid, "VERIFIED", "KEY_UPDATED")
            ch["drm_key"] = tracker_drm
            updated_channels.append(cid)
        else:
            print(f"  [REJECTED] Flow live stream KID is {live_kid} (expected {target_kid}). Preserving local key.")
            log_flow_audit(cid, cname, tracker_name, target_kid, live_kid, "REJECTED", "PRESERVED_LOCAL")
            rejected_kids.add((cid, target_kid.lower()))

    if not updated_channels:
        # Compact single-line confirmation for cron log to prevent bloat
        print(f"[{timestamp_str}] [OK] {mode_str}: Keys match trackers. Zero Flow requests made.")
        return

    # Bump version and save
    data["version"] = data.get("version", 1) + 1
    with open(CHANNELS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[{timestamp_str}] channels.json updated to version {data['version']}.")

    # Git commit and push to GitHub repository
    try:
        subprocess.run(["git", "add", "channels.json", "flow_audit.log"], cwd=REPO_DIR, check=True)
        commit_msg = f"Auto-update Flow keys ({', '.join(updated_channels)}) [v{data['version']}]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, check=True)
        print(f"[{timestamp_str}] Committed: {commit_msg}")
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        if push_res.returncode == 0:
            print(f"[{timestamp_str}] [SUCCESS] Pushed to GitHub repository successfully!")
        else:
            print(f"[{timestamp_str}] [ERROR] git push failed:\n{push_res.stderr}")
    except Exception as e:
        print(f"[{timestamp_str}] [ERROR] Git operation failed: {e}")

if __name__ == "__main__":
    main()
