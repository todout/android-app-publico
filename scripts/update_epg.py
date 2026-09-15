#!/usr/bin/env python3
"""
EPG Generator for LeichTV
Extracts public XMLTV programming and generates a lightweight epg.json (~80-100 KB)
optimized for Android TV with zero requests to Flow.
Supports standalone execution and autonomous Git commit/push for Raspberry Pi.
"""

import os
import sys
import json
import time
import gzip
import subprocess
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_JSON_PATH = os.path.join(REPO_DIR, "channels.json")
EPG_JSON_PATH = os.path.join(REPO_DIR, "epg.json")

EPG_SOURCE_URL = "https://epg.lat/files/ar.xml.gz"

# Argentine Timezone (UTC-3)
ARG_TZ = timezone(timedelta(hours=-3))

# Channels extracted directly from public XMLTV
CHANNEL_EPG_MAP = {
    "espn_1": ["Canal.ESPN.(Argentina).ar", "ESPN"],
    "espn_2": ["Canal.ESPN.2.(Bolivia).ar", "ESPN 2"],
    "espn_3": ["Canal.ESPN.3.(Argentina).ar", "ESPN 3"],
    "tyc_sports": ["Canal.TyC.Sports.ar", "TyC Sports"],
    "el_trece": ["Canal.13.de.Argentina.(El.Trece).ar", "El Trece"],
    "telefe": ["Canal.Telefé.(Argentina).ar", "Canal.Telefe.(Argentina).ar", "Telefe"],
    "america_tv": ["Canal.America.TV.(Argentina).ar", "Canal.América.TV.(Argentina).ar", "America TV", "América TV"],
    "tv_publica": ["Canal.Televisión.Pública.(Argentina).ar", "Canal.Television.Publica.(Argentina).ar", "TV Pública"],
    "cnn_espanol": ["Canal.CNN.en.Español.ar", "CNN en Español"],
    "star_channel": ["Canal.Star.Channel.(Argentina).ar", "Star Channel"],
    "cinecanal": ["Canal.Cinecanal.(Argentina).ar", "Cinecanal"],
    "tnt": ["Canal.TNT.(Argentina).ar", "TNT"],
    "space": ["Canal.Space.(Argentina).ar", "Space"],
    "cartoon_network": ["Canal.Cartoon.Network.(Argentina).ar", "Cartoon Network"],
    "disney_channel": ["Canal.Disney.Channel.(Argentina).ar", "Disney Channel"],
    "el_gourmet": ["Canal.Elgourmet.ar", "El Gourmet"],
}

# Scheduled daily programming templates for channels not present in public open XMLTV
SCHEDULE_TEMPLATES = {
    "tnt_sports_1": [
        (0, 0, 7, 0, "Lo Mejor de la Fecha", "Resumen de los partidos de la Liga Profesional"),
        (7, 0, 10, 0, "TNT Sports Noticias", "Toda la actualidad del fútbol argentino"),
        (10, 0, 13, 0, "Pelota Parada", "Información al instante de todos los clubes"),
        (13, 0, 16, 0, "Halcones y Palomas", "Debate y análisis de la fecha"),
        (16, 0, 19, 0, "TNT Data Sports", "Estadísticas, números y previa de los encuentros"),
        (19, 0, 21, 30, "TNT Sports Mundial", "Actualidad internacional y análisis"),
        (21, 30, 23, 30, "Fútbol en Vivo / Liga Profesional", "Transmisión en vivo y seguimiento de los partidos"),
        (23, 30, 24, 0, "Todos Somos Técnicos", "Análisis táctico y las polémicas de la jornada")
    ],
    "espn_premium_1": [
        (0, 0, 7, 0, "Repeticiones y Resúmenes", "Lo mejor de la Liga Profesional de Fútbol"),
        (7, 0, 12, 0, "SportsCenter", "Noticias del deporte y fútbol local"),
        (12, 0, 14, 0, "ESPN F12", "Conducción de Mariano Closs y debate del mediodía"),
        (14, 0, 16, 0, "ESPN F90", "Sebastián Vignolo y el análisis de la jornada"),
        (16, 0, 18, 30, "ESPN F360", "Actualidad deportiva con Gustavo López"),
        (18, 30, 20, 30, "ESPN FShow", "Información y debate de la tarde"),
        (20, 30, 21, 30, "Equipo F", "El debate del fútbol con las principales figuras"),
        (21, 30, 23, 30, "Fútbol 1 en ESPN Premium", "Transmisión en vivo de la Liga Profesional"),
        (23, 30, 24, 0, "SportsCenter Noche", "Goles, jugadas y testimonios de los protagonistas")
    ],
    "golf_channel": [
        (0, 0, 7, 0, "Golf de Noche", "Lo mejor de los torneos de golf del mundo"),
        (7, 0, 11, 0, "Golf Central", "Noticias y cobertura de las principales giras"),
        (11, 0, 15, 0, "PGA Tour: Cobertura en Vivo", "Transmisión en directo del PGA Tour"),
        (15, 0, 19, 0, "European Tour & Majors", "Torneo en vivo y seguimiento de hoyos"),
        (19, 0, 22, 0, "Highlights & Análisis del Día", "Resumen de la jornada y mejores golpes"),
        (22, 0, 24, 0, "Golf Central Noche", "Entrevistas, tablas y análisis en profundidad")
    ],
    "fox_sports_1": [
        (0, 0, 8, 0, "FOX Sports Resumen", "Lo mejor del deporte y competiciones"),
        (8, 0, 12, 0, "FOX Sports Radio", "Noticias y debate deportivo"),
        (12, 0, 15, 0, "La Vuelta", "Actualidad del automovilismo y fútbol"),
        (15, 0, 18, 0, "FOX Sports Noticias", "Actualidad de los clubes y Copa Libertadores"),
        (18, 0, 21, 0, "La Jugada Perfecta", "Análisis previo a las transmisiones"),
        (21, 0, 24, 0, "Fútbol & Deportes en Vivo", "Transmisión de eventos en directo")
    ],
    "tn": [
        (0, 0, 6, 0, "Replay Noticias", "Resumen de las noticias de la madrugada"),
        (6, 0, 10, 0, "TN Tempraneros", "El arranque del día con las primeras noticias"),
        (10, 0, 13, 0, "TN Mañanas", "Información en vivo y móviles en la calle"),
        (13, 0, 16, 0, "Nuestra Tarde", "El noticiero de la tarde con todo el panorama"),
        (16, 0, 18, 0, "Está Pasando", "Actualidad política, económica y social"),
        (18, 0, 20, 0, "TN Central", "El noticiero central con el análisis del día"),
        (20, 0, 22, 0, "Sólo una Vuelta Más", "Debate político y económico"),
        (22, 0, 24, 0, "A Dos Voces / Desde el Llano", "Entrevistas en profundidad y opinión")
    ],
    "ln_mas": [
        (0, 0, 6, 0, "Resumen LN+", "Las noticias destacadas de la jornada"),
        (6, 0, 10, 0, "Buen Día Nación", "Primeras noticias y estado de los servicios"),
        (10, 0, 13, 0, "8 AM / Nación Central", "Información económica y actualidad"),
        (13, 0, 16, 0, "El Noticiero", "Información con el equipo de LN+"),
        (16, 0, 18, 0, "Crónicas de la Tarde", "Seguimiento de la agenda nacional"),
        (18, 0, 20, 0, "Hora 18", "Panorama político y económico"),
        (20, 0, 22, 0, "Más Realidad", "Análisis de fondo y debate editorial"),
        (22, 0, 24, 0, "Odisea Argentina / La Cornisa", "Investigaciones e informes especiales")
    ],
    "c5n": [
        (0, 0, 6, 0, "Resumen C5N", "Noticias de la noche y madrugada"),
        (6, 0, 10, 0, "Mañanas Argentinas", "El inicio del día con las noticias más relevantes"),
        (10, 0, 13, 0, "Nos Vemos", "Información en vivo y actualidad social"),
        (13, 0, 17, 0, "El Diario", "Noticias, economía y política"),
        (17, 0, 20, 0, "Minuto Uno", "Análisis y debate con Gustavo Sylvestre"),
        (20, 0, 22, 0, "Duro de Domar", "Debate de actualidad e invitados especiales"),
        (22, 0, 24, 0, "Desiguales / Sobredosis de TV", "Informes y análisis político")
    ],
    "cronica_hd": [
        (0, 0, 6, 0, "Crónica de Madrugada", "Noticias al instante"),
        (6, 0, 10, 0, "Es Muy Temprano", "Móviles en vivo y transporte"),
        (10, 0, 13, 0, "Tiempo Real", "Casos policiales y reclamos sociales"),
        (13, 0, 17, 0, "Santo Día", "Información con el estilo característico de Crónica"),
        (17, 0, 21, 0, "El Noticiero de Crónica", "Toda la verdad al instante"),
        (21, 0, 24, 0, "Crónica Central", "Debate de noche y casos resonantes")
    ],
    "canal_26": [
        (0, 0, 7, 0, "26 Noticias Noche", "Resumen de las noticias mundiales"),
        (7, 0, 12, 0, "La Mañana de 26", "Información internacional y mercados"),
        (12, 0, 17, 0, "26 Noticias Tarde", "Geopolítica, guerras y panorama global"),
        (17, 0, 21, 0, "26 Global", "Informes especiales y corresponsales"),
        (21, 0, 24, 0, "Noche 26", "Análisis de la actualidad internacional")
    ]
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

def generate_template_programs(slots: list) -> list:
    """Generate multi-day program slots (yesterday, today, tomorrow) in Argentina timezone."""
    now_arg = datetime.now(ARG_TZ)
    programs = []
    
    for day_offset in [-1, 0, 1]:
        base_date = (now_arg + timedelta(days=day_offset)).date()
        for sh, sm, eh, em, title, desc in slots:
            start_dt = datetime(base_date.year, base_date.month, base_date.day, sh, sm, tzinfo=ARG_TZ)
            if eh == 24:
                end_dt = datetime(base_date.year, base_date.month, base_date.day, 0, 0, tzinfo=ARG_TZ) + timedelta(days=1)
            else:
                end_dt = datetime(base_date.year, base_date.month, base_date.day, eh, em, tzinfo=ARG_TZ)
            
            programs.append({
                "title": title,
                "desc": desc,
                "start": int(start_dt.timestamp() * 1000),
                "end": int(end_dt.timestamp() * 1000)
            })
    
    programs.sort(key=lambda x: x["start"])
    return programs

def generate_epg(auto_push: bool = False):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fetching EPG from community source...")
    try:
        resp = requests.get(EPG_SOURCE_URL, timeout=30)
        if resp.status_code != 200:
            print(f"Failed to fetch EPG: HTTP {resp.status_code}")
            return False
        raw = resp.content
        try:
            content = gzip.decompress(raw).decode("utf-8", errors="ignore")
        except Exception:
            content = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error fetching EPG: {e}")
        return False

    print("Parsing XMLTV data...")
    try:
        root = ET.fromstring(content)
    except Exception as e:
        print(f"XML parse error: {e}")
        return False

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

    # Supplement channels without open XMLTV with realistic schedule templates
    for ch_id, slots in SCHEDULE_TEMPLATES.items():
        if ch_id not in epg_data or len(epg_data[ch_id]) == 0:
            epg_data[ch_id] = generate_template_programs(slots)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channels": epg_data
    }

    with open(EPG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    file_size_kb = os.path.getsize(EPG_JSON_PATH) / 1024
    print(f"Generated {EPG_JSON_PATH} successfully! Size: {file_size_kb:.1f} KB, Channels covered: {len(epg_data)}")

    if auto_push:
        commit_and_push_epg(len(epg_data))

    return True

def commit_and_push_epg(channels_count: int):
    """Git commit and push epg.json to GitHub repository."""
    try:
        subprocess.run(["git", "add", "epg.json"], cwd=REPO_DIR, check=True)
        diff_res = subprocess.run(["git", "diff", "--staged", "--name-only"], cwd=REPO_DIR, capture_output=True, text=True)
        if "epg.json" not in diff_res.stdout:
            print("No changes in epg.json to commit.")
            return

        commit_msg = f"Auto-update EPG ({channels_count} channels) [{datetime.now(ARG_TZ).strftime('%Y-%m-%d %H:%M')}]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, check=True)
        print(f"Committed: {commit_msg}")
        
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        if push_res.returncode == 0:
            print("[SUCCESS] epg.json pushed to GitHub repository successfully!")
        else:
            print(f"[ERROR] git push failed:\n{push_res.stderr}")
    except Exception as e:
        print(f"[ERROR] Git operation failed: {e}")

if __name__ == "__main__":
    should_push = ("--push" in sys.argv)
    generate_epg(auto_push=should_push)
