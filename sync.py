#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu v1.5.1
Automatische Spulen-Synchronisation: Bambu Lab P2S (via Home Assistant) → Spoolman
Deterministisch, kein LLM im Laufzeitpfad.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from entity_config import external_spool_entity, is_external_spool_entity, slot_entities

# Wiederverwendete Session statt requests.get/post/patch(): haelt die
# TCP/TLS-Verbindung zu HA und Spoolman offen (Keep-Alive-Connection-Pooling)
# statt bei jedem einzelnen API-Call neu zu verbinden.
_http = requests.Session()

# Versions-Info auslesen (aus Git tags)
def _get_git_version():
    """Liest die aktuelle Version aus git tags, Fallback zu 1.5"""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5
        )
        version = result.stdout.strip()
        return version if version else "1.5"
    except Exception:
        return "1.5"


def _get_git_info():
    """Liest Git-Info aus Build-Zeit-Dateien (Container-safe)"""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))

        # Lese Build-Zeit-Infos aus Dateien (nicht via subprocess im Container)
        version = "1.5"
        commit = "unknown"
        timestamp = ""

        # Version aus git tag (Build-Zeit)
        version_file = os.path.join(app_dir, ".version")
        if os.path.exists(version_file):
            with open(version_file, "r") as f:
                version = f.read().strip() or "1.5"

        # Release-Version aus VERSION (wird bei jedem Release gepflegt) hat
        # Vorrang: `git pull` im NAS-Checkout holt keine neuen Tags, daher
        # lieferte `git describe` in .version eine veraltete Version.
        release_file = os.path.join(app_dir, "VERSION")
        if os.path.exists(release_file):
            with open(release_file, "r") as f:
                version = f.read().strip() or version

        # Commit Hash (Build-Zeit)
        commit_file = os.path.join(app_dir, ".commit")
        if os.path.exists(commit_file):
            with open(commit_file, "r") as f:
                commit = f.read().strip() or "unknown"

        # Timestamp (Build-Zeit)
        timestamp_file = os.path.join(app_dir, ".timestamp")
        if os.path.exists(timestamp_file):
            with open(timestamp_file, "r") as f:
                timestamp = f.read().strip() or ""

        return {
            "version": version,
            "commit": commit,
            "timestamp": timestamp,
        }
    except Exception:
        return {
            "version": "1.5",
            "commit": "unknown",
            "timestamp": "",
        }


GIT_VERSION = _get_git_version()

def _sm_extra_val(value):
    """Spoolman erwartet Extra-Felder als JSON-kodierte Strings (z.B. '\"myvalue\"')."""
    return json.dumps(str(value))

# ─────────────────────────────────────────────────────────────────
# .env laden
# ─────────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception as _e:
        sys.stderr.write(f"⚠️ Fehler beim Laden der .env-Datei: {_e}\n")

# ─────────────────────────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────────────────────────
HA_URL        = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN      = os.environ.get("HA_TOKEN", "")
SPOOLMAN_URL  = os.environ.get("SPOOLMAN_URL", "http://spoolman.local:7912").rstrip("/")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
STATE_FILE    = os.environ.get("STATE_FILE", "/app/data/state.json")
LOG_FILE      = os.environ.get("LOG_FILE", "/app/logs/sync.log")

# Vollabgleich der Locations (Spule entnommen → 'Lager', Spule im Slot → korrekter Slot)
RECONCILE_INTERVAL_SECONDS = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "900"))  # 15 Min
# Dashboard-Zuordnungen (von consumption.py geschrieben, gleicher Pfad wie dort)
CONSUMPTION_OVERRIDES_FILE = os.environ.get("CONSUMPTION_OVERRIDES_FILE", "/app/data/consumption_overrides.json")

# ── FAILSAFE: Integration bei Verbindungsverlust selbst neu laden ──
# Die Bambu-Lab-Integration verliert regelmaessig die Verbindung zum Drucker und
# meldet die AMS-Slot-Sensoren dann als 'unavailable' oder entfernt sie ganz
# (greghesp/ha-bambulab#1871). Meist genuegt ein Reload des Config-Entries.
# Bewusst konservativ: erst nach mehreren Polls, mit Cooldown und begrenzter
# Anzahl Versuche - ein Reload-Sturm wuerde die Integration nie stabil werden
# lassen. Ist der Fehler nicht behebbar (Drucker aus), bleibt es bei
# RELOAD_MAX_ATTEMPTS Versuchen, bis die Sensoren wieder gesund sind.
INTEGRATION_RELOAD_ENABLED = os.environ.get("INTEGRATION_RELOAD_ENABLED", "1").lower() not in ("0", "false", "no")
RELOAD_AFTER_UNAVAILABLE_POLLS = int(os.environ.get("RELOAD_AFTER_UNAVAILABLE_POLLS", "3"))
RELOAD_COOLDOWN_SECONDS = int(os.environ.get("RELOAD_COOLDOWN_SECONDS", "600"))  # 10 Min
RELOAD_MAX_ATTEMPTS = int(os.environ.get("RELOAD_MAX_ATTEMPTS", "3"))
# Reihenfolge ist relevant: erst der Drucker (liefert die Slot-Daten), dann
# Spoolman (laedt die Spulen-Entities neu).
RELOAD_DOMAINS = ("bambu_lab", "spoolman")

# Bambu-Farbnamen (aus Donkie/SpoolmanDB) für Filament-Namens-Anreicherung, z.B.
# 'Bambu PLA Basic' + Hex '6F5034' -> 'Bambu PLA Basic Cocoa Brown'.
# Lazy geladen (nicht beim Modul-Import): die Datei wird nur gebraucht, wenn
# tatsaechlich ein Bambu-Filament angereichert wird, nicht bei jedem Start.
_BAMBU_COLORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bambu_colors.json")
_BAMBU_COLORS = None


def _get_bambu_colors():
    global _BAMBU_COLORS
    if _BAMBU_COLORS is None:
        try:
            with open(_BAMBU_COLORS_PATH, encoding="utf-8") as f:
                _BAMBU_COLORS = json.load(f)
        except Exception:
            _BAMBU_COLORS = []
    return _BAMBU_COLORS

# Slot-Sensoren (AMS 1 + externe Spule); Namen per .env anpassbar (Issue #6)
SLOT_ENTITIES = slot_entities()

# Materialdichten für Spoolman (g/cm³)
DENSITY_BY_MATERIAL = {
    "PLA": 1.24, "PLA-CF": 1.28, "PLA-SILK": 1.24,
    "PETG": 1.27, "PETG-CF": 1.30, "PETG-HF": 1.27,
    "ABS": 1.05, "ABS-GF": 1.28,
    "ASA": 1.07, "ASA-CF": 1.22,
    "TPU": 1.21, "TPE": 1.20,
    "PA": 1.15, "PA-CF": 1.22, "PA6-CF": 1.22,
    "PC": 1.20, "PC-ABS": 1.15,
    "PVA": 1.19, "HIPS": 1.06,
}

EMPTY_TAG_UID   = "0000000000000000"
EMPTY_TRAY_UUID = "00000000000000000000000000000000"

# Bambu filament_id → Dichte für bessere Genauigkeit
BAMBU_FILAMENT_DENSITY = {
    "GFA00": 1.24,   # PLA Basic
    "GFA01": 1.24,   # PLA Matte
    "GFA15": 1.24,   # PLA Galaxy
    "GFB00": 1.27,   # PETG Basic
    "GFB01": 1.27,   # PETG-HF
    "GFC00": 1.05,   # ABS
    "GFD00": 1.07,   # ASA
    "GFN03": 1.15,   # PA-CF
    "GFS00": 1.24,   # PLA Silk
}

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("spoolsync")

# ─────────────────────────────────────────────────────────────────
# STATE-PERSISTENZ
# ─────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"State-Datei konnte nicht geladen werden: {e}")
    return {
        "slots": {},
    }


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"State-Datei konnte nicht gespeichert werden: {e}")


# ─────────────────────────────────────────────────────────────────
# HOME ASSISTANT API
# ─────────────────────────────────────────────────────────────────
def _ha_headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ha_get_states_batch(entity_ids):
    """Alle States in einem API-Call holen (effizient)."""
    r = _http.get(f"{HA_URL}/api/states", headers=_ha_headers(), timeout=15)
    r.raise_for_status()
    all_states = {s["entity_id"]: s for s in r.json()}
    return {eid: all_states[eid] for eid in entity_ids if eid in all_states}


def ha_get_external_spool_assignment():
    """Liest die externe Spule Zuordnung aus input_select.p2s_slot_external.
    Gibt Spulen-ID zurück oder None wenn keine Zuordnung vorhanden."""
    try:
        all_states = ha_get_states_batch(['input_select.p2s_slot_external'])
        state = all_states.get('input_select.p2s_slot_external')
        if not state:
            return None

        current_option = state.get('state')
        if not current_option or current_option.startswith('–'):
            return None

        # Parse "Spule #11" oder "11" oder "[RFID] ..."
        import re
        match = re.search(r'#?(\d+)', current_option)
        if match:
            spool_id = int(match.group(1))
            return spool_id
        return None
    except Exception as e:
        log.debug(f"Konnte externe Spule Zuordnung nicht lesen: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# FAILSAFE: INTEGRATION AUTOMATISCH NEU LADEN
# ─────────────────────────────────────────────────────────────────
def ha_find_config_entry_id(domain):
    """Sucht die Config-Entry-ID einer Integration anhand ihrer Domain.
    Absichtlich nicht fest verdrahtet: die ID aendert sich, wenn die
    Integration neu eingerichtet wird."""
    r = _http.get(
        f"{HA_URL}/api/config/config_entries/entry",
        headers=_ha_headers(),
        timeout=15,
    )
    r.raise_for_status()
    for entry in r.json():
        if entry.get("domain") == domain:
            return entry.get("entry_id")
    return None


def ha_reload_config_entry(domain):
    """Laedt den Config-Entry einer Integration neu (entspricht 'Neu laden' in
    der HA-Oberflaeche). Zuerst ueber den Service homeassistant.reload_config_entry,
    als Rueckfallebene ueber den Config-Entry-Endpunkt - je nach HA-Version ist
    der eine oder andere Weg verfuegbar.
    Gibt True zurueck, wenn der Reload angenommen wurde."""
    entry_id = ha_find_config_entry_id(domain)
    if not entry_id:
        log.warning(f"Failsafe: Kein Config-Entry fuer Integration '{domain}' gefunden")
        return False

    try:
        r = _http.post(
            f"{HA_URL}/api/services/homeassistant/reload_config_entry",
            headers=_ha_headers(),
            json={"entry_id": entry_id},
            timeout=30,
        )
        r.raise_for_status()
        log.info(f"Failsafe: Integration '{domain}' neu geladen (entry_id={entry_id})")
        return True
    except Exception as service_error:
        log.warning(f"Failsafe: Service-Reload fuer '{domain}' fehlgeschlagen: {service_error}")

    try:
        r = _http.post(
            f"{HA_URL}/api/config/config_entries/entry/{entry_id}/reload",
            headers=_ha_headers(),
            timeout=30,
        )
        r.raise_for_status()
        log.info(f"Failsafe: Integration '{domain}' neu geladen (Fallback-Endpunkt)")
        return True
    except Exception as endpoint_error:
        log.error(f"Failsafe: Reload von '{domain}' endgueltig fehlgeschlagen: {endpoint_error}")
        return False


def _known_slot_entities(state):
    """Slots, zu denen wir bereits eine Zuordnung kennen. Nur deren Ausfall ist
    ein verlaessliches Fehlersignal - ein nie belegter Slot (z.B. kein zweites
    AMS) fehlt dauerhaft und darf keinen Reload ausloesen."""
    known = [eid for eid in SLOT_ENTITIES if state.get("slots", {}).get(eid)]
    return known or SLOT_ENTITIES


def slot_sensors_unhealthy(entity_states, state):
    """True, wenn ALLE bekannten Slot-Sensoren fehlen oder unavailable sind.
    Einzelne unavailable Slots koennen normal sein (Slot leer, Sensor traege) -
    nur der Totalausfall deutet auf eine abgerissene Integration hin."""
    known = _known_slot_entities(state)
    unhealthy = [
        eid for eid in known
        if not entity_states.get(eid)
        or _is_unavailable(entity_states[eid].get("state", ""))
    ]
    return len(unhealthy) == len(known), unhealthy


def failsafe_reload_integrations(state, entity_states):
    """Erkennt einen Totalausfall der Slot-Sensoren und laedt die betroffenen
    Integrationen selbstaendig neu (Drucker + Spoolman).

    Schutzmechanismen gegen einen Reload-Sturm:
    - erst nach RELOAD_AFTER_UNAVAILABLE_POLLS aufeinanderfolgenden Polls
    - danach Cooldown von RELOAD_COOLDOWN_SECONDS zwischen zwei Versuchen
    - maximal RELOAD_MAX_ATTEMPTS Versuche, bis die Sensoren wieder gesund sind
      (Zaehler wird bei Erholung zurueckgesetzt)
    """
    failsafe = state.setdefault("failsafe", {})
    unhealthy_all, unhealthy = slot_sensors_unhealthy(entity_states, state)

    if not unhealthy_all:
        if failsafe.get("unavailable_polls") or failsafe.get("reload_attempts"):
            log.info("Failsafe: Slot-Sensoren wieder verfuegbar - Zaehler zurueckgesetzt")
        failsafe["unavailable_polls"] = 0
        failsafe["reload_attempts"] = 0
        return

    polls = failsafe.get("unavailable_polls", 0) + 1
    failsafe["unavailable_polls"] = polls
    log.warning(
        f"Failsafe: Alle bekannten Slot-Sensoren nicht verfuegbar "
        f"({polls}/{RELOAD_AFTER_UNAVAILABLE_POLLS} Polls) - betroffen: {unhealthy}"
    )

    if not INTEGRATION_RELOAD_ENABLED:
        return
    if polls < RELOAD_AFTER_UNAVAILABLE_POLLS:
        return

    attempts = failsafe.get("reload_attempts", 0)
    if attempts >= RELOAD_MAX_ATTEMPTS:
        log.error(
            f"Failsafe: Bereits {attempts} Reload-Versuche ohne Erfolg - "
            f"keine weiteren Versuche (Drucker aus oder Netzwerkproblem?). "
            f"Zaehler laeuft weiter, sobald die Sensoren wieder gesund sind."
        )
        return

    now = time.time()
    last_reload = failsafe.get("last_reload_ts", 0)
    wait_left = RELOAD_COOLDOWN_SECONDS - (now - last_reload)
    if last_reload and wait_left > 0:
        log.info(f"Failsafe: Cooldown aktiv - naechster Reload-Versuch in {wait_left:.0f}s")
        return

    failsafe["reload_attempts"] = attempts + 1
    failsafe["last_reload_ts"] = now
    log.warning(f"Failsafe: Starte Reload-Versuch {attempts + 1}/{RELOAD_MAX_ATTEMPTS}")

    for domain in RELOAD_DOMAINS:
        try:
            ha_reload_config_entry(domain)
        except Exception as e:
            log.error(f"Failsafe: Reload von '{domain}' fehlgeschlagen: {e}")

    # Nach dem Reload braucht die Integration einige Sekunden, bis die Entities
    # wieder da sind - Zaehler zuruecksetzen, damit der naechste Poll neu
    # bewertet statt sofort den Cooldown weiterzuzaehlen.
    failsafe["unavailable_polls"] = 0


# ─────────────────────────────────────────────────────────────────
# SPOOLMAN API
# ─────────────────────────────────────────────────────────────────
_SM_HEADERS = {"Content-Type": "application/json"}


def sm_get(path, params=None, timeout=10):
    r = _http.get(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM_HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:
        raise ValueError(f"Ungültige JSON von Spoolman (GET {path}): {e}") from e


def sm_post(path, data, timeout=10):
    r = _http.post(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM_HEADERS, json=data, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:
        raise ValueError(f"Ungültige JSON von Spoolman (POST {path}): {e}") from e


def sm_patch(path, data, timeout=10):
    r = _http.patch(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM_HEADERS, json=data, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:
        raise ValueError(f"Ungültige JSON von Spoolman (PATCH {path}): {e}") from e


def ensure_location_exists(location_name):
    """Stellt sicher dass die Location in Spoolman existiert.
    Spoolman Locations sind einfach Strings ohne API - wir erstellen sie indem
    wir eine existierende Spule mit dieser Location updaten."""
    if not location_name:
        return
    try:
        # Suche eine beliebige Spule und teste ob Location setzbar ist
        # Kurzer Timeout (5s) damit Startup nicht blockiert wenn Spoolman langsam ist
        spools = sm_get("/spool", timeout=5)
        if spools:
            test_spool = spools[0]
            sm_patch(f"/spool/{test_spool['id']}", {"location": location_name}, timeout=5)
            log.info(f"Location '{location_name}' registriert")
    except (requests.Timeout, requests.ConnectionError) as e:
        log.warning(f"Location '{location_name}' timeout/offline - überspringen (wird im nächsten Poll versucht): {e}")
    except Exception as e:
        log.debug(f"Konnte Location '{location_name}' nicht registrieren: {e}")


def _normalize_uid(value):
    """Normalisiert Extra-Feld-Wert: entfernt umgebende JSON-Anführungszeichen."""
    if isinstance(value, str):
        return value.strip('"').strip()
    return str(value).strip('"').strip()


def find_spool_by_bambu_uid(uid, spools=None):
    """Client-seitige Suche durch alle Spulen nach bambu_tag_uid.
    spools: optional bereits geladene Spoolman-Liste (siehe sync_loop) - erspart
    einen weiteren vollen Abruf, wenn der Aufrufer sie fuer diesen Poll schon
    hat. None (Default) fragt wie bisher live ab - wichtig als Fallback, damit
    ein fehlgeschlagener Abruf hier weiterhin als Fehler auffliegt statt
    faelschlich "keine Spule gefunden" zu bedeuten und eine Duplikat-Spule
    anzulegen."""
    target = uid.strip('"').upper()
    for spool in (spools if spools is not None else sm_get("/spool")):
        stored = _normalize_uid(spool.get("extra", {}).get("bambu_tag_uid", ""))
        if stored.upper() == target:
            return spool
    return None


def find_spool_by_tray_uuid(tray_uuid, spools=None):
    """Fallback-Suche über bambu_tray_uuid. Siehe find_spool_by_bambu_uid fuer
    die Bedeutung von spools."""
    target = tray_uuid.strip('"').upper()
    for spool in (spools if spools is not None else sm_get("/spool")):
        stored = _normalize_uid(spool.get("extra", {}).get("bambu_tray_uuid", ""))
        if stored.upper() == target:
            return spool
    return None


BAMBU_VENDOR_NAME = "Bambu Lab"


def ensure_vendor(name):
    """Lieferant holen oder anlegen. Gibt vendor_id zurück, None bei leerem Namen."""
    if not name:
        return None
    vendors = sm_get("/vendor")
    for v in vendors:
        if v["name"].lower() == name.lower():
            return v["id"]
    result = sm_post("/vendor", {"name": name})
    log.info(f"  Lieferant angelegt: '{name}' (ID {result['id']})")
    return result["id"]


def _generic_descriptor(material, ha_name):
    """Produktlinie aus dem AMS-Namen ('Bambu PLA Matte' -> 'Matte'). Leer, wenn der
    AMS nur das Material meldet ('PLA', 'Bambu PLA'): dann ist die Linie unbekannt."""
    descriptor = re.sub(r"(?i)bambu", "", ha_name or "")
    descriptor = re.sub(re.escape(material), "", descriptor, flags=re.IGNORECASE)
    return descriptor.strip()


def _match_existing_filament(filaments, name, ha_name, material, color_hex):
    """
    Bestehendes Filament zu einem AMS-Slot finden, None wenn keines passt.

    1. Exakt: Name + Material + Farbe (bisheriges Verhalten).
    2. Fallback (Issue #51): Der AMS meldet bei manchen Original-Spulen nur das
       Material ('PLA') statt 'Bambu PLA Pure'. Der Name passt dann nie, obwohl
       das Filament schon erfasst ist - bisher entstand eine Dublette. Dann zaehlt
       Material + Farbe; bei mehreren Treffern gewinnt das aelteste (kleinste ID).
       Meldet der AMS eine Produktlinie ('Matte'), muss sie im Namen des Filaments
       vorkommen - sonst wuerde z.B. 'Bambu PLA Matte' auf ein 'Bambu PLA Basic'
       gleicher Farbe fallen.
    """
    hex_u = color_hex.upper()
    same_color = [f for f in filaments
                  if f.get("material", "").upper() == material
                  and f.get("color_hex", "").upper() == hex_u]

    for f in same_color:
        if f.get("name", "").lower() == name.lower():
            return f

    descriptor = _generic_descriptor(material, ha_name).lower()
    candidates = [f for f in same_color if descriptor in f.get("name", "").lower()]
    if not candidates:
        return None

    chosen = min(candidates, key=lambda f: f["id"])
    log.info(f"  Filament '{name}' nicht namensgleich, aber Material+Farbe passen: "
             f"'{chosen['name']}' (ID {chosen['id']}) wiederverwendet"
             + (f" ({len(candidates)} Kandidaten)" if len(candidates) > 1 else ""))
    return chosen


def find_or_create_filament(slot, vendor_id):
    """Passendes Filament suchen oder neu anlegen. Gibt filament_id zurück."""
    color_hex  = _parse_color(slot["color"])
    material   = (slot["type"] or "PLA").upper()
    name       = _enrich_filament_name(material, slot["name"], color_hex) or slot["name"] or "Unbekannt"
    weight     = float(slot["tray_weight"] or 1000)
    density    = (BAMBU_FILAMENT_DENSITY.get(slot.get("filament_id", ""))
                  or DENSITY_BY_MATERIAL.get(material, 1.24))

    # Vorhandenes Filament suchen
    params = {"vendor_id": vendor_id} if vendor_id else {}
    filaments = sm_get("/filament", params)
    existing = _match_existing_filament(filaments, name, slot["name"], material, color_hex)
    if existing:
        log.debug(f"  Filament gefunden: '{existing['name']}' (ID {existing['id']})")
        return existing["id"]

    # Neues Filament anlegen
    filament_data = {
        "name": name,
        "material": material,
        "color_hex": color_hex,
        "weight": weight,
        "diameter": 1.75,
        "density": density,
    }
    if vendor_id:
        filament_data["vendor_id"] = vendor_id

    # AMS-Sensor liefert Temperaturen nur waehrend eines aktiven Drucks zuverlaessig
    # (meldet sonst 0) - bambu_colors.json (SpoolmanDB) als Fallback fuer die
    # Produktlinie verwenden. Spulengewicht kommt ausschliesslich von dort, der AMS
    # kennt das Leergewicht der Spule gar nicht.
    family      = _bambu_family_specs(material, slot["name"])
    nozzle_max  = slot.get("nozzle_temp_max", 0) or (family.get("extruder_temp") if family else 0) or 0
    bed_temp    = slot.get("bed_temp", 0) or (family.get("bed_temp") if family else 0) or 0
    spool_weight = family.get("spool_weight") if family else None

    if nozzle_max > 0:
        filament_data["settings_extruder_temp"] = nozzle_max
    if bed_temp > 0:
        filament_data["settings_bed_temp"] = bed_temp
    if spool_weight:
        filament_data["spool_weight"] = spool_weight

    result = sm_post("/filament", filament_data)
    log.info(f"  Filament angelegt: '{name}' ({material}, #{color_hex}) → ID {result['id']}")
    return result["id"]


def create_spool(slot, bambu_uid, location=None):
    """Neue Spule in Spoolman anlegen mit Metadaten (Location, First Used)."""
    # RFID-Spulen sind immer Original-Bambu-Spulen, auch wenn der AMS nur 'PLA'
    # ohne 'Bambu' im Namen meldet (Issue #51). create_spool() wird nur fuer
    # RFID-Spulen aufgerufen (siehe process_slot).
    vendor_name = _detect_vendor(slot["name"]) or BAMBU_VENDOR_NAME
    vendor_id   = ensure_vendor(vendor_name)
    filament_id = find_or_create_filament(slot, vendor_id)

    initial_weight   = float(slot["tray_weight"] or 1000)
    remaining_weight = _calc_remaining(slot) or initial_weight
    now = _get_iso_timestamp()

    spool_data = {
        "filament_id": filament_id,
        "initial_weight": initial_weight,
        "remaining_weight": remaining_weight,
        "location": location or "Lager",
        "extra": {
            "bambu_tag_uid":   _sm_extra_val(bambu_uid),
            "bambu_tray_uuid": _sm_extra_val(slot["tray_uuid"]),
            "first_used":      _sm_extra_val(now),
            "last_used":       _sm_extra_val(now),
        },
    }
    result = sm_post("/spool", spool_data)
    log.info(
        f"  Spule angelegt: ID={result['id']} | '{slot['name']}' | "
        f"UID={bambu_uid} | Location={location or 'Lager'} | Rest={remaining_weight}g"
    )
    return result


def _set_location_to_storage(entity_id, spool_id):
    """Spule ist nicht mehr im Slot → Location zurück auf 'Lager' setzen (falls nötig)."""
    try:
        spool = sm_get(f"/spool/{spool_id}")
        current_location = spool.get("location") or ""
        if current_location.lower() != "lager":
            sm_patch(f"/spool/{spool_id}", {"location": "Lager"})
            log.info(f"{entity_id}: Spule ID={spool_id} entnommen – Location '{current_location}' → 'Lager'")
    except Exception as e:
        log.error(f"{entity_id}: Location-Reset auf 'Lager' fehlgeschlagen (ID={spool_id}): {e}")


def _release_empty_external_spools(entity_states, spools):
    """Externer Slot laut Sensor leer → Spulen mit Standort 'P2S - Extern' auf 'Lager'.
    Greift unabhängig von bambu_tag_uid: der Abgleich alle RECONCILE_INTERVAL_SECONDS
    erfasst Spulen ohne RFID-Tag nicht. 'unavailable' gilt nicht als leer."""
    entity_id = external_spool_entity()
    entity_state = entity_states.get(entity_id)
    if not entity_state:
        return
    slot = _parse_slot(entity_id, entity_state)
    if slot["unavailable"] or not slot["empty"]:
        return
    for spool in spools:
        if (spool.get("location") or "") == "P2S - Extern":
            _set_location_to_storage(entity_id, spool["id"])


def _reconcile_spool_metadata(entity_id, spool_id, location):
    """
    Metadaten bei bestehenden Spulen aktualisieren (nur bei Änderung).
    - Location setzen wenn unterschiedlich
    - Last Used aktualisieren wenn nicht gestern
    Location und Last-Used werden absichtlich in getrennten PATCH-Calls gesendet:
    scheitert einer (z.B. weil ein Extra-Feld in Spoolman nicht registriert ist),
    darf der andere trotzdem durchgehen.
    """
    try:
        spool = sm_get(f"/spool/{spool_id}")
    except Exception as e:
        log.error(f"  {entity_id}: Spule konnte nicht geladen werden (ID={spool_id}): {e}")
        return

    # Location checken und updaten wenn unterschiedlich
    current_location = spool.get("location") or ""
    if current_location.lower() != (location or "").lower():
        try:
            sm_patch(f"/spool/{spool_id}", {"location": location or "Lager"})
            log.info(f"  {entity_id}: Location '{current_location}' → '{location or 'Lager'}'")
        except Exception as e:
            log.error(f"  {entity_id}: Location-Update fehlgeschlagen (ID={spool_id}): {e}")

    # Last Used aktualisieren wenn nicht heute (Datumsteil)
    now = _get_iso_timestamp()
    now_date = now[:10]
    extra = spool.get("extra", {})
    last_used_str = (extra.get("last_used", "") or "").strip('"')
    last_used_date = last_used_str[:10] if last_used_str else ""

    if last_used_date != now_date:
        new_extra = dict(extra)
        new_extra["last_used"] = _sm_extra_val(now)
        try:
            sm_patch(f"/spool/{spool_id}", {"extra": new_extra})
            log.info(f"  {entity_id}: Last Used → {now}")
        except Exception as e:
            log.error(f"  {entity_id}: Last-Used-Update fehlgeschlagen (ID={spool_id}): {e}")


# ─────────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────────
def _parse_color(color_str):
    """#RRGGBBAA oder #RRGGBB → 6-stelliger Hex ohne Raute."""
    c = (color_str or "#000000FF").lstrip("#")
    return c[:6].upper()


def _bambu_family_candidates(material, ha_name):
    """
    Liefert passende Bambu-Produktlinien (Templates) aus bambu_colors.json (SpoolmanDB)
    für Material + generischen AMS-Namen (z.B. 'Bambu PLA Matte' -> Template
    'Matte {color_name}'). Exakte Treffer zuerst, danach alle übrigen Templates
    desselben Materials als Fallback. Leere Liste wenn kein Bambu-Filament oder
    keine Farbdaten geladen.
    """
    bambu_colors = _get_bambu_colors()
    if not bambu_colors or not ha_name or "bambu" not in ha_name.lower():
        return []

    material_u = (material or "").upper()
    descriptor = re.sub(r"(?i)bambu", "", ha_name)
    if material_u:
        descriptor = re.sub(re.escape(material_u), "", descriptor, flags=re.IGNORECASE)
    descriptor = descriptor.strip()

    candidates = [e for e in bambu_colors if e.get("material", "").upper() == material_u]

    def _template_extra(entry):
        return entry.get("name_template", "").replace("{color_name}", "").strip()

    if not descriptor or descriptor.lower() == "basic":
        exact = [e for e in candidates if _template_extra(e) == ""]
    else:
        exact = [e for e in candidates if _template_extra(e).lower() == descriptor.lower()]

    rest = [e for e in candidates if e not in exact]
    return exact + rest


def _enrich_filament_name(material, ha_name, color_hex):
    """
    Ergänzt den generischen AMS-Namen (z.B. 'Bambu PLA Basic') um den offiziellen
    Bambu-Farbnamen (z.B. 'Cocoa Brown'). Gibt bei eindeutigem Treffer
    'Bambu PLA Basic Cocoa Brown' zurück, sonst unverändert ha_name (nie ein
    Fehler, nur best effort).
    """
    hexv = (color_hex or "").upper()
    for entry in _bambu_family_candidates(material, ha_name):
        for c in entry.get("colors", []):
            if c.get("hex", "").upper() == hexv:
                return f"{ha_name} {c['name']}"
    return ha_name


def _bambu_family_specs(material, ha_name):
    """
    Beste passende Bambu-Produktlinie -> dict mit extruder_temp/bed_temp/spool_weight
    (aus bambu_colors.json), oder None wenn kein Bambu-Filament / keine Farbdaten.
    Dient als Fallback für Temperatur/Spulengewicht, wenn der AMS-Sensor gerade
    0 meldet (z.B. Drucker idle).
    """
    candidates = _bambu_family_candidates(material, ha_name)
    return candidates[0] if candidates else None


def _detect_vendor(name):
    """Hersteller aus Filamentname ableiten. None = unbekannt (User weist manuell zu)."""
    if not name:
        return None
    n = name.lower()
    if "bambu" in n:
        return BAMBU_VENDOR_NAME
    if "prusament" in n:
        return "Prusa"
    if "polymaker" in n:
        return "Polymaker"
    if "esun" in n:
        return "eSUN"
    if "sunlu" in n:
        return "SUNLU"
    if "hatchbox" in n:
        return "Hatchbox"
    if "overture" in n:
        return "Overture"
    # Unbekannt → None, Benutzer weist Lieferant manuell in Spoolman zu
    return None


def _calc_remaining(slot):
    """Restgewicht aus remain% und Spulengewicht berechnen."""
    remain = slot.get("remain", -1)
    weight = slot.get("tray_weight", 0)
    if remain is not None and 0 <= remain <= 100 and weight > 0:
        return round(weight * remain / 100, 1)
    return None


UNAVAILABLE_STATES = ("unavailable", "unknown")


def _is_unavailable(state_str):
    """True, wenn der Slot-Sensor keine gueltigen Daten liefert (Bambu-Lab-
    Integration kurzzeitig getrennt - bekanntes Verhalten der Integration).
    WICHTIG: Das ist NICHT dasselbe wie 'leer'. Wird ein Aussetzer als 'leer'
    behandelt, verwirft process_slot die Slot-Zuordnung und setzt die Location
    der Spule auf 'Lager' zurueck - bei flatternder Verbindung gehen so
    laufend korrekte Zuordnungen verloren."""
    return state_str in UNAVAILABLE_STATES


def _is_empty(state_str, attrs):
    """True, wenn kein Filament eingelegt ist. Aussetzer der Integration
    (unavailable/unknown) gelten ausdruecklich NICHT als leer - siehe
    _is_unavailable()."""
    if state_str == "Empty":
        return True
    if attrs.get("empty", False):
        return True
    if (attrs.get("tag_uid") == EMPTY_TAG_UID
            and attrs.get("type", "") == ""
            and attrs.get("name", "") in ("", "Empty")):
        return True
    return False


def _parse_slot(entity_id, entity_state):
    """Relevante Slot-Daten aus HA-Entity-State extrahieren."""
    attrs = entity_state.get("attributes", {})
    return {
        "entity_id":       entity_id,
        "state":           entity_state.get("state", ""),
        "name":            attrs.get("name", "Unbekannt"),
        "type":            attrs.get("type", ""),
        "color":           attrs.get("color", "#00000000"),
        "tag_uid":         attrs.get("tag_uid", EMPTY_TAG_UID),
        "tray_uuid":       attrs.get("tray_uuid", EMPTY_TRAY_UUID),
        "remain":          attrs.get("remain", -1),
        "remain_enabled":  attrs.get("remain_enabled", False),
        "tray_weight":     int(attrs.get("tray_weight", 0) or 0),
        "nozzle_temp_min": int(attrs.get("nozzle_temp_min", 0) or 0),
        "nozzle_temp_max": int(attrs.get("nozzle_temp_max", 0) or 0),
        "bed_temp":        int(attrs.get("bed_temp", 0) or 0),
        "filament_id":     attrs.get("filament_id", ""),
        "slot":            attrs.get("slot"),
        "empty":           _is_empty(entity_state.get("state", ""), attrs),
        "unavailable":     _is_unavailable(entity_state.get("state", "")),
    }


def _generate_fallback_uid(entity_id):
    """Fallback-UID für Spulen ohne RFID und ohne Tray-UUID."""
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slot_key = entity_id.split("_")[-1]
    return f"MANUAL_{ts}_{slot_key}"


def _get_location_from_entity(entity_id):
    """Standort aus entity_id ableiten: 'AMS X - Slot Y' oder 'P2S - Extern' oder None."""
    if not entity_id:
        return None

    if is_external_spool_entity(entity_id):
        return "P2S - Extern"

    m = re.search(r"ams_(\d+)_slot_(\d+)", entity_id.lower())
    if m:
        ams_num, slot_num = m.groups()
        return f"AMS {ams_num} - Slot {slot_num}"

    return None


def _dashboard_tray_key(entity_id):
    """Schlüssel der Dashboard-Zuordnung ('AMS 1 Tray 2' bzw. 'External') zu einer Slot-Entity.
    Muss zu consumption._build_tray_key_to_slot() passen."""
    if is_external_spool_entity(entity_id):
        return "External"
    m = re.search(r"ams_(\d+)_slot_(\d+)", entity_id.lower())
    if m:
        return f"AMS {m.group(1)} Tray {m.group(2)}"
    return None


def _load_dashboard_assignment(entity_id):
    """Im Dashboard gewählte Spulen-ID für diesen Slot oder None (keine Auswahl)."""
    tray_key = _dashboard_tray_key(entity_id)
    if not tray_key or not os.path.exists(CONSUMPTION_OVERRIDES_FILE):
        return None
    try:
        with open(CONSUMPTION_OVERRIDES_FILE, encoding="utf-8") as f:
            overrides = json.load(f)
    except Exception as e:
        log.warning(f"{entity_id}: Dashboard-Zuordnung konnte nicht gelesen werden: {e}")
        return None
    spool_id = overrides.get(tray_key)
    return int(spool_id) if spool_id is not None else None


def _spool_exists(spool_id, spools=None):
    """True/False, ob die Spule in Spoolman existiert. Nutzt die pro Poll geladene
    Liste, sonst eine Einzelabfrage. Nur 404 gilt als 'nicht vorhanden' - andere
    Fehler (Netzwerk, Timeout) werden weitergereicht, damit kein Slot durch einen
    Aussetzer fälschlich auf 'Lager' fällt."""
    if spools is not None:
        return any(s["id"] == spool_id for s in spools)
    try:
        sm_get(f"/spool/{spool_id}")
        return True
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise


def _apply_dashboard_assignment(entity_id, prev_state, spoolman_id, spools=None):
    """Dashboard-Zuordnung auf den Slot anwenden. Gibt (spoolman_id, assigned_by) zurück.

    - Zuordnung gewählt: Spule bekommt den Slot-Standort; eine bisherige Spule im
      Slot geht auf 'Lager'.
    - Zuordnung entfernt, Spule war vorher dashboard-zugeordnet: Spule geht auf 'Lager'.
    - Zuordnung zeigt auf eine in Spoolman nicht (mehr) vorhandene Spule: wird wie
      eine entfernte Zuordnung behandelt.
    - Sonst: keine Änderung.
    """
    dashboard_id = _load_dashboard_assignment(entity_id)
    previous_id = prev_state.get("spoolman_id")

    if dashboard_id is not None and not _spool_exists(dashboard_id, spools):
        log.warning(f"{entity_id}: Dashboard-Zuordnung auf Spule ID={dashboard_id} – Spule in Spoolman nicht vorhanden")
        dashboard_id = None

    if dashboard_id is not None:
        if previous_id and previous_id != dashboard_id:
            _set_location_to_storage(entity_id, previous_id)
        if dashboard_id != previous_id:
            log.info(f"{entity_id}: Dashboard-Zuordnung → Spule ID={dashboard_id}")
        return dashboard_id, "dashboard"

    if prev_state.get("assigned_by") == "dashboard" and previous_id:
        log.info(f"{entity_id}: Dashboard-Zuordnung entfernt – Spule ID={previous_id} → 'Lager'")
        _set_location_to_storage(entity_id, previous_id)
        return None, None

    return spoolman_id, None


def _get_iso_timestamp():
    """Aktuelle Zeit als ISO 8601 String (UTC)."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────
# SLOT-VERARBEITUNG
# ─────────────────────────────────────────────────────────────────
def process_slot(entity_id, slot, state, spools=None):
    """
    Einen Slot verarbeiten:
    1. Leer → State löschen
    2. Spule vorhanden → Dedup-UID bestimmen → Spoolman suchen/anlegen
    3. Restgewicht aktualisieren wenn geändert

    spools: optional bereits fuer diesen Poll geladene Spoolman-Liste (siehe
    sync_loop) - wird an find_spool_by_bambu_uid/find_spool_by_tray_uuid
    weitergereicht, damit nicht pro Slot erneut die komplette Liste geholt wird.
    """
    slots_state = state["slots"]

    # Aussetzer der Bambu-Lab-Integration: Zustand unveraendert lassen. Weder
    # State verwerfen noch Location zuruecksetzen - die Spule liegt physisch
    # weiterhin im Slot, nur HA kann sie gerade nicht melden.
    if slot["unavailable"]:
        cached = slots_state.get(entity_id, {})
        log.info(
            f"{entity_id}: Sensor nicht verfuegbar ({slot['state']}) - "
            f"behalte bisherige Zuordnung"
            + (f" (Spule ID={cached['spoolman_id']})" if cached.get("spoolman_id") else "")
        )
        return

    if slot["empty"]:
        if entity_id in slots_state:
            prev = slots_state.pop(entity_id)
            log.info(f"{entity_id}: Slot geleert (war: '{prev.get('name', '?')}')")
            prev_spoolman_id = prev.get("spoolman_id")
            if prev_spoolman_id:
                _set_location_to_storage(entity_id, prev_spoolman_id)
        return

    tag_uid   = slot["tag_uid"]
    tray_uuid = slot["tray_uuid"]

    # Dedup-Schlüssel festlegen
    if tag_uid and tag_uid != EMPTY_TAG_UID:
        bambu_uid  = tag_uid
        uid_source = "RFID"
    elif tray_uuid and tray_uuid != EMPTY_TRAY_UUID:
        bambu_uid  = f"NOTAG_{tray_uuid}"
        uid_source = "TRAY_UUID"
    else:
        prev = slots_state.get(entity_id, {})
        bambu_uid = prev.get("bambu_uid")
        if not bambu_uid:
            bambu_uid = _generate_fallback_uid(entity_id)
            log.info(f"{entity_id}: Kein RFID/UUID – Fallback-UID generiert: {bambu_uid}")
        uid_source = "FALLBACK"

    # Spulenwechsel erkennen: Tray-UUID geändert → neue Spule
    prev_state   = slots_state.get(entity_id, {})
    prev_uuid    = prev_state.get("tray_uuid", "")
    slot_changed = (
        tray_uuid != EMPTY_TRAY_UUID
        and tray_uuid != prev_uuid
        and prev_uuid != ""
    )
    if slot_changed:
        log.info(
            f"{entity_id}: Spulenwechsel erkannt "
            f"(alt={prev_uuid[:8]}… → neu={tray_uuid[:8]}…)"
        )
        prev_spoolman_id = prev_state.get("spoolman_id")
        if prev_spoolman_id:
            _set_location_to_storage(entity_id, prev_spoolman_id)

    # Spoolman-ID aus State oder neu suchen
    # WICHTIG: Für externe Spule IMMER neu suchen um richtige Location zu setzen
    is_external = is_external_spool_entity(entity_id)
    spoolman_id = prev_state.get("spoolman_id") if (not slot_changed and not is_external) else None

    # Wenn gecachte Spule nicht mehr existiert, State zurücksetzen für Neusuche
    # Nutze spools_cache wenn vorhanden (O(n) nur bei init, dann O(1) via set), sonst Fallback
    if spoolman_id:
        if spools:
            spool_ids = {s["id"] for s in spools}
            if spoolman_id not in spool_ids:
                log.warning(f"{entity_id}: Gecachte Spule (ID={spoolman_id}) nicht mehr vorhanden, neusuche...")
                spoolman_id = None
        else:
            try:
                sm_get(f"/spool/{spoolman_id}")
            except Exception as e:
                log.warning(f"{entity_id}: Gecachte Spule (ID={spoolman_id}) nicht mehr vorhanden, neusuche...")
                spoolman_id = None

    location = _get_location_from_entity(entity_id)

    # Dashboard-Zuordnung (consumption.py) gilt nur für nicht erkannte Spulen.
    # Eine per RFID erkannte Spule bleibt vorrangig.
    assigned_by = None
    if uid_source != "RFID":
        spoolman_id, assigned_by = _apply_dashboard_assignment(entity_id, prev_state, spoolman_id, spools)

    if not spoolman_id:
        log.info(f"{entity_id}: Suche in Spoolman – '{slot['name']}' (UID={bambu_uid}, Quelle={uid_source})")

        existing = find_spool_by_bambu_uid(bambu_uid, spools)

        # Fallback: tray_uuid-Suche bei RFID-Spulen
        if not existing and uid_source == "RFID" and tray_uuid != EMPTY_TRAY_UUID:
            existing = find_spool_by_tray_uuid(tray_uuid, spools)
            if existing:
                log.info(f"{entity_id}: Spule über tray_uuid gefunden (ID={existing['id']})")

        # Fallback: Externe Spule - check HA input_select Zuordnung
        if not existing and is_external_spool_entity(entity_id):
            assigned_id = ha_get_external_spool_assignment()
            if assigned_id:
                existing = find_spool_by_bambu_uid(f"_assigned_{assigned_id}", spools) or \
                           next((s for s in (spools or sm_get("/spool")) if s["id"] == assigned_id), None)
                if existing:
                    log.info(f"{entity_id}: Externe Spule via HA Zuordnung erkannt (ID={existing['id']})")

        if existing:
            spoolman_id = existing["id"]
            log.info(f"{entity_id}: Bestehende Spule gefunden → ID={spoolman_id}")
        else:
            # Neue RFID-Spulen automatisch anlegen (verlässlich identifizierbar)
            if uid_source == "RFID":
                log.info(f"{entity_id}: Neue RFID-Spule erkannt – erstelle automatisch in Spoolman")
                try:
                    new_spool = create_spool(slot, bambu_uid, location)
                    spoolman_id = new_spool["id"]
                    log.info(f"{entity_id}: ✓ RFID-Spule angelegt: Spule #{spoolman_id}")
                except Exception as e:
                    log.error(f"{entity_id}: Fehler beim Anlegen der RFID-Spule: {e}", exc_info=True)
                    spoolman_id = None
            else:
                # Spulen ohne RFID: nur mit manueller Bestätigung
                log.warning(f"{entity_id}: Unbekannte Spule erkannt ('{slot['name']}') – ausstehende Zuordnung in HA")
                spoolman_id = None

    # WICHTIG: remaining_weight wird hier NICHT mehr laufend aus remain% gesetzt.
    # Der RFID-remain%-Wert des Bambu-AMS ist unzuverlässig (bleibt oft bei 100%).
    # Der tatsächliche Verbrauch wird deterministisch von consumption.py über die
    # Druckgewichte getrackt. sync.py setzt das Restgewicht nur EINMALIG bei der
    # Neuanlage einer Spule (siehe create_spool) und pflegt danach nur Metadaten.

    # Metadaten reconciliieren (Location, Last Used) bei bestehenden Spulen
    if location and spoolman_id:
        _reconcile_spool_metadata(entity_id, spoolman_id, location)

    # State persistieren
    slots_state[entity_id] = {
        "name":         slot["name"],
        "tag_uid":      tag_uid,
        "tray_uuid":    tray_uuid,
        "bambu_uid":    bambu_uid,
        "spoolman_id":  spoolman_id,
        "last_remain":  slot["remain"],
        "uid_source":   uid_source,
        "location":     location,
        "assigned_by":  assigned_by,
    }


# ─────────────────────────────────────────────────────────────────
# AUTO-UID-MANAGEMENT FÜR NEUE SPULEN
# ─────────────────────────────────────────────────────────────────
def manage_spool_uids():
    """
    Überwacht neue Spulen in Spoolman:
    - Findet Spulen OHNE bambu_tag_uid
    - Vergibt automatisch eine UID nach Muster: AUTO_[Timestamp]_[SpoolID]
    - Diese UID bleibt persistent und ermöglicht künftige Erkennung
    """
    try:
        spools = sm_get("/spool")
    except Exception as e:
        log.error(f"UID-Management: Spulenliste konnte nicht geladen werden: {e}")
        return

    for spool in spools:
        if spool.get("archived"):
            continue

        # Prüfe auf existierende bambu_tag_uid
        existing_uid = _normalize_uid(spool.get("extra", {}).get("bambu_tag_uid", ""))
        if existing_uid:
            continue  # Spule hat bereits UID

        # Neue Spule ohne UID → Auto-UID vergeben
        spool_id = spool["id"]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        auto_uid = f"AUTO_{ts}_{spool_id}"

        try:
            # WICHTIG: Bestehende extra-Felder behalten, nicht überschreiben!
            existing_extra = spool.get("extra") or {}

            # Sicherstelle dass extra ein Dict ist (nicht null/string/etc)
            if not isinstance(existing_extra, dict):
                existing_extra = {}

            existing_extra["bambu_tag_uid"] = _sm_extra_val(auto_uid)

            # Versuche mit vollständigem extra-Dict
            try:
                log.debug(f"  Sende extra-Dict zu Spoolman: {existing_extra}")
                sm_patch(f"/spool/{spool_id}", {
                    "extra": existing_extra
                })
                log.info(f"✅ UID-Management: Spule {spool_id} ({spool['filament']['name']}) → UID {auto_uid}")
            except Exception as full_error:
                # Fallback: Versuche nur die UID zu setzen (einfacher API-Call)
                log.warning(f"  ⚠️ Fallback (full patch fehlgeschlagen): {full_error}")
                try:
                    sm_patch(f"/spool/{spool_id}", {
                        "extra": {"bambu_tag_uid": _sm_extra_val(auto_uid)}
                    })
                    log.info(f"✅ UID-Management: Spule {spool_id} ({spool['filament']['name']}) → UID {auto_uid} (Fallback)")
                except Exception as fallback_error:
                    log.error(f"❌ Fallback auch fehlgeschlagen: {fallback_error}")
                    raise
        except Exception as e:
            log.error(f"❌ UID-Management: Fehler beim Setzen der UID für Spule {spool_id}: {e}")


# ─────────────────────────────────────────────────────────────────
# RECONCILIATION
# ─────────────────────────────────────────────────────────────────
def reconcile_all_locations(state):
    """
    Vollabgleich der Locations (alle RECONCILE_INTERVAL_SECONDS):
    - Aktuell belegte Slots: Location + Last Used pflegen (korrekter AMS-Slot).
    - Bambu-verknüpfte Spulen (haben eine bambu_tag_uid), die keinem aktuell belegten
      Slot mehr zugeordnet sind: zurück auf 'Lager' setzen — egal ob sie noch eine
      AMS-/Drucker-Location tragen oder gar keine (z.B. weil ein vorheriger PATCH
      wegen eines anderen Feldes fehlgeschlagen ist).
    """
    slots_state = state.get("slots", {})

    active_spoolman_ids = set()
    for entity_id, slot_state in slots_state.items():
        if not slot_state or slot_state.get("spoolman_id") is None:
            continue
        spoolman_id = slot_state["spoolman_id"]
        active_spoolman_ids.add(spoolman_id)
        location = slot_state.get("location") or _get_location_from_entity(entity_id)
        if location:
            _reconcile_spool_metadata(entity_id, spoolman_id, location)

    try:
        spools = sm_get("/spool")
    except Exception as e:
        log.error(f"Reconciliation: Spulenliste konnte nicht geladen werden: {e}")
        return

    for spool in spools:
        spool_id = spool["id"]
        if spool_id in active_spoolman_ids:
            continue
        is_bambu_managed = bool(_normalize_uid(spool.get("extra", {}).get("bambu_tag_uid", "")))
        current_location = spool.get("location") or ""
        if is_bambu_managed and current_location.lower() != "lager":
            try:
                sm_patch(f"/spool/{spool_id}", {"location": "Lager"})
                log.info(
                    f"Reconciliation: Spule ID={spool_id} nicht mehr im AMS/Drucker "
                    f"– Location '{current_location}' → 'Lager'"
                )
            except Exception as e:
                log.error(f"Reconciliation: Spule ID={spool_id} Location-Reset fehlgeschlagen: {e}")


# ─────────────────────────────────────────────────────────────────
# HAUPTSCHLEIFE
# ─────────────────────────────────────────────────────────────────
def sync_loop():
    info = _get_git_info()
    log.info("=" * 60)
    log.info("SpoolManSyncBambu - Sync-Job (sync.py)")
    log.info(f"Version: {info['version']}")
    log.info(f"Commit:  {info['commit']} ({info['timestamp']})")
    log.info(f"HA:        {HA_URL}")
    log.info(f"Spoolman:  {SPOOLMAN_URL}")
    log.info(f"Intervall: {POLL_INTERVAL}s")
    log.info(f"Location-Vollabgleich: alle {RECONCILE_INTERVAL_SECONDS}s")
    if INTEGRATION_RELOAD_ENABLED:
        log.info(
            f"Failsafe: Reload von {', '.join(RELOAD_DOMAINS)} nach "
            f"{RELOAD_AFTER_UNAVAILABLE_POLLS} Polls ohne Sensoren "
            f"(Cooldown {RELOAD_COOLDOWN_SECONDS}s, max. {RELOAD_MAX_ATTEMPTS} Versuche)"
        )
    else:
        log.info("Failsafe: Auto-Reload deaktiviert (INTEGRATION_RELOAD_ENABLED=0)")
    log.info("=" * 60)

    state = load_state()
    last_reconcile = 0
    consecutive_errors = 0
    max_backoff = 300  # Max 5 Minuten Backoff
    min_backoff = POLL_INTERVAL
    location_check_done = False  # Lazy-Init: ensure_location_exists() im ersten Poll

    while True:
        try:
            entity_states = ha_get_states_batch(SLOT_ENTITIES)

            # Failsafe VOR der Slot-Verarbeitung: faellt die Integration komplett
            # aus, wird sie selbstaendig neu geladen. Die Slot-Verarbeitung laeuft
            # danach normal weiter (unavailable Slots bleiben dank process_slot
            # unangetastet, statt Zuordnungen zu verwerfen).
            try:
                failsafe_reload_integrations(state, entity_states)
            except Exception as e:
                log.error(f"Failsafe-Pruefung fehlgeschlagen: {e}")

            # Einmal pro Poll laden und an process_slot durchreichen, statt pro
            # Slot bis zu zweimal die komplette Spulenliste neu abzufragen.
            # Schlaegt der Abruf fehl, bleibt spools_cache None - die einzelnen
            # find_spool_by_*-Aufrufe fragen dann wie bisher pro Slot live ab,
            # damit ein Fehlschlag weiterhin als Fehler auffliegt statt
            # faelschlich eine Duplikat-Spule anzulegen.
            try:
                spools_cache = sm_get("/spool")
            except Exception as e:
                log.warning(f"Spulenliste konnte nicht vorab geladen werden: {e}")
                spools_cache = None

            for entity_id in SLOT_ENTITIES:
                entity_state = entity_states.get(entity_id)
                if not entity_state:
                    log.warning(f"Entität nicht in HA gefunden: {entity_id}")
                    continue
                try:
                    slot = _parse_slot(entity_id, entity_state)
                    process_slot(entity_id, slot, state, spools_cache)
                except Exception as e:
                    log.error(
                        f"{entity_id}: Verarbeitungsfehler: {e}",
                        exc_info=True,
                    )

            if spools_cache is not None:
                try:
                    _release_empty_external_spools(entity_states, spools_cache)
                except Exception as e:
                    log.error(f"Externe Spule: Location-Reset fehlgeschlagen: {e}")

            # Reconciliation alle RECONCILE_INTERVAL_SECONDS: Location, Last Used aktualisieren
            now = time.time()
            if now - last_reconcile >= RECONCILE_INTERVAL_SECONDS:
                try:
                    manage_spool_uids()  # Neue Spulen automatisch mit UID versehen
                    reconcile_all_locations(state)
                    last_reconcile = now
                except Exception as e:
                    log.error(f"Reconciliation fehlgeschlagen: {e}", exc_info=True)

            save_state(state)
            consecutive_errors = 0  # Zurücksetzen bei Erfolg

            # Lazy-Init: Stelle sicher dass externe Spule Location existiert (non-blocking)
            if not location_check_done:
                try:
                    ensure_location_exists("P2S - Extern")
                    location_check_done = True
                except Exception as e:
                    log.debug(f"Location-Check im ersten Poll fehlgeschlagen (überspringen): {e}")
                    location_check_done = True  # Nicht nochmal versuchen, auch wenn fehlgeschlagen

        except requests.exceptions.ConnectionError as e:
            consecutive_errors += 1
            backoff = min(min_backoff * (2 ** min(consecutive_errors - 1, 5)), max_backoff)
            log.error(f"Verbindungsfehler #{consecutive_errors}: {e} – warte {backoff:.0f}s")
            time.sleep(backoff)
            continue
        except requests.exceptions.Timeout:
            consecutive_errors += 1
            backoff = min(min_backoff * (2 ** min(consecutive_errors - 1, 5)), max_backoff)
            log.error(f"API-Timeout #{consecutive_errors} – warte {backoff:.0f}s")
            time.sleep(backoff)
            continue
        except requests.exceptions.HTTPError as e:
            consecutive_errors += 1
            backoff = min(min_backoff * (2 ** min(consecutive_errors - 1, 5)), max_backoff)
            log.error(f"HTTP-Fehler #{consecutive_errors}: {e} – warte {backoff:.0f}s")
            time.sleep(backoff)
            continue
        except Exception as e:
            log.error(f"Unerwarteter Fehler: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────
# EINSTIEGSPUNKT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not HA_TOKEN:
        log.error("HA_TOKEN nicht gesetzt! Bitte .env konfigurieren.")
        sys.exit(1)
    sync_loop()
