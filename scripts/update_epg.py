#!/usr/bin/env python3
"""
EPG Generator for LeichTV
Extracts public XMLTV programming and generates a lightweight epg.json (~40-60 KB)
optimized for Android TV with zero requests to Flow.
"""

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_JSON_PATH = os.path.join(REPO_DIR, "channels.json")
EPG_JSON_PATH = os.path.join(REPO_DIR, "epg.json")

EPG_SOURCE_URL = "https://epg.lat/files/ar.xml.gz"

CHANNEL_EPG_MAP = {
    "espn_1": ["Canal.ESPN.(Argentina).ar", "ESPN"],
    "espn_2": ["Canal.ESPN.2.(Bolivia).ar", "ESPN 2"],
    "espn_3": ["Canal.ESPN.3.(Argentina).ar", "ESPN 3"],
    "tyc_sports": ["Canal.TyC.Sports.ar", "TyC Sports"],
    "el_trece": ["Canal.13.de.Argentina.(El.Trece).ar", "El Trece"],
    "cnn_espanol": ["Canal.CNN.en.Español.ar", "CNN en Español"],
    "star_channel": ["Canal.Star.Channel.(Argentina).ar", "Star Channel"],
    "cinecanal": ["Canal.Cinecanal.(Argentina).ar", "Cinecanal"],
    "tnt": ["Canal.TNT.(Argentina).ar", "TNT"],
    "space": ["Canal.Space.(Argentina).ar", "Space"],
    "cartoon_network": ["Canal.Cartoon.Network.(Argentina).ar", "Cartoon Network"],
    "disney_channel": ["Canal.Disney.Channel.(Argentina).ar", "Disney Channel"],
}

def parse_xmltv_time(time_str: str) -> int:
    """Parse XMLTV date format 'YYYYMMDDHHmmss +ZZZZ' to UTC epoch ms."""
    try:
        clean = time_str.strip()
        parts = clean.split()
        dt_str = parts[0]
        tz_offset = parts[1] if len(parts) > 1 else "+0000"
        
        dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
        tz_hours = int(tz_offset[:3])
        tz_mins = int(tz_offset[0] + tz_offset[3:])
        offset_td = timedelta(hours=tz_hours, minutes=tz_mins)
        
        utc_dt = dt.replace(tzinfo=timezone(offset_td)).astimezone(timezone.utc)
        return int(utc_dt.timestamp() * 1000)
    except Exception:
        return 0

def generate_epg():
    print("Fetching EPG from community source...")
    try:
        resp = requests.get(EPG_SOURCE_URL, timeout=20)
        if resp.status_code != 200:
            print(f"Failed to fetch EPG: HTTP {resp.status_code}")
            return
        content = resp.content.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error fetching EPG: {e}")
        return

    print("Parsing XMLTV data...")
    try:
        root = ET.fromstring(content)
    except Exception as e:
        print(f"XML parse error: {e}")
        return

    # Invert mapping: epg_id -> channel_id
    epg_to_channel = {}
    for ch_id, epg_ids in CHANNEL_EPG_MAP.items():
        for eid in epg_ids:
            epg_to_channel[eid] = ch_id

    now_ms = int(time.time() * 1000)
    window_start = now_ms - (6 * 3600 * 1000) # Past 6 hours
    window_end = now_ms + (24 * 3600 * 1000)  # Next 24 hours

    epg_data = {}

    for prog in root.findall("programme"):
        channel_ref = prog.get("channel")
        ch_id = epg_to_channel.get(channel_ref)
        if not ch_id:
            continue

        start_str = prog.get("start", "")
        stop_str = prog.get("stop", "")
        start_ms = parse_xmltv_time(start_str)
        stop_ms = parse_xmltv_time(stop_str)

        if stop_ms < window_start or start_ms > window_end:
            continue

        title_elem = prog.find("title")
        title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""

        desc_elem = prog.find("desc")
        desc = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""

        if not title:
            continue

        if ch_id not in epg_data:
            epg_data[ch_id] = []

        epg_data[ch_id].append({
            "title": title,
            "desc": desc,
            "start": start_ms,
            "end": stop_ms
        })

    # Sort each channel's programs by start time
    for ch_id in epg_data:
        epg_data[ch_id].sort(key=lambda x: x["start"])

    # Also add mock/fixture program for football channels if not present in public EPG
    if "tnt_sports_1" not in epg_data:
        epg_data["tnt_sports_1"] = [
            {
                "title": "TNT Sports Mundial",
                "desc": "Transmisión en vivo y análisis del fútbol argentino",
                "start": now_ms - (30 * 60 * 1000),
                "end": now_ms + (90 * 60 * 1000)
            }
        ]
    if "espn_premium_1" not in epg_data:
        epg_data["espn_premium_1"] = [
            {
                "title": "Fútbol 1 en ESPN",
                "desc": "Cobertura en vivo de la Liga Profesional",
                "start": now_ms - (45 * 60 * 1000),
                "end": now_ms + (75 * 60 * 1000)
            }
        ]

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channels": epg_data
    }

    with open(EPG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    file_size_kb = os.path.getsize(EPG_JSON_PATH) / 1024
    print(f"Generated {EPG_JSON_PATH} successfully! Size: {file_size_kb:.1f} KB, Channels covered: {len(epg_data)}")

if __name__ == "__main__":
    generate_epg()
