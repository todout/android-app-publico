#!/usr/bin/env python3
"""
Flow Channel DRM Key Watchdog & Smart Consensus Auto-Updater for LeichTV
Runs autonomously on Raspberry Pi.
- Architecture: TRACKER-FIRST + SMART TWO-SOURCE CONSENSUS + AUTOMATIC FAILOVER
- Multi-Author Tracking:
    * Author 1: dxrioacxta (PlayPrem / DxPanel - continuous ~20 min commit cadence)
    * Author 2: cheroga (CherogaTV - independent Cono Sur bot, ~3.3 hr commit cadence)
    * Author 3 (Candidata de Reserva / Standby): mazurikian (155 canales Flow, M3U con tokens diarios)
- Automatic Failover: Si Author 1 o Author 2 deja de responder o es dado de baja, mazurikian
  es promovido automáticamente al par activo para sostener el consenso de 2 fuentes sin interrupción.
- Two-Source Consensus: Cuando las 2 fuentes activas coinciden en una nueva key, se aplica
  DIRECTAMENTE a channels.json con CERO peticiones a Flow.
- Fast-Track para Pack Fútbol (TNT Sports & ESPN Premium): Si solo UNA fuente publica una nueva key,
  el script usa una verificación quirúrgica contra Flow (Semáforo mediante) para no esperar 24h
  en día de partido.
- Safety Semaphore: Máximo 3 peticiones de verificación a Flow en cualquier ventana móvil de 60 minutos.
- Canales comunes NUNCA hacen peticiones a Flow (requieren consenso).
- Rejection Memory: Recuerda keys rechazadas previamente en flow_audit.log.
- Soporta formatos JSON y M3U (#KODIPROP:inputstream.adaptive.license_key=...).
- Ejecutar con --stats para ver el estado del semáforo, auditoría y redundancia de fuentes.
"""

import os
import sys
import json
import re
import time
import base64
import subprocess
from datetime import datetime, timedelta
import requests

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_JSON_PATH = os.path.join(REPO_DIR, "channels.json")
FLOW_AUDIT_LOG_PATH = os.path.join(REPO_DIR, "flow_audit.log")

# Safety Semaphore: Max surgical Flow requests allowed in any rolling 60-minute window
MAX_FLOW_REQUESTS_PER_HOUR = 3

# Primary Independent Community Sources (Active Consensus Pair)
PRIMARY_SOURCES = {
    "dxrioacxta": [
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/tv1.json",
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/canales.json",
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/cvn.json",
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/fieratv.json",
    ],
    "cheroga": [
        "https://raw.githubusercontent.com/cheroga/cheroga.github.io/master/canales_cache.json"
    ]
}

# Standby Candidate Sources (Promoted automatically if any primary source goes offline/404)
STANDBY_SOURCES = {
    "mazurikian": [
        "https://raw.githubusercontent.com/mazurikian/iptv/main/playlist.m3u"
    ]
}

# Priority sports channels allowed to use Flow surgical verification (Vía Rápida)
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

def get_semaphore_status(max_per_hour: int = MAX_FLOW_REQUESTS_PER_HOUR) -> tuple:
    """
    Checks the rolling 60-minute Flow request window.
    Returns (is_green: bool, count_in_last_hour: int, max_per_hour: int)
    """
    if not os.path.exists(FLOW_AUDIT_LOG_PATH):
        return True, 0, max_per_hour

    now = datetime.now()
    one_hour_ago = now - timedelta(hours=1)
    recent_count = 0

    try:
        with open(FLOW_AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if "FLOW_REQUEST" in line:
                    m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                    if m:
                        log_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                        if log_time > one_hour_ago:
                            recent_count += 1
    except Exception:
        pass

    is_green = (recent_count < max_per_hour)
    return is_green, recent_count, max_per_hour

def fetch_single_tracker(url: str) -> tuple:
    """
    Downloads and parses a tracker URL supporting both JSON and M3U formats.
    Returns (channel_map: dict, edge_token: str or None, error: str or None)
    """
    t_name = url.split("/")[-1]
    bust_url = f"{url}?t={int(time.time())}"
    try:
        resp = requests.get(bust_url, timeout=10)
        if resp.status_code != 200:
            return {}, None, f"HTTP {resp.status_code}"

        text = resp.text
        channels = {}
        edge_tok = None

        # Format A: M3U playlist (#KODIPROP:inputstream.adaptive.license_key=...)
        if "#EXTM3U" in text or url.endswith(".m3u"):
            curr_key = None
            for line in text.splitlines():
                line = line.strip()
                if "license_key=" in line:
                    m = re.search(r"license_key=([a-fA-F0-9]{32}:[a-fA-F0-9]{32})", line)
                    if m:
                        curr_key = m.group(1).lower()
                    else:
                        m2 = re.search(r"keyid=([a-zA-Z0-9_-]+)&(?:amp;)?key=([a-zA-Z0-9_-]+)", line)
                        if m2:
                            curr_key = f"{raw_to_hex(m2.group(1))}:{raw_to_hex(m2.group(2))}"
                elif "/live/" in line and line.startswith("http"):
                    if edge_tok is None:
                        m_tok = re.search(r"/(tok_[^/]+)/", line)
                        if m_tok:
                            edge_tok = m_tok.group(1)
                    p = normalize_path(line[line.find("/live/"):])
                    if curr_key and p not in channels:
                        kid = curr_key.split(":")[0]
                        key = curr_key.split(":")[1]
                        channels[p] = {
                            "combo": curr_key,
                            "tracker": t_name,
                            "kid": kid,
                            "key": key
                        }
                    curr_key = None
            return channels, edge_tok, None

        # Format B: JSON categories and samples/channels
        data = resp.json()
        for cat in data:
            items = cat.get("samples", []) or cat.get("channels", [])
            for s in items:
                u_str = s.get("url", "")
                drm = s.get("drm_license_uri", "")

                if edge_tok is None and u_str:
                    m_tok = re.search(r"/(tok_[^/]+)/", u_str)
                    if m_tok:
                        edge_tok = m_tok.group(1)

                m = re.search(r"keyid=([a-zA-Z0-9_-]+)&(?:amp;)?key=([a-zA-Z0-9_-]+)", drm)
                if not m:
                    continue

                kid_hex = raw_to_hex(m.group(1))
                key_hex = raw_to_hex(m.group(2))
                combo = f"{kid_hex}:{key_hex}"

                if "/live/" in u_str:
                    p = normalize_path(u_str[u_str.find("/live/"):])
                    if p not in channels:
                        channels[p] = {
                            "combo": combo,
                            "tracker": t_name,
                            "kid": kid_hex,
                            "key": key_hex
                        }
        return channels, edge_tok, None
    except Exception as e:
        return {}, None, str(e)

def load_author_tracker_maps() -> tuple:
    """
    Downloads community trackers from GitHub with automatic failover to standby candidate sources.
    Returns (active_author_maps: dict, edge_token: str, sources_status: dict)
    """
    author_maps = {}
    sources_status = {}
    edge_token = DEFAULT_EDGE_TOKEN

    # 1. Load Primary Sources
    for author, urls in PRIMARY_SOURCES.items():
        author_maps[author] = {}
        author_errors = []
        for url in urls:
            ch_map, tok, err = fetch_single_tracker(url)
            if err:
                author_errors.append(f"{url.split('/')[-1]}: {err}")
            else:
                author_maps[author].update(ch_map)
                if tok and edge_token == DEFAULT_EDGE_TOKEN:
                    edge_token = tok

        count = len(author_maps[author])
        if count > 0:
            sources_status[author] = {"role": "primary", "status": "ONLINE", "channels": count}
        else:
            sources_status[author] = {"role": "primary", "status": "OFFLINE", "channels": 0, "errors": author_errors}

    # 2. Check if failover is needed (if fewer than 2 primary sources are healthy)
    healthy_primaries = [a for a, s in sources_status.items() if s["status"] == "ONLINE"]

    if len(healthy_primaries) < 2:
        for s_author, s_urls in STANDBY_SOURCES.items():
            standby_map = {}
            for url in s_urls:
                ch_map, tok, err = fetch_single_tracker(url)
                if not err:
                    standby_map.update(ch_map)
                    if tok and edge_token == DEFAULT_EDGE_TOKEN:
                        edge_token = tok

            if len(standby_map) > 0:
                print(f"[FAILOVER ACTIVO] Promoviendo candidata de reserva '{s_author}' ({len(standby_map)} canales) para sostener el consenso de 2 fuentes.")
                author_maps[s_author] = standby_map
                sources_status[s_author] = {"role": "promoted_standby", "status": "ONLINE", "channels": len(standby_map)}
                healthy_primaries.append(s_author)
                if len(healthy_primaries) >= 2:
                    break

    # Build active maps with up to 2 active sources
    active_maps = {a: author_maps[a] for a in healthy_primaries[:2]}
    return active_maps, edge_token, sources_status

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

def log_event(event_type: str, cid: str, cname: str, tracker: str, proposed_kid: str, flow_kid: str, result: str, action: str):
    """Logs verification requests and consensus updates for audit analytics."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {event_type} | CHANNEL: {cid} ({cname}) | TRACKER: {tracker} | PROPOSED_KID: {proposed_kid} | FLOW_KID: {flow_kid} | RESULT: {result} | ACTION: {action}\n"
    try:
        with open(FLOW_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[ERROR] Could not write to audit log: {e}")

def verify_live_flow_kid(channel: dict, target_kid: str, edge_token: str) -> tuple:
    """
    Surgical verification: ONLY called when a single source proposes a new football key.
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
    """Reads flow_audit.log and prints community tracker reliability stats, semaphore health, and redundancy status."""
    is_green, count_last_hour, max_req = get_semaphore_status()
    color_str = "VERDE (Permitido)" if is_green else "ROJO (Circuit Breaker Activo)"

    print("\n" + "=" * 76)
    print(f" FLOW AUDIT & SMART CONSENSUS REPORT")
    print(f" Safety Semaphore Status: {color_str} [{count_last_hour}/{max_req} peticiones a Flow en la última hora]")
    print("=" * 76)

    # Check sources status
    print("Estado de Fuentes Comunitarias:")
    for a, urls in PRIMARY_SOURCES.items():
        ch_total = 0
        status_str = "ONLINE"
        for u in urls:
            ch_map, _, err = fetch_single_tracker(u)
            if err:
                status_str = f"PARCIAL ({err})"
            ch_total += len(ch_map)
        print(f" * {a:12} (Principal):        {status_str} ({ch_total} canales mapeados)")

    for a, urls in STANDBY_SOURCES.items():
        ch_total = 0
        for u in urls:
            ch_map, _, _ = fetch_single_tracker(u)
            ch_total += len(ch_map)
        print(f" * {a:12} (Candidata Suplente): LISTA / EN ESPERA ({ch_total} canales disponibles)")
    print("-" * 76)

    if not os.path.exists(FLOW_AUDIT_LOG_PATH):
        print("[AUDIT REPORT] No events logged yet (0 requests, perfect zero-traffic state).\n")
        return

    with open(FLOW_AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    if not lines:
        print("[AUDIT REPORT] Audit log is empty.\n")
        return

    flow_requests = 0
    flow_verified = 0
    flow_rejected = 0
    consensus_applied = 0
    tracker_stats = {}

    for line in lines:
        is_flow = "FLOW_REQUEST" in line
        m_t = re.search(r"TRACKER:\s*([^\s\|]+)", line)
        m_res = re.search(r"RESULT:\s*([^\s\|]+)", line)
        t_name = m_t.group(1) if m_t else "unknown"
        res = m_res.group(1) if m_res else "unknown"

        if is_flow:
            flow_requests += 1
            if t_name not in tracker_stats:
                tracker_stats[t_name] = {"verified": 0, "rejected": 0, "total": 0}
            tracker_stats[t_name]["total"] += 1
            if res == "VERIFIED":
                flow_verified += 1
                tracker_stats[t_name]["verified"] += 1
            else:
                flow_rejected += 1
                tracker_stats[t_name]["rejected"] += 1
        elif "CONSENSUS" in res:
            consensus_applied += 1

    print(f"Total Flow verification requests: {flow_requests}")
    print(f" - Flow Verified & Applied:      {flow_verified}")
    print(f" - Flow Rejected (Bad keys):     {flow_rejected}")
    print(f"Total Consensus Applied (0 Flow):{consensus_applied}")
    print("-" * 76)
    if tracker_stats:
        print("Tracker Verification Accuracy:")
        for t_name, s in tracker_stats.items():
            acc = (s["verified"] / s["total"] * 100) if s["total"] > 0 else 0
            print(f" * {t_name:20}: {s['verified']} verified / {s['rejected']} rejected ({acc:.1f}% accuracy)")
        print("-" * 76)

    print("Recent Audit Events (last 5):")
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

    # Load tracker maps from active authors (with automatic failover to standby candidate)
    author_maps, edge_token, sources_status = load_author_tracker_maps()
    active_authors = list(author_maps.keys())

    if len(active_authors) < 2:
        print(f"[{timestamp_str}] [WARN] Menos de 2 fuentes disponibles ({active_authors}). Consenso suspendido temporalmente.")
        return

    author_a, author_b = active_authors[0], active_authors[1]
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

        entry_a = author_maps[author_a].get(path)
        entry_b = author_maps[author_b].get(path)

        drm_a = entry_a["combo"] if entry_a else None
        drm_b = entry_b["combo"] if entry_b else None

        # Check: do both trackers match local_drm?
        if (drm_a == local_drm or drm_a is None) and (drm_b == local_drm or drm_b is None):
            continue

        # RULE 1: TWO-SOURCE CONSENSUS (Zero Flow requests)
        # If both independent active authors agree on a new key, apply immediately!
        if drm_a and drm_b and drm_a == drm_b and drm_a != local_drm:
            new_kid = drm_a.split(":")[0]
            print(f"[{timestamp_str}] [CONSENSO 2 FUENTES] {author_a} y {author_b} coinciden en nueva key para {cname} ({cid}):")
            print(f"  Local:     {local_drm}")
            print(f"  Consenso:  {drm_a}")
            print(f"  Aplicando directamente a channels.json con CERO tráfico a Flow...")

            log_event("CONSENSUS_UPDATE", cid, cname, f"{author_a}+{author_b}", new_kid, "N/A", "CONSENSUS_APPLIED", "KEY_UPDATED")
            ch["drm_key"] = drm_a
            updated_channels.append(cid)
            continue

        # RULE 2: SINGLE-SOURCE NEW KEY PROPOSAL
        # One source has a new key, but the other has not updated yet.
        candidate_entry = None
        candidate_author = None

        if drm_b and drm_b != local_drm:
            candidate_entry = entry_b
            candidate_author = author_b
        elif drm_a and drm_a != local_drm:
            candidate_entry = entry_a
            candidate_author = author_a

        if not candidate_entry:
            continue

        candidate_drm = candidate_entry["combo"]
        target_kid = candidate_entry["kid"]
        t_name = candidate_entry["tracker"]

        # Check rejection memory: if this key was already tested and rejected by Flow, skip it
        if (cid, target_kid.lower()) in rejected_kids:
            continue

        # SUB-CASE A: PACK FÚTBOL (Vía Rápida para no perder primicias)
        if cid in PRIORITY_CHANNEL_IDS:
            is_green, count_last_hour, max_req = get_semaphore_status()
            if not is_green:
                print(f"[{timestamp_str}] [SEMAPHORE ROJO] Rate limit safety reached ({count_last_hour}/{max_req} in last 60m).")
                print(f"  Aborting Flow request for football channel {cname} to protect home IP.")
                continue

            print(f"[{timestamp_str}] [PRIMICIA FÚTBOL] Autor '{candidate_author}' ({t_name}) publicó nueva key para {cname}:")
            print(f"  Local:     {local_drm}")
            print(f"  Candidata: {candidate_drm}")
            print(f"  [SEMAPHORE VERDE ({count_last_hour}/{max_req})] Verificando quirúrgicamente con Flow (1 petición)...")

            is_verified, live_kid = verify_live_flow_kid(ch, target_kid, edge_token)
            if is_verified:
                print(f"  [VERIFIED] Stream de Flow confirmó live KID {live_kid}! Aplicando nueva key...")
                log_event("FLOW_REQUEST", cid, cname, f"{candidate_author}:{t_name}", target_kid, live_kid, "VERIFIED", "KEY_UPDATED")
                ch["drm_key"] = candidate_drm
                updated_channels.append(cid)
            else:
                print(f"  [REJECTED] Flow live stream KID es {live_kid} (esperado {target_kid}). Preservando local key.")
                log_event("FLOW_REQUEST", cid, cname, f"{candidate_author}:{t_name}", target_kid, live_kid, "REJECTED", "PRESERVED_LOCAL")
                rejected_kids.add((cid, target_kid.lower()))
        else:
            # SUB-CASE B: CANALES COMUNES (Zero Flow Traffic Policy)
            # Non-football channels wait for second source consensus.
            pass

    if not updated_channels:
        # Compact single-line confirmation for cron log to prevent bloat
        print(f"[{timestamp_str}] [OK] {mode_str} [{author_a} + {author_b}]: Keys match trackers. Zero Flow requests made.")
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
