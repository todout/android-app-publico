#!/usr/bin/env python3
"""
Flow Channel DRM Key Watchdog & Smart Consensus Auto-Updater for LeichTV
Runs autonomously on Raspberry Pi.
- Architecture: TRACKER-FIRST + SMART TWO-SOURCE CONSENSUS + AUTOMATIC FAILOVER
- Multi-Author Tracking:
    * Author 1 (Primary): cheroga (CherogaTV / Cheroga de GitHub - Origen backend: mensajerofm.org, ~1-3 hr cadence)
    * Author 2 (Primary): dxrioacxta (PlayPrem / DxPanel - con descifrado AES-128-ECB / XOR transparente, ~20 min cadence)
    * Author 3 (Reserva / Standby): mazurikian (155 canales Flow, M3U con tokens diarios)
- Anti-Ban Protection for External Host (mensajerofm.org):
    * Lazy Polling: Solo consulta mensajerofm.org cada 60 minutos en reposo.
    * On-Demand Trigger: Si dxrioacxta (GitHub CDN) detecta cambio de key, despierta a cheroga de inmediato.
    * HTTP 304 Conditional Cache: Envía If-Modified-Since/ETag (.tracker_cache.json). Si no cambió, 0 bytes.
    * No Cache-Buster: No envía ?t=timestamp al Apache externo para evitar saltos en firewalls o ModSecurity.
- Automatic Failover: Si alguna de las fuentes principales cae o deja de responder,
  las candidatas de reserva son promovidas automáticamente al par activo para sostener el consenso.
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
TRACKER_CACHE_PATH = os.path.join(REPO_DIR, ".tracker_cache.json")

# External tracker lazy polling interval (e.g. mensajerofm.org) in seconds (1 hour)
EXTERNAL_TRACKER_INTERVAL_SECONDS = 3600

# Safety Semaphore: Max surgical Flow requests allowed in any rolling 60-minute window
MAX_FLOW_REQUESTS_PER_HOUR = 3

# Obfuscation keys and decoders used by DxPanel / PlayPrem:
# - Prior to 2026-09-30: Base64 + XOR with DX_XOR_KEY = b"e72dxpro1py"
# - Since 2026-09-30 ("Actualizado desde DX PANEL (AES)"): AES-128-ECB with DX_AES_KEY = b"e72of82ke0gu2o2k"
#   (Configured via Firebase Remote Config 'claveapp')
DX_AES_KEY = b"e72of82ke0gu2o2k"
DX_XOR_KEY = b"e72dxpro1py"

# Crypto backends: try cryptography, then pycryptodome, with pure-Python fallback
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

try:
    from Crypto.Cipher import AES as _PyCryptoAES
    _HAS_PYCRYPTO = True
except ImportError:
    _HAS_PYCRYPTO = False

_AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16
]
_AES_RSBOX = [_AES_SBOX.index(x) for x in range(256)]

def _pure_aes_decrypt_block(block: bytes, rks: list) -> bytes:
    state = [[block[r + 4*c] for c in range(4)] for r in range(4)]
    rk = rks[10]
    for r in range(4):
        for c in range(4): state[r][c] ^= rk[r + 4*c]
    def _xtime(a): return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)
    def _mul(a, b):
        res = 0
        while b:
            if b & 1: res ^= a
            a = _xtime(a)
            b >>= 1
        return res
    for rnd in range(9, 0, -1):
        state[1] = [state[1][3], state[1][0], state[1][1], state[1][2]]
        state[2] = [state[2][2], state[2][3], state[2][0], state[2][1]]
        state[3] = [state[3][1], state[3][2], state[3][3], state[3][0]]
        for r in range(4):
            for c in range(4): state[r][c] = _AES_RSBOX[state[r][c]]
        rk = rks[rnd]
        for r in range(4):
            for c in range(4): state[r][c] ^= rk[r + 4*c]
        for c in range(4):
            col = [state[r][c] for r in range(4)]
            state[0][c] = _mul(col[0], 0x0e) ^ _mul(col[1], 0x0b) ^ _mul(col[2], 0x0d) ^ _mul(col[3], 0x09)
            state[1][c] = _mul(col[0], 0x09) ^ _mul(col[1], 0x0e) ^ _mul(col[2], 0x0b) ^ _mul(col[3], 0x0d)
            state[2][c] = _mul(col[0], 0x0d) ^ _mul(col[1], 0x09) ^ _mul(col[2], 0x0e) ^ _mul(col[3], 0x0b)
            state[3][c] = _mul(col[0], 0x0b) ^ _mul(col[1], 0x0d) ^ _mul(col[2], 0x09) ^ _mul(col[3], 0x0e)
    state[1] = [state[1][3], state[1][0], state[1][1], state[1][2]]
    state[2] = [state[2][2], state[2][3], state[2][0], state[2][1]]
    state[3] = [state[3][1], state[3][2], state[3][3], state[3][0]]
    for r in range(4):
        for c in range(4): state[r][c] = _AES_RSBOX[state[r][c]]
    rk = rks[0]
    for r in range(4):
        for c in range(4): state[r][c] ^= rk[r + 4*c]
    return bytes([state[r][c] for c in range(4) for r in range(4)])

def _pure_aes_key_expansion_128(key: bytes):
    rcon = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
    w = [key[4*i:4*i+4] for i in range(4)]
    for i in range(4, 44):
        temp = w[i-1]
        if i % 4 == 0:
            rot = temp[1:] + temp[:1]
            sub = bytes([_AES_SBOX[b] for b in rot])
            temp = bytes([b ^ (rcon[i//4] if j == 0 else 0) for j, b in enumerate(sub)])
        w.append(bytes([b1 ^ b2 for b1, b2 in zip(w[i-4], temp)]))
    return [b''.join(w[4*r:4*r+4]) for r in range(11)]

def decrypt_aes_128_ecb(data: bytes, key: bytes) -> bytes:
    """Decrypts AES-128-ECB with PKCS7 unpadding (using cryptography, pycrypto, or pure-python fallback)."""
    if len(data) % 16 != 0 or len(data) == 0:
        return b""
    dec = None
    if _HAS_CRYPTOGRAPHY:
        try:
            cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
            dec = cipher.update(data) + cipher.finalize()
        except Exception:
            pass
    elif _HAS_PYCRYPTO:
        try:
            cipher = _PyCryptoAES.new(key, _PyCryptoAES.MODE_ECB)
            dec = cipher.decrypt(data)
        except Exception:
            pass
    if dec is None:
        try:
            rks = _pure_aes_key_expansion_128(key)
            out = bytearray()
            for i in range(0, len(data), 16):
                out.extend(_pure_aes_decrypt_block(data[i:i+16], rks))
            dec = bytes(out)
        except Exception:
            return b""
    if dec and len(dec) > 0:
        pad_len = dec[-1]
        if 1 <= pad_len <= 16 and dec[-pad_len:] == bytes([pad_len]) * pad_len:
            dec = dec[:-pad_len]
    return dec

def decode_dx_str(val: str) -> str:
    """Decodes AES-128-ECB or Base64+XOR obfuscated URLs and DRM license URIs from DxPanel."""
    if not val or not isinstance(val, str):
        return ""
    val = val.strip()
    if val.startswith("http://") or val.startswith("https://") or "keyid=" in val:
        return val
    try:
        raw = base64.b64decode(val)
        # 1. Try modern AES-128-ECB
        if len(raw) % 16 == 0 and len(raw) > 0:
            dec_bytes = decrypt_aes_128_ecb(raw, DX_AES_KEY)
            if dec_bytes:
                dec = dec_bytes.decode("utf-8", errors="ignore")
                if dec.startswith("http://") or dec.startswith("https://") or "keyid=" in dec:
                    return dec
        # 2. Try legacy XOR
        dec = bytes([b ^ DX_XOR_KEY[i % len(DX_XOR_KEY)] for i, b in enumerate(raw)]).decode("utf-8", errors="ignore")
        if dec.startswith("http://") or dec.startswith("https://") or "keyid=" in dec:
            return dec
    except Exception:
        pass
    return val

# Primary Independent Community Sources (Active Consensus Pair)
#
# NOTA DE TRAZABILIDAD Y ARQUITECTURA:
# - 'cheroga': Se deja constancia expresa de que 'mensajerofm.org' es la infraestructura origen de
#   Cheroga (autor del repositorio https://github.com/cheroga/cheroga.github.io). En septiembre 2026,
#   Cheroga reestructuró su repositorio GitHub eliminando 'canales_cache.json' y pasando a consumir
#   sus listas directamente desde mensajerofm.org en sus GitHub Actions (PlayTvPremium.yml y actualizar_json.yml).
#   Se apunta a estos endpoints directos para evitar 404s y se documenta aquí para no perder el hilo
#   en caso de futuras reestructuraciones.
# - 'dxrioacxta': PlayPrem / DxPanel en GitHub con descifrado transparente AES-128-ECB (DX_AES_KEY) y fallback XOR.
PRIMARY_SOURCES = {
    "cheroga": [
        "https://mensajerofm.org/json/nocache_channel.json",
        "https://mensajerofm.org/json/puntoplay_canales.json"
    ],
    "dxrioacxta": [
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/tv1.json",
        "https://raw.githubusercontent.com/dxrioacxta/playprem/main/canales.json"
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

def load_tracker_cache() -> dict:
    """Loads HTTP 304 metadata (Last-Modified, ETag, last_checked, channels, edge_token) from disk."""
    if os.path.exists(TRACKER_CACHE_PATH):
        try:
            with open(TRACKER_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_tracker_cache(cache: dict):
    """Persists tracker cache metadata to disk."""
    try:
        with open(TRACKER_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] No se pudo guardar .tracker_cache.json: {e}")

def fetch_single_tracker(url: str, force_network: bool = False) -> tuple:
    """
    Downloads and parses a tracker URL supporting both JSON and M3U formats.
    Includes smart HTTP 304 conditional cache (If-Modified-Since) for external hosts.
    Returns (channel_map: dict, edge_token: str or None, error: str or None)
    """
    t_name = url.split("/")[-1]
    is_external_self_hosted = ("mensajerofm.org" in url)

    # For external hosts, do NOT use cache-busting ?t=... (it defeats HTTP 304 and overloads Apache)
    if is_external_self_hosted:
        fetch_url = url
    else:
        fetch_url = f"{url}?t={int(time.time())}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }

    cache = load_tracker_cache()
    cached_entry = cache.get(url, {})

    if is_external_self_hosted and not force_network and cached_entry:
        if cached_entry.get("last_modified"):
            headers["If-Modified-Since"] = cached_entry["last_modified"]
        if cached_entry.get("etag"):
            headers["If-None-Match"] = cached_entry["etag"]

    try:
        resp = requests.get(fetch_url, headers=headers, timeout=10)

        # Handle HTTP 304 Not Modified: Reuse cached data with 0 bytes downloaded
        if resp.status_code == 304 and cached_entry:
            cached_entry["last_checked"] = int(time.time())
            cache[url] = cached_entry
            save_tracker_cache(cache)
            return cached_entry.get("channels", {}), cached_entry.get("edge_token"), None

        if resp.status_code != 200:
            if cached_entry.get("channels"):
                return cached_entry["channels"], cached_entry.get("edge_token"), None
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

            if is_external_self_hosted:
                cache[url] = {
                    "last_modified": resp.headers.get("Last-Modified"),
                    "etag": resp.headers.get("ETag"),
                    "last_checked": int(time.time()),
                    "channels": channels,
                    "edge_token": edge_tok
                }
                save_tracker_cache(cache)
            return channels, edge_tok, None

        # Format B: JSON categories and samples/channels
        data = resp.json()
        for cat in data:
            items = cat.get("samples", []) or cat.get("channels", [])
            for s in items:
                u_str = decode_dx_str(s.get("url", ""))
                drm = decode_dx_str(s.get("drm_license_uri", ""))

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

        if is_external_self_hosted:
            cache[url] = {
                "last_modified": resp.headers.get("Last-Modified"),
                "etag": resp.headers.get("ETag"),
                "last_checked": int(time.time()),
                "channels": channels,
                "edge_token": edge_tok
            }
            save_tracker_cache(cache)

        return channels, edge_tok, None
    except Exception as e:
        if cached_entry.get("channels"):
            return cached_entry["channels"], cached_entry.get("edge_token"), None
        return {}, None, str(e)

def load_author_tracker_maps(force_refresh_author: str = None, quiet: bool = False) -> tuple:
    """
    Downloads community trackers with lazy polling for external hosts
    and automatic failover to standby candidate sources.
    Returns (active_author_maps: dict, edge_token: str, sources_status: dict)
    """
    author_maps = {}
    sources_status = {}
    edge_token = DEFAULT_EDGE_TOKEN
    cache = load_tracker_cache()
    now_ts = int(time.time())

    # 1. Load Primary Sources
    for author, urls in PRIMARY_SOURCES.items():
        author_maps[author] = {}
        author_errors = []

        is_external = any("mensajerofm.org" in u for u in urls)
        can_use_lazy_cache = (
            is_external
            and force_refresh_author != author
            and all(u in cache and cache[u].get("channels") for u in urls)
            and all((now_ts - cache[u].get("last_checked", 0)) < EXTERNAL_TRACKER_INTERVAL_SECONDS for u in urls)
        )

        if can_use_lazy_cache:
            for u in urls:
                ch_map = cache[u].get("channels", {})
                tok = cache[u].get("edge_token")
                author_maps[author].update(ch_map)
                if tok and edge_token == DEFAULT_EDGE_TOKEN:
                    edge_token = tok
            count = len(author_maps[author])
            mins_ago = int((now_ts - min(cache[u].get("last_checked", 0) for u in urls)) / 60)
            sources_status[author] = {"role": "primary", "status": f"ONLINE (Cache {mins_ago}m)", "channels": count, "cached": True}
            continue

        for url in urls:
            force_net = (force_refresh_author == author)
            ch_map, tok, err = fetch_single_tracker(url, force_network=force_net)
            if err:
                author_errors.append(f"{url.split('/')[-1]}: {err}")
            else:
                author_maps[author].update(ch_map)
                if tok and edge_token == DEFAULT_EDGE_TOKEN:
                    edge_token = tok

        count = len(author_maps[author])
        if count > 0:
            sources_status[author] = {"role": "primary", "status": "ONLINE", "channels": count, "cached": False}
        else:
            sources_status[author] = {"role": "primary", "status": "OFFLINE", "channels": 0, "errors": author_errors, "cached": False}

    # 2. Check if failover is needed (if fewer than 2 primary sources are healthy)
    healthy_primaries = [a for a, s in sources_status.items() if "ONLINE" in s["status"]]

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
                if not quiet:
                    print(f"[FAILOVER ACTIVO] Promoviendo candidata de reserva '{s_author}' ({len(standby_map)} canales) para sostener el consenso de 2 fuentes.")
                author_maps[s_author] = standby_map
                sources_status[s_author] = {"role": "promoted_standby", "status": "ONLINE", "channels": len(standby_map), "cached": False}
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
    _, _, sources_status = load_author_tracker_maps(force_refresh_author="cheroga")
    for a, urls in PRIMARY_SOURCES.items():
        s = sources_status.get(a, {})
        status_str = s.get("status", "ONLINE")
        ch_total = s.get("channels", 0)
        print(f" * {a:12} (Principal):        {status_str} ({ch_total} canales mapeados)")

    for a, urls in STANDBY_SOURCES.items():
        if a in sources_status:
            ch_total = sources_status[a].get("channels", 0)
        else:
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
    force_refresh = ("--force-refresh" in sys.argv)
    author_maps, edge_token, sources_status = load_author_tracker_maps(force_refresh_author="cheroga" if force_refresh else None)
    active_authors = list(author_maps.keys())

    if len(active_authors) < 2:
        print(f"[{timestamp_str}] [WARN] Menos de 2 fuentes disponibles ({active_authors}). Consenso suspendido temporalmente.")
        return

    author_a, author_b = active_authors[0], active_authors[1]
    rejected_kids = load_rejected_kids()

    updated_channels = []
    cheroga_refreshed_live = not sources_status.get("cheroga", {}).get("cached", False)

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

        # Check if the proposed candidate key is already known and rejected in audit memory
        candidate_test_entry = None
        if drm_b and drm_b != local_drm:
            candidate_test_entry = entry_b
        elif drm_a and drm_a != local_drm:
            candidate_test_entry = entry_a

        if candidate_test_entry and (cid, candidate_test_entry.get("kid", "").lower()) in rejected_kids:
            # Candidate key is a known stale/rejected key, ignore without waking external host
            continue

        # If a candidate proposed a new key and cheroga was loaded from lazy cache,
        # refresh cheroga immediately (HTTP 304 / 200) to confirm live consensus!
        if not cheroga_refreshed_live and (
            (drm_a and drm_a != local_drm) or (drm_b and drm_b != local_drm)
        ):
            print(f"[{timestamp_str}] [LAZY REFRESH] Posible cambio de key detectado en {cid}. Validando cheroga (mensajerofm con If-Modified-Since)...")
            fresh_maps, fresh_tok, _ = load_author_tracker_maps(force_refresh_author="cheroga", quiet=True)
            if "cheroga" in fresh_maps:
                author_maps["cheroga"] = fresh_maps["cheroga"]
                entry_a = author_maps[author_a].get(path)
                entry_b = author_maps[author_b].get(path)
                drm_a = entry_a["combo"] if entry_a else None
                drm_b = entry_b["combo"] if entry_b else None
            cheroga_refreshed_live = True

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

    # Autonomous EPG maintenance for Raspberry Pi (updates epg.json every 6 hours)
    epg_file = os.path.join(REPO_DIR, "epg.json")
    if not os.path.exists(epg_file) or (time.time() - os.path.getmtime(epg_file)) > (6 * 3600):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from update_epg import generate_epg
            print(f"[{timestamp_str}] [EPG] Datos de EPG superan 6 horas. Actualizando epg.json...")
            if generate_epg(auto_push=False):
                subprocess.run(["git", "add", "epg.json"], cwd=REPO_DIR, check=True)
                diff_res = subprocess.run(["git", "diff", "--staged", "--name-only"], cwd=REPO_DIR, capture_output=True, text=True)
                if "epg.json" in diff_res.stdout:
                    commit_msg = f"Auto-update EPG [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
                    subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, check=True)
                    subprocess.run(["git", "push", "origin", "main"], cwd=REPO_DIR, check=True)
                    print(f"[{timestamp_str}] [EPG] [SUCCESS] epg.json actualizado y subido a GitHub!")
        except Exception as e:
            print(f"[{timestamp_str}] [EPG] Aviso: no se pudo actualizar EPG: {e}")

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
